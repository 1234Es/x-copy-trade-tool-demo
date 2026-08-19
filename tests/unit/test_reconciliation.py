from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.broker.reconciliation import Reconciler
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.risk_manager import RiskManager
from app.storage.database import create_db_engine
from app.storage.repository import Repository

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def repository() -> Repository:
    return Repository(create_db_engine("sqlite:///:memory:"))


def _seed_open_trade(repository: Repository, trade_id: str = "t1", instrument: str = "EUR_USD") -> None:
    repository.insert_order(
        {
            "order_id": f"o-{trade_id}", "signal_id": f"o-{trade_id}", "oanda_order_id": f"ord-{trade_id}",
            "instrument": instrument, "units": -12500, "status": "filled",
            "submitted_at": NOW, "broker_response_json": "{}",
        }
    )
    repository.insert_trade(
        {
            "oanda_trade_id": trade_id, "order_id": f"o-{trade_id}", "instrument": instrument, "direction": "short",
            "open_price": 1.1450, "close_price": None, "open_time": NOW, "close_time": None,
            "realized_pl": None, "exit_reason": None,
        }
    )


def _risk_manager() -> RiskManager:
    config = {"cooldown_after_loss_minutes": 60}
    return RiskManager(config, CircuitBreaker())


def test_reconcile_does_nothing_when_states_match(repository):
    _seed_open_trade(repository)
    broker = MagicMock()
    broker.get_open_trades.return_value = [{"id": "t1"}]
    reconciler = Reconciler(broker, repository)

    summary = reconciler.reconcile(now=NOW)

    assert not summary.closed_synced
    assert not summary.has_unexplained_mismatch
    broker.get_trade.assert_not_called()


def test_reconcile_syncs_a_normally_closed_trade(repository):
    _seed_open_trade(repository)
    broker = MagicMock()
    broker.get_open_trades.return_value = []  # no longer open at broker
    broker.get_trade.return_value = {
        "state": "CLOSED",
        "averageClosePrice": "1.1470",
        "closeTime": "2026-07-15T11:00:00.123456789Z",
        "realizedPL": "-25.50",
        "closingTransactionIDs": ["tx1"],
    }
    broker.get_transaction.return_value = {"reason": "STOP_LOSS_ORDER"}
    reconciler = Reconciler(broker, repository)
    rm = _risk_manager()

    summary = reconciler.reconcile(risk_manager=rm, now=NOW)

    assert summary.closed_synced == ["t1"]
    assert not summary.has_unexplained_mismatch

    trades = repository.get_all_trades()
    assert len(trades) == 1
    assert trades[0]["close_price"] == 1.1470
    assert trades[0]["realized_pl"] == -25.50
    assert trades[0]["exit_reason"] == "stop_loss"
    assert trades[0]["close_time"].year == 2026

    # A loss should have been recorded against the risk manager's
    # consecutive-loss/cooldown tracking.
    assert rm._consecutive_losses == 1


def test_reconcile_does_not_trip_circuit_breaker_on_normal_close(repository):
    from sqlalchemy import select

    from app.storage.models import circuit_breaker_events

    _seed_open_trade(repository)
    broker = MagicMock()
    broker.get_open_trades.return_value = []
    broker.get_trade.return_value = {
        "state": "CLOSED", "averageClosePrice": "1.1470", "closeTime": "2026-07-15T11:00:00Z",
        "realizedPL": "12.00", "closingTransactionIDs": ["tx1"],
    }
    broker.get_transaction.return_value = {"reason": "TAKE_PROFIT_ORDER"}
    reconciler = Reconciler(broker, repository)

    reconciler.reconcile(now=NOW)

    with repository.engine.connect() as conn:
        rows = conn.execute(select(circuit_breaker_events)).fetchall()
    assert rows == []


def test_reconcile_flags_unexplained_trade_as_mismatch_not_a_silent_close(repository):
    _seed_open_trade(repository)
    broker = MagicMock()
    broker.get_open_trades.return_value = []
    broker.get_trade.return_value = None  # broker can't explain it at all
    reconciler = Reconciler(broker, repository)

    summary = reconciler.reconcile(now=NOW)

    assert summary.unexplained == ["t1"]
    assert not summary.closed_synced
    assert summary.has_unexplained_mismatch

    trades = repository.get_all_trades()
    assert trades[0]["close_time"] is None  # never marked closed on an unexplained mismatch


def test_reconcile_flags_trade_open_at_broker_but_unknown_locally(repository):
    broker = MagicMock()
    broker.get_open_trades.return_value = [{"id": "mystery-trade"}]
    reconciler = Reconciler(broker, repository)

    summary = reconciler.reconcile(now=NOW)

    assert summary.open_at_broker_not_local == ["mystery-trade"]
    assert summary.has_unexplained_mismatch


def test_reconcile_handles_multiple_trades_independently(repository):
    _seed_open_trade(repository, trade_id="t1", instrument="EUR_USD")
    _seed_open_trade(repository, trade_id="t2", instrument="GBP_USD")
    broker = MagicMock()
    broker.get_open_trades.return_value = [{"id": "t2"}]  # t1 closed, t2 still open

    def fake_get_trade(trade_id):
        if trade_id == "t1":
            return {
                "state": "CLOSED", "averageClosePrice": "1.1470", "closeTime": "2026-07-15T11:00:00Z",
                "realizedPL": "5.00", "closingTransactionIDs": ["tx1"],
            }
        return None

    broker.get_trade.side_effect = fake_get_trade
    broker.get_transaction.return_value = {"reason": "MARKET_ORDER_TRADE_CLOSE"}
    reconciler = Reconciler(broker, repository)

    summary = reconciler.reconcile(now=NOW)

    assert summary.closed_synced == ["t1"]
    assert not summary.unexplained

    open_trades = repository.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["oanda_trade_id"] == "t2"
