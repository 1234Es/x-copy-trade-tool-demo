from datetime import datetime, timezone

import pytest

from app.storage.database import create_db_engine
from app.storage.repository import Repository

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def repository() -> Repository:
    return Repository(create_db_engine("sqlite:///:memory:"))


def _seed_signal(repository: Repository, signal_id: str, author: str) -> None:
    repository.insert_signal(
        {
            "signal_id": signal_id, "post_id": f"post-{signal_id}", "author": author,
            "signal_type": "new_trade", "instrument": "EUR_USD", "direction": "short",
            "order_type": None, "entry_price": None, "entry_zone_low": None, "entry_zone_high": None,
            "stop_loss": None, "take_profit_json": "[]", "timeframe": None, "valid_until": None,
            "referenced_trade_id": None, "confidence": 0.9, "evidence_json": "[]", "assumptions_json": "[]",
            "missing_fields_json": "[]", "requires_human_review": False, "reasoning_summary": None,
            "openai_request_id": None, "validation_status": "approved", "rejection_reason": None,
            "created_at": NOW,
        }
    )


def _seed_open_trade(repository: Repository, trade_id: str, instrument: str, signal_id: str, author: str) -> None:
    _seed_signal(repository, signal_id, author)
    repository.insert_order(
        {
            "order_id": f"o-{trade_id}", "signal_id": signal_id, "oanda_order_id": f"ord-{trade_id}",
            "instrument": instrument, "units": -1000, "status": "filled",
            "submitted_at": NOW, "broker_response_json": "{}",
        }
    )
    repository.insert_trade(
        {
            "oanda_trade_id": trade_id, "order_id": f"o-{trade_id}", "instrument": instrument, "direction": "short",
            "open_price": 1.1000, "close_price": None, "open_time": NOW, "close_time": None,
            "realized_pl": None, "exit_reason": None,
        }
    )


def test_get_open_trades_with_source_attributes_each_trade_to_its_own_author(repository):
    # Regression test: execution_engine._build_account_state() used to
    # stamp every open position with whoever's signal was CURRENTLY being
    # evaluated, regardless of who actually opened it -- meaning any account
    # with >=2 positions open globally (from any source) looked like it had
    # hit ITS OWN per-source-account cap. This asserts each trade carries
    # its real, distinct author.
    _seed_open_trade(repository, "t1", "EUR_USD", "sig-1", "waltervannelli")
    _seed_open_trade(repository, "t2", "GBP_USD", "sig-2", "trynahustle")

    trades = repository.get_open_trades_with_source()

    by_id = {t["oanda_trade_id"]: t for t in trades}
    assert by_id["t1"]["source_account"] == "waltervannelli"
    assert by_id["t2"]["source_account"] == "trynahustle"


def test_get_open_trades_with_source_handles_trade_with_no_resolvable_signal(repository):
    # Left join, not inner -- a trade whose order/signal chain can't be
    # resolved (e.g. legacy data) must still be counted, just with
    # source_account=None, rather than silently disappearing from
    # max_open_positions accounting.
    repository.insert_order(
        {
            "order_id": "o-orphan", "signal_id": "sig-missing", "oanda_order_id": "ord-orphan",
            "instrument": "EUR_USD", "units": -1000, "status": "filled",
            "submitted_at": NOW, "broker_response_json": "{}",
        }
    )
    repository.insert_trade(
        {
            "oanda_trade_id": "t-orphan", "order_id": "o-orphan", "instrument": "EUR_USD", "direction": "short",
            "open_price": 1.1000, "close_price": None, "open_time": NOW, "close_time": None,
            "realized_pl": None, "exit_reason": None,
        }
    )

    trades = repository.get_open_trades_with_source()

    assert len(trades) == 1
    assert trades[0]["source_account"] is None
