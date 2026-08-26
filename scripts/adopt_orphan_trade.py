"""Teach the local database about a trade that is open at OANDA but has no
local record -- the `open_at_broker_not_local` half of a reconciliation
mismatch (app/broker/reconciliation.py), which halts trading until resolved.

This exists because the deployment's SQLite file used to live on Render's
ephemeral filesystem and was wiped on every redeploy, while the positions it
described stayed open at the broker. Adopting is the alternative to closing
such a position by hand: it reconstructs the minimum set of rows needed for
the system to treat the trade as one of its own.

Three rows are written, mirroring what execution_engine/order_manager would
have produced:
  - `signals`  -- needed for BOTH per-source-account risk attribution and,
                  more importantly, so a later "close" post can resolve this
                  trade via referenced_trade_id (context_engine's candidate
                  list is built by joining signals -> orders -> trades; with
                  no signals row the position could never be closed by a
                  signal, only by hand or by its stop/target).
  - `orders`   -- the join between the signal and the trade.
  - `trades`   -- what reconciliation and the dashboard actually read.

The signals row is SYNTHETIC and deliberately marked as such: it was not
produced by the NLP pipeline, so it carries validation_status="adopted"
(never "approved"), openai_request_id=None, empty evidence, and a
reasoning_summary saying plainly where it came from. This system's audit
trail is only worth having if a reconstructed record can't be mistaken for
a real one.

Usage (run against the SAME database the app uses):
    DATABASE_URL=... OANDA_API_TOKEN=... OANDA_ACCOUNT_ID=... \
        python -m scripts.adopt_orphan_trade 648 --author waltervannelli

For the Render deployment, DATABASE_URL must be the database's EXTERNAL
connection string (the internal one only resolves inside Render's network).
Trade details are read from OANDA rather than passed in, so the adopted row
matches the broker's own view of the position rather than a typo.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from app.broker.oanda_practice import OandaPracticeBroker, OandaPracticeConfig
from app.config.settings import load_settings
from app.storage.database import create_db_engine
from app.storage.repository import Repository


def _parse_oanda_time(value: str) -> datetime:
    # Same nanosecond-precision handling as broker/reconciliation.py.
    if "." in value:
        head, frac_and_zone = value.split(".", 1)
        frac = frac_and_zone.rstrip("Z")[:6]
        value = f"{head}.{frac}+00:00"
    else:
        value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def adopt(trade_id: str, author: str, dry_run: bool) -> int:
    settings = load_settings()
    if not (settings.oanda_api_token and settings.oanda_account_id):
        print("ERROR: OANDA_API_TOKEN and OANDA_ACCOUNT_ID must be set.", file=sys.stderr)
        return 1

    broker = OandaPracticeBroker(
        OandaPracticeConfig(api_token=settings.oanda_api_token, account_id=settings.oanda_account_id),
        environment=settings.oanda_environment,
    )
    repository = Repository(create_db_engine(settings.database_url))

    trade = broker.get_trade(trade_id)
    if trade is None:
        print(f"ERROR: OANDA has no trade {trade_id} on this account.", file=sys.stderr)
        return 1
    if trade.get("state") != "OPEN":
        print(
            f"ERROR: trade {trade_id} is in state {trade.get('state')!r}, not OPEN. "
            "Only a currently-open position can be adopted; a closed one should be left to "
            "the reconciliation loop.",
            file=sys.stderr,
        )
        return 1

    if any(t["oanda_trade_id"] == trade_id for t in repository.get_open_trades()):
        print(f"Trade {trade_id} is already tracked locally -- nothing to do.")
        return 0

    units = int(float(trade["currentUnits"]))
    instrument = trade["instrument"]
    direction = "long" if units > 0 else "short"
    open_price = float(trade["price"])
    open_time = _parse_oanda_time(trade["openTime"])
    now = datetime.now(timezone.utc)

    # "adopted-" prefixed ids are self-describing in the audit trail and can
    # never collide with a real post_id (numeric) or a uuid4 signal_id.
    signal_id = f"adopted-{trade_id}"
    order_id = f"adopted-{trade_id}:{instrument}"

    print(f"  trade {trade_id}: {direction} {abs(units)} {instrument} @ {open_price} opened {open_time.isoformat()}")
    print(f"  unrealized P&L at broker: {trade.get('unrealizedPL', 'n/a')}")
    print(f"  will attribute to author: {author}")
    if dry_run:
        print("\nDry run -- nothing written. Re-run without --dry-run to apply.")
        return 0

    repository.insert_signal(
        {
            "signal_id": signal_id,
            "post_id": signal_id,
            "author": author,
            "signal_type": "new_trade",
            "instrument": instrument,
            "direction": direction,
            "order_type": "market",
            "entry_price": open_price,
            "entry_zone_low": None,
            "entry_zone_high": None,
            "stop_loss": None,
            "take_profit_json": json.dumps([]),
            "timeframe": None,
            "valid_until": None,
            "referenced_trade_id": None,
            "confidence": 0.0,
            "evidence_json": json.dumps([]),
            "assumptions_json": json.dumps(
                ["Synthetic record: reconstructed from OANDA's own view of an already-open position."]
            ),
            "missing_fields_json": json.dumps([]),
            "requires_human_review": False,
            "reasoning_summary": (
                f"SYNTHETIC record written by scripts/adopt_orphan_trade.py on {now.isoformat()}. "
                f"OANDA trade {trade_id} was open at the broker with no local record (its original "
                "signal was lost when the deployment's ephemeral SQLite database was wiped). This row "
                "exists so the position can be risk-attributed and closed by a future signal. It was "
                "NOT produced by the classification/extraction pipeline and no OpenAI call backs it."
            ),
            "openai_request_id": None,
            "validation_status": "adopted",
            "rejection_reason": None,
            "created_at": now,
        }
    )
    repository.insert_order(
        {
            "order_id": order_id,
            "signal_id": signal_id,
            "oanda_order_id": None,
            "instrument": instrument,
            "units": units,
            "status": "filled",
            "submitted_at": open_time,
            "broker_response_json": None,
        }
    )
    repository.insert_trade(
        {
            "oanda_trade_id": trade_id,
            "order_id": order_id,
            "instrument": instrument,
            "direction": direction,
            "open_price": open_price,
            "close_price": None,
            "open_time": open_time,
            "close_time": None,
            "realized_pl": None,
            "exit_reason": None,
        }
    )

    print(f"\nAdopted trade {trade_id}. The next reconciliation pass should find no mismatch.")
    print("Clear the circuit breaker from the dashboard once you've confirmed that.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trade_id", help="The OANDA trade id to adopt, e.g. 648")
    parser.add_argument(
        "--author",
        default="waltervannelli",
        help="Tracked account to attribute the position to, for per-source-account risk limits "
        "(default: waltervannelli)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written without writing it")
    args = parser.parse_args()
    return adopt(args.trade_id, args.author, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
