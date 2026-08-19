from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from app.broker.base_broker import InstrumentMetadata, PriceTick
from app.execution.signal_validator import SignalValidator
from app.nlp.schemas import Direction, OrderType, SignalType, TradeSignal
from app.storage.database import create_db_engine
from app.storage.repository import Repository
from tests.fixtures.sample_posts import NOW

ALIASES = {"eurusd": "EUR_USD", "gbpusd": "GBP_USD"}
KNOWN_UNSUPPORTED = {"aapl"}


@pytest.fixture
def repository() -> Repository:
    engine = create_db_engine("sqlite:///:memory:")
    return Repository(engine)


def _metadata() -> InstrumentMetadata:
    return InstrumentMetadata(name="EUR_USD", pip_location=-4, display_precision=5, minimum_trade_size=1, margin_rate=0.05, trade_units_precision=0)


def _broker(price: float = 1.0850, spread: float = 0.0001, tradeable: bool = True) -> MagicMock:
    broker = MagicMock()
    broker.get_instrument_metadata.return_value = _metadata()
    broker.get_pricing.return_value = PriceTick(instrument="EUR_USD", bid=price, ask=price + spread, tradeable=tradeable)
    return broker


def _signal(**overrides) -> TradeSignal:
    base = dict(
        post_id="post-1",
        author="waltervannelli",
        signal_type=SignalType.NEW_TRADE,
        instrument="eurusd",
        direction=Direction.LONG,
        order_type=OrderType.MARKET,
        entry_price=None,
        stop_loss=1.0800,
        take_profit=[1.0950],
        confidence=0.95,
        requires_human_review=False,
    )
    base.update(overrides)
    return TradeSignal(**base)


def test_approves_valid_signal(repository):
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(), NOW, NOW + timedelta(minutes=2))
    assert result.approved
    assert result.oanda_instrument == "EUR_USD"


def test_rejects_stale_signal(repository):
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(), NOW, NOW + timedelta(minutes=30))
    assert not result.approved
    assert "signal_too_stale" in result.reason


def test_rejects_missing_instrument(repository):
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(instrument=None), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert result.reason == "missing_instrument"


def test_rejects_known_unsupported_instrument(repository):
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(instrument="AAPL"), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert "known_unsupported_instrument" in result.reason


def test_rejects_unmapped_instrument(repository):
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(instrument="xyzcoin"), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert "unmapped_instrument" in result.reason


def test_rejects_instrument_unavailable_on_account(repository):
    broker = _broker()
    broker.get_instrument_metadata.return_value = None
    validator = SignalValidator(repository, broker, ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert "instrument_unavailable_on_account" in result.reason


def test_rejects_missing_direction_for_new_trade(repository):
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(direction=None), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert result.reason == "missing_direction"


def test_rejects_missing_stop_loss_for_new_trade(repository):
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(stop_loss=None), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert "missing_stop_loss" in result.reason


def test_rejects_market_closed(repository):
    validator = SignalValidator(repository, _broker(tradeable=False), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert result.reason == "market_closed"


def test_rejects_entry_too_far_from_market(repository):
    # current price ~1.0850, entry stated far away -> should reject
    validator = SignalValidator(repository, _broker(price=1.0850), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10, max_entry_distance_percent=0.1)
    result = validator.validate(_signal(entry_price=1.2000), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert "entry_too_far_from_market" in result.reason


def test_rejects_stop_loss_wrong_side_for_long(repository):
    validator = SignalValidator(repository, _broker(price=1.0850), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(direction=Direction.LONG, stop_loss=1.0900), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert result.reason == "stop_loss_wrong_side_for_long"


def test_validation_does_not_self_reject_the_signal_row_just_written_for_this_attempt(repository):
    # execution_engine.py inserts the signal row for the CURRENT attempt
    # before calling validate() (for audit-trail purposes, Phase 11) --
    # validate() must not treat that just-written row as a pre-existing
    # duplicate. Real post-level dedup happens earlier via raw_posts'
    # primary key; real order-level dedup happens in order_manager.py.
    repository.insert_signal(
        {
            "signal_id": "this-attempt-sig", "post_id": "post-1", "author": "waltervannelli", "signal_type": "new_trade",
            "instrument": "EUR_USD", "direction": "long", "order_type": "market", "entry_price": None,
            "entry_zone_low": None, "entry_zone_high": None, "stop_loss": 1.08, "take_profit_json": "[1.09]",
            "timeframe": None, "valid_until": None, "referenced_trade_id": None, "confidence": 0.9,
            "evidence_json": "[]", "assumptions_json": "[]", "missing_fields_json": "[]",
            "requires_human_review": False, "reasoning_summary": None, "openai_request_id": None,
            "validation_status": "pending", "rejection_reason": None, "created_at": NOW,
        }
    )
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(post_id="post-1"), NOW, NOW + timedelta(minutes=1))
    assert result.approved


def test_applies_fallback_stop_loss_and_take_profit_when_configured(repository):
    validator = SignalValidator(
        repository, _broker(price=1.0850), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10,
        missing_stop_loss_behavior="apply_risk_model",
        fallback_stop_distance_pips={"EUR_USD": 20},
        fallback_reward_to_risk_multiple=1.5,
    )
    result = validator.validate(_signal(direction=Direction.SHORT, stop_loss=None, take_profit=[]), NOW, NOW + timedelta(minutes=1))
    assert result.approved
    assert result.stop_loss_is_fallback
    assert result.take_profit_is_fallback
    # SHORT: current price (bid) = 1.0850, 20 pips = 0.0020 -> stop above entry
    assert result.effective_stop_loss_price == pytest.approx(1.0870)
    # target at 1.5x the stop distance below the reference price
    assert result.effective_take_profit_price == pytest.approx(1.0850 - 0.0020 * 1.5)


def test_apply_risk_model_still_rejects_when_no_fallback_distance_configured(repository):
    validator = SignalValidator(
        repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10,
        missing_stop_loss_behavior="apply_risk_model",
        fallback_stop_distance_pips={},  # no entry for EUR_USD
    )
    result = validator.validate(_signal(stop_loss=None), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert "no_fallback_stop_distance_configured" in result.reason


def test_human_review_mode_still_rejects_missing_stop_loss(repository):
    # Default behavior (missing_stop_loss_behavior not passed) must remain
    # unchanged -- accounts that DO state a stop still get rejected outright
    # when they don't, unless apply_risk_model is deliberately turned on.
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(stop_loss=None), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert "missing_stop_loss_requires_human_review" in result.reason


def test_stated_take_profit_is_never_overridden_by_fallback(repository):
    validator = SignalValidator(
        repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10,
        missing_stop_loss_behavior="apply_risk_model",
        fallback_stop_distance_pips={"EUR_USD": 20},
    )
    result = validator.validate(_signal(stop_loss=1.0800, take_profit=[1.0950]), NOW, NOW + timedelta(minutes=1))
    assert result.approved
    assert not result.stop_loss_is_fallback
    assert not result.take_profit_is_fallback
    assert result.effective_stop_loss_price == 1.0800
    assert result.effective_take_profit_price == 1.0950


def test_rejects_conflicting_open_position(repository):
    repository.insert_order(
        {"order_id": "o1", "signal_id": "o1", "oanda_order_id": "b1", "instrument": "EUR_USD", "units": -1000,
         "status": "filled", "submitted_at": NOW, "broker_response_json": "{}"}
    )
    repository.insert_trade(
        {"oanda_trade_id": "t1", "order_id": "o1", "instrument": "EUR_USD", "direction": "short",
         "open_price": 1.09, "close_price": None, "open_time": NOW, "close_time": None,
         "realized_pl": None, "exit_reason": None}
    )
    validator = SignalValidator(repository, _broker(), ALIASES, KNOWN_UNSUPPORTED, max_signal_age_minutes=10)
    result = validator.validate(_signal(direction=Direction.LONG, post_id="post-2"), NOW, NOW + timedelta(minutes=1))
    assert not result.approved
    assert result.reason == "conflicting_open_position_opposite_direction"
