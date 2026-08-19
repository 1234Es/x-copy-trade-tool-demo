"""Run once before committing data/copy_trader.db to the public repo (and
again any time it's re-baked into the deployed image). Blanks the only
columns anywhere in the schema that could contain the real OANDA account ID
(embedded in broker response bodies / reconciliation state, e.g. from a URL
like https://api-fxpractice.oanda.com/v3/accounts/<id>/...) -- see
app/storage/models.py. Nothing the dashboard reads touches these columns
(verified in app/storage/repository.py: /api/trades and /api/proposals read
straight from the self-contained trades/proposals tables, never orders or
reconciliation_log), so this has no effect on what's shown publicly.

Usage: python scripts/sanitize_public_columns.py [path/to/copy_trader.db]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "copy_trader.db"


def sanitize(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("UPDATE orders SET broker_response_json = NULL WHERE broker_response_json IS NOT NULL")
        orders_cleared = cursor.rowcount
        cursor = conn.execute(
            "UPDATE reconciliation_log SET local_state_json = NULL, broker_state_json = NULL, "
            "discrepancy_json = NULL WHERE local_state_json IS NOT NULL OR broker_state_json IS NOT NULL "
            "OR discrepancy_json IS NOT NULL"
        )
        reconciliation_cleared = cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"Cleared broker_response_json on {orders_cleared} order row(s).")
    print(f"Cleared local/broker/discrepancy JSON on {reconciliation_cleared} reconciliation_log row(s).")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    if not path.exists():
        raise SystemExit(f"No database found at {path}")
    sanitize(path)
