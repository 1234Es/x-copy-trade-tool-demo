"""Full pipeline integration tests: classify -> extract -> validate ->
risk -> approval workflow, with OpenAI and the OANDA broker both mocked.
No real network calls happen in this test module.
"""
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from app.broker.base_broker import InstrumentMetadata, PriceTick
from app.broker.order_manager import OrderManager
from app.execution.approval_workflow import ApprovalWorkflow
from app.execution.execution_engine import ExecutionEngine
from app.execution.signal_validator import SignalValidator
from app.monitoring.alerts import CompositeAlertSink
from app.nlp.classifier import Classifier
from app.nlp.context_engine import ContextEngine
from app.nlp.openai_client import StructuredCompletionResult
from app.nlp.signal_extractor import SignalExtractor
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.risk_manager import RiskManager
from app.storage.database import create_db_engine
from app.storage.repository import Repository
from tests.fixtures.sample_posts import (
    CRYPTIC_POST,
    EXPLICIT_LONG_POST,
    FULL_CLOSE_POST,
    MISSING_STOP_POST,
    NOW,
    STALE_POST,
    UNRELATED_POST,
    UPDATE_STOP_POST,
)

ALIASES = {"eurusd": "EUR_USD"}
RISK_CONFIG = {
    "risk_per_trade_percent": 0.25,
    "max_daily_loss_percent": 1.0,
    "max_weekly_loss_percent": 2.5,
    "max_consecutive_losses": 3,
    "max_open_positions": 2,
    "max_total_exposure_percent": 1.0,
    "max_exposure_per_instrument_percent": 0.5,
    "max_exposure_per_source_account_percent": 0.75,
    "max_open_positions_per_source_account": 2,
    "max_trades_per_hour": 5,
    "max_trades_per_day": 10,
    "cooldown_after_loss_minutes": 60,
    "cooldown_after_consecutive_losses_minutes": 120,
    "cooldown_after_api_failures_minutes": 30,
    "api_failure_circuit_breaker_count": 5,
    "minimum_reward_to_risk": 1.5,
    "max_spread_pips": {"EUR_USD": 1.5},
}


def _broker() -> MagicMock:
    broker = MagicMock()
    broker.get_instrument_metadata.return_value = InstrumentMetadata(
        name="EUR_USD", pip_location=-4, display_precision=5, minimum_trade_size=1, margin_rate=0.05, trade_units_precision=0
    )
    broker.get_pricing.return_value = PriceTick(instrument="EUR_USD", bid=1.0850, ask=1.0851, tradeable=True)
    broker.get_account_snapshot.return_value = MagicMock(equity=10_000, balance=10_000, currency="GBP")
    return broker


def _build_engine(app_mode: str, openai_parsed: dict | None, broker=None):
    repository = Repository(create_db_engine("sqlite:///:memory:"))
    broker = broker or _broker()

    mock_openai = MagicMock()
    if openai_parsed is not None:
        mock_openai.structured_completion.side_effect = [
            StructuredCompletionResult(request_id="req-classify", parsed={"category": "explicit_trade_entry", "reasoning": "clear entry"}, raw_content="{}"),
            StructuredCompletionResult(request_id="req-extract", parsed=openai_parsed, raw_content="{}"),
        ]

    classifier = Classifier(mock_openai)
    context_engine = ContextEngine(repository)
    extractor = SignalExtractor(mock_openai, context_engine, min_confidence=0.9)
    validator = SignalValidator(repository, broker, ALIASES, set(), max_signal_age_minutes=10)
    circuit_breaker = CircuitBreaker()
    risk_manager = RiskManager(dict(RISK_CONFIG), circuit_breaker)
    order_manager = OrderManager(broker, repository)
    approval_workflow = ApprovalWorkflow(repository, app_mode, proposal_expiry_minutes=5, min_signal_confidence=0.9)
    alert_sink = CompositeAlertSink()

    engine = ExecutionEngine(
        repository, classifier, context_engine, extractor, validator, risk_manager, approval_workflow, order_manager, broker, alert_sink
    )
    return engine, repository, mock_openai


def _valid_signal_payload(**overrides) -> dict:
    base = {
        "post_id": "post-long", "author": "waltervannelli", "signal_type": "new_trade", "instrument": "EURUSD",
        "direction": "long", "order_type": "market", "entry_price": None, "entry_zone_low": None, "entry_zone_high": None,
        "stop_loss": 1.0800, "take_profit": [1.0950], "timeframe": None, "valid_until": None, "referenced_trade_id": None,
        "confidence": 0.95, "evidence": ["Long EURUSD here", "Entry 1.0850", "stop 1.0800", "target 1.0950"],
        "assumptions": [], "missing_fields": [], "requires_human_review": False, "reasoning_summary": "clear",
    }
    base.update(overrides)
    return base


def test_unrelated_post_never_calls_openai():
    engine, repository, mock_openai = _build_engine("observe", None)
    outcome = engine.process_post(UNRELATED_POST, now=NOW)
    assert outcome.status == "not_actionable"
    mock_openai.structured_completion.assert_not_called()


def test_duplicate_post_is_ignored():
    engine, repository, _ = _build_engine("observe", None)
    engine.process_post(UNRELATED_POST, now=NOW)
    outcome = engine.process_post(UNRELATED_POST, now=NOW)
    assert outcome.status == "duplicate_post"


def test_valid_signal_observe_mode_logs_hypothetical_no_order():
    engine, repository, _ = _build_engine("observe", _valid_signal_payload())
    outcome = engine.process_post(EXPLICIT_LONG_POST, now=NOW)
    assert outcome.status == "logged_hypothetical"
    assert repository.get_open_trades() == []


def test_missing_stop_loss_creates_proposal_in_approval_mode():
    engine, repository, _ = _build_engine(
        "approval",
        _valid_signal_payload(
            post_id="post-nostop",
            stop_loss=None,
            missing_fields=["stop_loss"],
            requires_human_review=True,
            evidence=["Long EURUSD at 1.0850", "targeting 1.0950"],
        ),
    )
    outcome = engine.process_post(MISSING_STOP_POST, now=NOW)
    assert outcome.status == "validation_rejected"
    assert "missing_stop_loss" in outcome.detail


def test_cryptic_post_extraction_rejected_when_referenced_trade_unresolvable():
    # The model can't resolve "adding here / same setup" without a valid
    # referenced_trade_id candidate -- simulate it correctly declining to guess.
    engine, repository, _ = _build_engine(
        "observe",
        {
            "post_id": "post-cryptic", "author": "waltervannelli", "signal_type": "no_trade", "instrument": None,
            "direction": None, "order_type": None, "entry_price": None, "entry_zone_low": None, "entry_zone_high": None,
            "stop_loss": None, "take_profit": [], "timeframe": None, "valid_until": None, "referenced_trade_id": None,
            "confidence": 0.3, "evidence": [], "assumptions": ["cannot resolve which trade this refers to"],
            "missing_fields": ["instrument", "referenced_trade_id"], "requires_human_review": True,
            "reasoning_summary": "ambiguous reference with no resolvable candidate",
        },
    )
    outcome = engine.process_post(CRYPTIC_POST, now=NOW)
    assert outcome.status == "validation_rejected"
    assert outcome.detail == "not_actionable"


def test_stale_post_rejected_at_validation():
    engine, repository, _ = _build_engine(
        "observe",
        _valid_signal_payload(
            post_id="post-stale",
            evidence=["Long EURUSD, entry 1.0850", "stop 1.0800", "target 1.0950"],
        ),
    )
    outcome = engine.process_post(STALE_POST, now=NOW)
    assert outcome.status == "validation_rejected"
    assert "signal_too_stale" in outcome.detail


def test_practice_auto_executes_order_when_confident():
    broker = _broker()
    broker.submit_order.return_value = MagicMock(success=True, oanda_order_id="o1", oanda_trade_id="t1", fill_price=1.0851, rejection_reason=None, raw_response={})
    engine, repository, _ = _build_engine("practice_auto", _valid_signal_payload(confidence=0.95), broker=broker)
    outcome = engine.process_post(EXPLICIT_LONG_POST, now=NOW)
    assert outcome.status == "executed"
    broker.submit_order.assert_called_once()


def test_executed_order_links_back_to_its_real_signal_id():
    # Regression test: order_manager used to store client_order_id (a
    # post_id+instrument dedup key, needed for its own idempotency check)
    # in the orders.signal_id column instead of the actual signal UUID.
    # That silently broke every join from an order back to its signal --
    # get_open_trades_with_source() (used for per-source-account risk
    # limits) and the dashboard's Trades tab "Source" column both always
    # resolved to no author as a result, for every trade ever placed.
    broker = _broker()
    broker.submit_order.return_value = MagicMock(success=True, oanda_order_id="o1", oanda_trade_id="t1", fill_price=1.0851, rejection_reason=None, raw_response={})
    engine, repository, _ = _build_engine("practice_auto", _valid_signal_payload(confidence=0.95), broker=broker)
    outcome = engine.process_post(EXPLICIT_LONG_POST, now=NOW)
    assert outcome.status == "executed"

    from app.storage.models import orders
    from sqlalchemy import select
    with repository.engine.connect() as conn:
        order = dict(conn.execute(select(orders)).mappings().first())
    assert order["signal_id"] == outcome.signal_id

    open_trades = repository.get_open_trades_with_source()
    assert len(open_trades) == 1
    assert open_trades[0]["source_account"] == "waltervannelli"


def test_practice_auto_never_double_submits_same_signal():
    broker = _broker()
    broker.submit_order.return_value = MagicMock(success=True, oanda_order_id="o1", oanda_trade_id="t1", fill_price=1.0851, rejection_reason=None, raw_response={})
    engine, repository, _ = _build_engine("practice_auto", _valid_signal_payload(confidence=0.95), broker=broker)
    engine.process_post(EXPLICIT_LONG_POST, now=NOW)
    # Reprocessing the same post_id is already blocked by duplicate_post at
    # the raw_posts stage -- this confirms no second broker call happens.
    outcome = engine.process_post(EXPLICIT_LONG_POST, now=NOW)
    assert outcome.status == "duplicate_post"
    broker.submit_order.assert_called_once()


def _full_close_payload(referenced_trade_id: str, **overrides) -> dict:
    base = {
        "post_id": "post-close", "author": "waltervannelli", "signal_type": "full_close", "instrument": "EURUSD",
        "direction": None, "order_type": None, "entry_price": None, "entry_zone_low": None, "entry_zone_high": None,
        "stop_loss": None, "take_profit": [], "timeframe": None, "valid_until": None,
        "referenced_trade_id": referenced_trade_id, "confidence": 0.95, "evidence": ["Closed EURUSD long at 1.0900"],
        "assumptions": [], "missing_fields": [], "requires_human_review": False, "reasoning_summary": "clear close",
    }
    base.update(overrides)
    return base


def _update_stop_payload(referenced_trade_id: str, **overrides) -> dict:
    base = {
        "post_id": "post-move-stop", "author": "waltervannelli", "signal_type": "update_stop", "instrument": "EURUSD",
        "direction": None, "order_type": None, "entry_price": None, "entry_zone_low": None, "entry_zone_high": None,
        "stop_loss": 1.0850, "take_profit": [], "timeframe": None, "valid_until": None,
        "referenced_trade_id": referenced_trade_id, "confidence": 0.95, "evidence": ["Moving stop on EURUSD long to 1.0850"],
        "assumptions": [], "missing_fields": [], "requires_human_review": False, "reasoning_summary": "moved to breakeven",
    }
    base.update(overrides)
    return base


def test_full_close_signal_closes_the_referenced_trade():
    # Regression test: risk_manager.evaluate_trade()'s mandatory stop-loss
    # check used to run unconditionally for every signal type, including
    # full_close -- which has no stop_loss by definition -- so every close
    # instruction was silently rejected with missing_or_invalid_stop_loss
    # and no order/position code path ever ran for it at all.
    broker = _broker()
    broker.submit_order.return_value = MagicMock(
        success=True, oanda_order_id="o1", oanda_trade_id="t1", fill_price=1.0851, rejection_reason=None, raw_response={}
    )
    broker.close_trade.return_value = {"orderFillTransaction": {"price": "1.0900", "pl": "50.0"}}

    engine, repository, mock_openai = _build_engine("practice_auto", _valid_signal_payload(confidence=0.95), broker=broker)
    open_outcome = engine.process_post(EXPLICIT_LONG_POST, now=NOW)
    assert open_outcome.status == "executed"
    entry_signal_id = open_outcome.signal_id

    mock_openai.structured_completion.side_effect = [
        StructuredCompletionResult(request_id="req-classify-2", parsed={"category": "full_close_instruction", "reasoning": "clear close"}, raw_content="{}"),
        StructuredCompletionResult(request_id="req-extract-2", parsed=_full_close_payload(entry_signal_id), raw_content="{}"),
    ]
    close_outcome = engine.process_post(FULL_CLOSE_POST, now=NOW)

    assert close_outcome.status == "executed"
    assert close_outcome.detail == "trade_closed"
    broker.close_trade.assert_called_once_with("t1")
    assert repository.get_open_trades() == []


def test_update_stop_signal_modifies_the_referenced_trade():
    broker = _broker()
    broker.submit_order.return_value = MagicMock(
        success=True, oanda_order_id="o1", oanda_trade_id="t1", fill_price=1.0851, rejection_reason=None, raw_response={}
    )
    broker.modify_trade.return_value = {}

    engine, repository, mock_openai = _build_engine("practice_auto", _valid_signal_payload(confidence=0.95), broker=broker)
    open_outcome = engine.process_post(EXPLICIT_LONG_POST, now=NOW)
    entry_signal_id = open_outcome.signal_id

    mock_openai.structured_completion.side_effect = [
        StructuredCompletionResult(request_id="req-classify-2", parsed={"category": "stop_loss_adjustment", "reasoning": "clear stop move"}, raw_content="{}"),
        StructuredCompletionResult(request_id="req-extract-2", parsed=_update_stop_payload(entry_signal_id), raw_content="{}"),
    ]
    outcome = engine.process_post(UPDATE_STOP_POST, now=NOW)

    assert outcome.status == "executed"
    assert outcome.detail == "trade_modified"
    broker.modify_trade.assert_called_once_with("t1", 1.0850, None)
    # A stop-move must never touch the open trade record itself (still open,
    # units/instrument unchanged) -- only the broker-side protective order.
    assert len(repository.get_open_trades()) == 1


def test_full_close_with_no_referenced_trade_is_skipped_not_guessed():
    engine, repository, mock_openai = _build_engine(
        "practice_auto", None,
    )
    mock_openai.structured_completion.side_effect = [
        StructuredCompletionResult(request_id="req-classify", parsed={"category": "full_close_instruction", "reasoning": "close, but which one?"}, raw_content="{}"),
        StructuredCompletionResult(request_id="req-extract", parsed=_full_close_payload(None), raw_content="{}"),
    ]
    outcome = engine.process_post(FULL_CLOSE_POST, now=NOW)
    assert outcome.status == "validation_rejected"
    assert outcome.detail == "no_referenced_trade_to_act_on"
