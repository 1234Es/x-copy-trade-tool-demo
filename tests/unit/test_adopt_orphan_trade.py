"""Covers the row-shape contract scripts/adopt_orphan_trade.py depends on:
an adopted trade must satisfy reconciliation, risk attribution, and
referenced_trade_id resolution exactly as a pipeline-created one does.
"""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.broker.reconciliation import Reconciler
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.risk_manager import RiskManager
from app.storage.database import create_db_engine
from app.storage.repository import Repository

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
OPEN_TIME = datetime(2026, 8, 25, 10, 17, tzinfo=timezone.utc)
TRADE_ID = "648"


@pytest.fixture
def repository() -> Repository:
    return Repository(create_db_engine("sqlite:///:memory:"))


def _adopt(repository: Repository, author: str = "waltervannelli") -> tuple[str, str]:
    """Mirrors the three inserts scripts/adopt_orphan_trade.py performs."""
    signal_id = f"adopted-{TRADE_ID}"
    order_id = f"adopted-{TRADE_ID}:EUR_USD"
    repository.insert_signal(
        {
            "signal_id": signal_id, "post_id": signal_id, "author": author, "signal_type": "new_trade",
            "instrument": "EUR_USD", "direction": "short", "order_type": "market", "entry_price": 1.16649,
            "entry_zone_low": None, "entry_zone_high": None, "stop_loss": None, "take_profit_json": json.dumps([]),
            "timeframe": None, "valid_until": None, "referenced_trade_id": None, "confidence": 0.0,
            "evidence_json": json.dumps([]), "assumptions_json": json.dumps(["Synthetic record"]),
            "missing_fields_json": json.dumps([]), "requires_human_review": False,
            "reasoning_summary": "SYNTHETIC record written by scripts/adopt_orphan_trade.py",
            "openai_request_id": None, "validation_status": "adopted", "rejection_reason": None, "created_at": NOW,
        }
    )
    repository.insert_order(
        {
            "order_id": order_id, "signal_id": signal_id, "oanda_order_id": None, "instrument": "EUR_USD",
            "units": -20865, "status": "filled", "submitted_at": OPEN_TIME, "broker_response_json": None,
        }
    )
    repository.insert_trade(
        {
            "oanda_trade_id": TRADE_ID, "order_id": order_id, "instrument": "EUR_USD", "direction": "short",
            "open_price": 1.16649, "close_price": None, "open_time": OPEN_TIME, "close_time": None,
            "realized_pl": None, "exit_reason": None,
        }
    )
    return signal_id, order_id


def test_adopted_trade_resolves_the_reconciliation_mismatch(repository):
    broker = MagicMock()
    broker.get_open_trades.return_value = [{"id": TRADE_ID}]
    reconciler = Reconciler(broker, repository)
    rm = RiskManager({"cooldown_after_loss_minutes": 60}, CircuitBreaker())

    # Before adoption: open at broker, unknown locally -> halts trading.
    before = reconciler.reconcile(risk_manager=rm, now=NOW)
    assert before.open_at_broker_not_local == [TRADE_ID]
    assert rm.circuit_breaker.is_tripped(NOW)

    _adopt(repository)
    rm.circuit_breaker.manual_clear()

    after = reconciler.reconcile(risk_manager=rm, now=NOW)
    assert not after.has_unexplained_mismatch
    assert not rm.circuit_breaker.is_tripped(NOW)


def test_adopted_trade_counts_toward_risk_with_its_source_account(repository):
    _adopt(repository, author="waltervannelli")

    open_positions = repository.get_open_trades_with_source()
    assert len(open_positions) == 1
    # Without the synthetic signals row this join yields source_account=None,
    # silently exempting the position from per-source-account exposure caps.
    assert open_positions[0]["source_account"] == "waltervannelli"
    assert open_positions[0]["instrument"] == "EUR_USD"


def test_adopted_trade_can_be_closed_by_a_later_signal(repository):
    # The real point of writing a signals row: context_engine builds
    # referenced_trade_id candidates by joining signals -> orders -> trades,
    # and execution_engine resolves a full_close through
    # get_open_trade_for_signal(). With no signals row an adopted position
    # could never be closed by a signal, only by hand or by its stop/target.
    signal_id, _ = _adopt(repository)

    candidates = repository.get_open_signals_by_author("waltervannelli")
    assert [c["signal_id"] for c in candidates] == [signal_id]

    resolved = repository.get_open_trade_for_signal(signal_id)
    assert resolved is not None
    assert resolved["oanda_trade_id"] == TRADE_ID


def test_adopted_signal_is_not_disguised_as_a_pipeline_signal(repository):
    signal_id, _ = _adopt(repository)
    signal = repository.get_signal(signal_id)

    # An audit trail is only worth having if a reconstructed record can't be
    # mistaken for a real one.
    assert signal["validation_status"] == "adopted"
    assert signal["openai_request_id"] is None
    assert json.loads(signal["evidence_json"]) == []
    assert "SYNTHETIC" in signal["reasoning_summary"]
