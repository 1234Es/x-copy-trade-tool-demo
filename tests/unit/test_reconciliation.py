from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.broker.reconciliation import Reconciler
from app.risk.circuit_breaker import CircuitBreaker, TripReason
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


def _seed_resting_order(repository: Repository, order_id: str = "o-resting", units: int = -21092) -> None:
    """An order accepted by the broker that produced no trade row -- what a
    resting limit/stop order looks like locally until its level is hit."""
    repository.insert_order(
        {
            "order_id": order_id, "signal_id": f"s-{order_id}", "oanda_order_id": "ord-1",
            "instrument": "EUR_USD", "units": units, "status": "filled",
            "submitted_at": NOW, "broker_response_json": "{}",
        }
    )


def test_trade_from_a_resting_order_is_adopted_not_flagged(repository):
    # Regression: a limit order rests, so submit_order returns success with
    # no trade id and order_manager writes no trade row. Whenever the level
    # is hit the broker has a position we've no record of -- which halted
    # trading as an unexplained mismatch, even though we placed the order
    # that became it. Seen live as trade 653.
    _seed_resting_order(repository)
    broker = MagicMock()
    broker.get_open_trades.return_value = [
        {"id": "653", "instrument": "EUR_USD", "currentUnits": "-21092", "price": "1.16530",
         "openTime": "2026-08-26T15:46:42.031931958Z"}
    ]
    reconciler = Reconciler(broker, repository)
    rm = _risk_manager()

    summary = reconciler.reconcile(risk_manager=rm, now=NOW)

    assert summary.adopted_from_resting_order == ["653"]
    assert not summary.has_unexplained_mismatch
    assert not rm.circuit_breaker.is_tripped(NOW)

    trades = repository.get_open_trades()
    assert len(trades) == 1
    assert trades[0]["oanda_trade_id"] == "653"
    assert trades[0]["open_price"] == 1.16530
    assert trades[0]["direction"] == "short"


def test_adopted_resting_trade_keeps_its_source_attribution(repository):
    _seed_resting_order(repository)
    repository.insert_signal(
        {
            "signal_id": "s-o-resting", "post_id": "p1", "author": "waltervannelli", "signal_type": "new_trade",
            "instrument": "EUR_USD", "direction": "short", "order_type": "limit", "entry_price": 1.1653,
            "entry_zone_low": None, "entry_zone_high": None, "stop_loss": None, "take_profit_json": "[]",
            "timeframe": None, "valid_until": None, "referenced_trade_id": None, "confidence": 0.9,
            "evidence_json": "[]", "assumptions_json": "[]", "missing_fields_json": "[]",
            "requires_human_review": False, "reasoning_summary": None, "openai_request_id": None,
            "validation_status": "approved", "rejection_reason": None, "created_at": NOW,
        }
    )
    broker = MagicMock()
    broker.get_open_trades.return_value = [
        {"id": "653", "instrument": "EUR_USD", "currentUnits": "-21092", "price": "1.16530",
         "openTime": "2026-08-26T15:46:42Z"}
    ]

    Reconciler(broker, repository).reconcile(now=NOW)

    # Adoption must join back through the real order, or the position is
    # exempt from per-source-account risk limits.
    positions = repository.get_open_trades_with_source()
    assert positions[0]["source_account"] == "waltervannelli"


def test_unmatched_broker_trade_is_still_flagged(repository):
    # A position opened by hand in OANDA's UI matches no order of ours and
    # must still halt trading -- adoption is for trades we caused, and must
    # not become a way to silently absorb anything the broker reports.
    _seed_resting_order(repository, units=-21092)
    broker = MagicMock()
    broker.get_open_trades.return_value = [
        {"id": "999", "instrument": "GBP_USD", "currentUnits": "-5000", "price": "1.2650",
         "openTime": "2026-08-26T15:46:42Z"}
    ]
    reconciler = Reconciler(broker, repository)
    rm = _risk_manager()

    summary = reconciler.reconcile(risk_manager=rm, now=NOW)

    assert summary.open_at_broker_not_local == ["999"]
    assert not summary.adopted_from_resting_order
    assert rm.circuit_breaker.is_tripped(NOW)


def test_one_resting_order_is_only_claimed_once(repository):
    # Two identical broker trades must not both match the single order that
    # could explain one of them.
    _seed_resting_order(repository)
    broker = MagicMock()
    broker.get_open_trades.return_value = [
        {"id": "653", "instrument": "EUR_USD", "currentUnits": "-21092", "price": "1.16530",
         "openTime": "2026-08-26T15:46:42Z"},
        {"id": "654", "instrument": "EUR_USD", "currentUnits": "-21092", "price": "1.16540",
         "openTime": "2026-08-26T15:47:42Z"},
    ]
    reconciler = Reconciler(broker, repository)

    summary = reconciler.reconcile(now=NOW)

    assert summary.adopted_from_resting_order == ["653"]
    assert summary.open_at_broker_not_local == ["654"]


def test_unexplained_mismatch_actually_trips_the_circuit_breaker(repository):
    # Regression: reconcile() recorded a mismatch to two DB tables but never
    # tripped the breaker itself, so trading continued while local and
    # broker state were known to disagree -- and the dashboard still showed
    # "circuit breaker: clear". RiskManager.record_reconciliation_mismatch()
    # existed for exactly this and was simply never called by anything.
    broker = MagicMock()
    broker.get_open_trades.return_value = [{"id": "mystery-trade"}]
    reconciler = Reconciler(broker, repository)
    rm = _risk_manager()

    reconciler.reconcile(risk_manager=rm, now=NOW)

    assert rm.circuit_breaker.is_tripped(NOW)
    assert TripReason.RECONCILIATION_MISMATCH in rm.circuit_breaker.active_reasons(NOW)
    # No cooldown: an operator must inspect the account and clear it.
    event = next(e for e in rm.circuit_breaker.active_events(NOW) if e.reason == TripReason.RECONCILIATION_MISMATCH)
    assert event.clears_at is None
    assert "mystery-trade" in event.details


def test_matching_state_never_trips_the_circuit_breaker(repository):
    _seed_open_trade(repository)
    broker = MagicMock()
    broker.get_open_trades.return_value = [{"id": "t1"}]
    reconciler = Reconciler(broker, repository)
    rm = _risk_manager()

    reconciler.reconcile(risk_manager=rm, now=NOW)

    assert not rm.circuit_breaker.is_tripped(NOW)


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
