from datetime import datetime, timedelta, timezone

import pytest

from app.monitoring.copy_ledger import build_ledger, parse_close, pips_between
from app.storage.database import create_db_engine
from app.storage.repository import Repository

T0 = datetime(2026, 9, 21, 11, 54, tzinfo=timezone.utc)


@pytest.fixture
def repo() -> Repository:
    return Repository(create_db_engine("sqlite:///:memory:"))


def _post(repo, post_id, text, at):
    repo.insert_raw_post({
        "post_id": post_id, "author": "waltervannelli", "text": text, "posted_at": at, "source": "x_api",
        "reply_to_id": None, "quoted_post_id": None, "is_repost": False, "media_json": "[]",
        "ingested_at": at, "raw_payload_json": None, "superseded_by_post_id": None,
    })


def _trade(repo, n, *, source_entry, open_price, close_price=None, exit_reason=None, pl=None,
           direction="short", opened=T0, filled_after=timedelta(seconds=40), text=None):
    post_id = f"open{n}"
    _post(repo, post_id, text or f"eur/usd sold 2.5 lots at {source_entry}", opened)
    repo.insert_signal({
        "signal_id": f"s{n}", "post_id": post_id, "author": "waltervannelli", "signal_type": "new_trade",
        "instrument": "eur/usd", "direction": direction, "order_type": "market", "entry_price": source_entry,
        "entry_zone_low": None, "entry_zone_high": None, "stop_loss": None, "take_profit_json": "[]",
        "timeframe": None, "valid_until": None, "referenced_trade_id": None, "confidence": 0.9,
        "evidence_json": "[]", "assumptions_json": "[]", "missing_fields_json": "[]",
        "requires_human_review": True, "reasoning_summary": None, "openai_request_id": None,
        "validation_status": "approved", "rejection_reason": None, "created_at": opened,
    })
    repo.insert_order({
        "order_id": f"o{n}", "signal_id": f"s{n}", "oanda_order_id": "1", "instrument": "EUR_USD",
        "units": -21000 if direction == "short" else 21000, "status": "filled",
        "submitted_at": opened + filled_after, "broker_response_json": None,
    })
    repo.insert_trade({
        "oanda_trade_id": str(700 + n), "order_id": f"o{n}", "instrument": "EUR_USD", "direction": direction,
        "open_price": open_price, "close_price": close_price, "open_time": opened + filled_after,
        "close_time": (opened + timedelta(hours=3)) if close_price is not None else None,
        "realized_pl": pl, "exit_reason": exit_reason,
    })


# ---- parsing: real posts from the source account ----------------------------

@pytest.mark.parametrize("text,open_p,close_p,stated,average", [
    ("eur/usd closed short from 1.1489 at 1.1470 +19 pips https://t.co/x", 1.1489, 1.1470, 19.0, False),
    ("eur/usd closed short from 1.1665 at 1.1674 -9 pips daily lost -179 pips on 2.5 lots", 1.1665, 1.1674, -9.0, False),
    ("Eur/usd take off short from 1.1653 &amp; closed at 1.1640+13 pips daily total -166", 1.1653, 1.1640, 13.0, False),
    ("eur/usd take off position short from 1.1618 closed at 1.1587 +31 pips", 1.1618, 1.1587, 31.0, False),
    ("1st step closed short frfom 1.1578 at 1.1632 -54 pips ratio -205 pips", 1.1578, 1.1632, -54.0, False),
    ("eur/usd closed trading inside short from 1.1706 at 1.1695 11 pips dily total -170", 1.1706, 1.1695, 11.0, False),
    ("eur/usd reduced 2.5 lots from average 1.1580at 1.1572 +108 pips", 1.1580, 1.1572, 108.0, True),
    ("eur/usd closed 5 lots short from average 1.1806 at 1.1461 +345 pips = 690 pips", 1.1806, 1.1461, 345.0, True),
])
def test_parses_the_sources_real_close_posts(text, open_p, close_p, stated, average):
    close = parse_close(text)
    assert close is not None
    assert (close.open_price, close.close_price, close.stated_pips, close.is_average) == (open_p, close_p, stated, average)


def test_takes_this_trades_result_not_the_running_total_after_it():
    # "daily lost -179 pips" follows the real result and must not replace it.
    assert parse_close("closed short from 1.1665 at 1.1674 -9 pips daily lost -179 pips").stated_pips == -9.0


@pytest.mark.parametrize("text", [
    "eur/usd sold 2.5 lots at 1.1489",                                              # an open, not a close
    "eur/usd 1st step take off short from 1.1629 from position back to 20 lots at the average 1.1557",  # no close price
    "Good morning dealers eur/usd still short 2.5 lots at the average 1.1484",
])
def test_ignores_posts_that_are_not_a_priced_close(text):
    assert parse_close(text) is None


def test_pips_sign_follows_direction():
    assert pips_between(1.1489, 1.1470, "short", 0.0001) == 19.0
    assert pips_between(1.1489, 1.1470, "long", 0.0001) == -19.0


# ---- pairing and comparison --------------------------------------------------

def test_pairs_his_close_to_his_open_and_compares_with_ours(repo):
    _trade(repo, 1, source_entry=1.1489, open_price=1.14885, close_price=1.1475, exit_reason="signal_full_close", pl=20.0)
    _post(repo, "close1", "eur/usd closed short from 1.1489 at 1.1470 +19 pips", T0 + timedelta(hours=3))

    rows, summary = build_ledger(repo)
    row = rows[0]
    assert row.source_close_post_id == "close1"
    assert row.source_pips == 19.0
    assert row.our_pips == 13.5
    assert row.pips_gap == -5.5  # we did 5.5 pips worse
    assert summary.paired == 1
    assert summary.our_total_pips == 13.5 and summary.source_total_pips == 19.0


def test_slippage_is_positive_when_we_got_a_worse_price(repo):
    # Short: he sold at 1.1489, we sold lower at 1.1487 -> 2 pips worse for us.
    _trade(repo, 1, source_entry=1.1489, open_price=1.1487)
    # Long: he bought at 1.1489, we paid more at 1.1491 -> 2 pips worse for us.
    _trade(repo, 2, source_entry=1.1489, open_price=1.1491, direction="long", opened=T0 + timedelta(days=1))

    rows, _ = build_ledger(repo)
    assert {r.oanda_trade_id: r.entry_slippage_pips for r in rows} == {"701": 2.0, "702": 2.0}


def test_entry_delay_is_measured_from_his_post_to_our_fill(repo):
    _trade(repo, 1, source_entry=1.1489, open_price=1.1489, filled_after=timedelta(seconds=75))
    assert build_ledger(repo)[0][0].entry_delay_seconds == 75.0


def test_a_close_posted_before_the_open_is_never_paired(repo):
    _post(repo, "earlier", "closed short from 1.1489 at 1.1470 +19 pips", T0 - timedelta(days=2))
    _trade(repo, 1, source_entry=1.1489, open_price=1.1489)
    assert build_ledger(repo)[0][0].source_close_post_id is None


def test_one_close_post_is_never_paired_to_two_trades(repo):
    _trade(repo, 1, source_entry=1.1489, open_price=1.1489, close_price=1.1470, exit_reason="signal_full_close")
    _trade(repo, 2, source_entry=1.1489, open_price=1.1489, close_price=1.1470, exit_reason="signal_full_close",
           opened=T0 + timedelta(minutes=30))
    _post(repo, "close1", "closed short from 1.1489 at 1.1470 +19 pips", T0 + timedelta(hours=4))

    rows, summary = build_ledger(repo)
    assert summary.paired == 1
    assert sum(1 for r in rows if r.source_close_post_id == "close1") == 1


def test_a_close_from_a_different_price_is_not_paired(repo):
    # 6 pips away is a different position, not slippage on this one -- a
    # wrong pairing would put a false number in a report meant to be trusted.
    _trade(repo, 1, source_entry=1.1489, open_price=1.1489)
    _post(repo, "other", "closed short from 1.1495 at 1.1470 +25 pips", T0 + timedelta(hours=3))
    assert build_ledger(repo)[0][0].source_close_post_id is None


def test_average_position_closes_are_not_paired_to_single_trades(repo):
    _trade(repo, 1, source_entry=1.1484, open_price=1.1484)
    _post(repo, "avg", "closed short from average 1.1484 at 1.1440 +44 pips", T0 + timedelta(hours=3))
    assert build_ledger(repo)[0][0].source_close_post_id is None


def test_counts_trades_our_stop_closed_while_he_won(repo):
    # The case the fallback stop exists to be judged on.
    _trade(repo, 1, source_entry=1.1489, open_price=1.1489, close_price=1.1509, exit_reason="stop_loss", pl=-31.0)
    _post(repo, "close1", "closed short from 1.1489 at 1.1470 +19 pips", T0 + timedelta(hours=5))
    assert build_ledger(repo)[1].stopped_out_while_source_profited == 1


def test_open_trade_appears_without_a_gap(repo):
    _trade(repo, 1, source_entry=1.1489, open_price=1.1489)
    row = build_ledger(repo)[0][0]
    assert row.status == "open" and row.our_pips is None and row.pips_gap is None


def test_trade_without_a_source_post_still_appears(repo):
    # An adopted trade's synthetic signal has no real post behind it.
    repo.insert_order({
        "order_id": "adopted-648:EUR_USD", "signal_id": "adopted-648", "oanda_order_id": None,
        "instrument": "EUR_USD", "units": -21025, "status": "filled", "submitted_at": T0, "broker_response_json": None,
    })
    repo.insert_trade({
        "oanda_trade_id": "648", "order_id": "adopted-648:EUR_USD", "instrument": "EUR_USD", "direction": "short",
        "open_price": 1.16649, "close_price": None, "open_time": T0, "close_time": None,
        "realized_pl": None, "exit_reason": None,
    })
    rows, summary = build_ledger(repo)
    assert summary.trades == 1
    assert rows[0].source_post_id is None and rows[0].entry_slippage_pips is None


def test_endpoint_serves_the_ledger(repo):
    import json
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.auth import AuthState

    _trade(repo, 1, source_entry=1.1489, open_price=1.14885, close_price=1.1475, exit_reason="signal_full_close")
    _post(repo, "close1", "eur/usd closed short from 1.1489 at 1.1470 +19 pips", T0 + timedelta(hours=3))
    context = MagicMock()
    context.settings.operator_password = ""
    context.repository = repo
    context.auth_state = AuthState()
    context.broker.get_instrument_metadata.return_value = None  # falls back to the default pip size

    body = TestClient(create_app(context)).get("/api/ledger").json()
    json.dumps(body)
    assert body["summary"]["paired"] == 1
    assert body["rows"][0]["pips_gap"] == -5.5
