from datetime import timedelta

from app.risk.circuit_breaker import CircuitBreaker
from app.risk.risk_manager import AccountState, OpenPositionInfo, RiskManager
from tests.fixtures.sample_posts import NOW

BASE_CONFIG = {
    "risk_per_trade_percent": 0.25,
    "max_daily_loss_percent": 1.0,
    "max_weekly_loss_percent": 2.5,
    "max_consecutive_losses": 3,
    "max_open_positions": 2,
    "max_total_exposure_percent": 1.0,
    "max_exposure_per_instrument_percent": 0.5,
    "max_exposure_per_source_account_percent": 0.75,
    "max_open_positions_per_source_account": 2,
    "max_trades_per_hour": 2,
    "max_trades_per_day": 5,
    "cooldown_after_loss_minutes": 60,
    "cooldown_after_consecutive_losses_minutes": 120,
    "cooldown_after_api_failures_minutes": 30,
    "api_failure_circuit_breaker_count": 5,
    "minimum_reward_to_risk": 1.5,
    "max_spread_pips": {"EUR_USD": 1.5},
}


def _account_state(**overrides) -> AccountState:
    base = dict(equity=10_000, balance=10_000, open_positions=[], daily_starting_equity=10_000, weekly_starting_equity=10_000)
    base.update(overrides)
    return AccountState(**base)


def _risk_manager(config=None) -> RiskManager:
    return RiskManager(config or dict(BASE_CONFIG), CircuitBreaker())


def test_approves_valid_trade():
    rm = _risk_manager()
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(), NOW)
    assert decision.approved
    assert decision.position_size.is_valid


def test_rejects_missing_stop_loss():
    rm = _risk_manager()
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0, 0.0020, 1.0, 1.0850, _account_state(), NOW)
    assert not decision.approved
    assert decision.reason == "missing_or_invalid_stop_loss"


def test_rejects_reward_risk_below_minimum():
    rm = _risk_manager()
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0005, 1.0, 1.0850, _account_state(), NOW)
    assert not decision.approved
    assert decision.reason == "reward_risk_below_minimum"


def test_reward_risk_at_exact_floor_survives_float_rounding():
    # Regression test: a target computed as exactly stop_distance * 1.5 can
    # land a hair under 1.5 in floating point (e.g. 1.4999999999999998), which
    # previously caused a spurious reward_risk_below_minimum rejection for a
    # trade that was mathematically fine. This is exactly the real-world
    # floating-point values produced by the fallback stop/target model for a
    # 20-pip EUR_USD stop.
    rm = _risk_manager()
    stop_distance_price = 0.0020000000000000018
    target_distance_price = 0.0029999999999998916  # ~1.5x the stop, minus float dust
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", stop_distance_price, target_distance_price, 1.0, 1.14497, _account_state(), NOW)
    assert decision.approved


def test_still_rejects_reward_risk_meaningfully_below_minimum():
    # The epsilon tolerance must not swallow a real shortfall, only float dust.
    rm = _risk_manager()
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0014, 1.0, 1.0850, _account_state(), NOW)
    assert not decision.approved
    assert decision.reason == "reward_risk_below_minimum"


def test_rejects_spread_too_wide():
    rm = _risk_manager()
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0020, 5.0, 1.0850, _account_state(), NOW)
    assert not decision.approved
    assert decision.reason == "spread_too_wide"


def test_rejects_daily_loss_limit_and_trips_breaker():
    rm = _risk_manager()
    account_state = _account_state(equity=9_800, daily_starting_equity=10_000)  # -2% > 1% limit
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, account_state, NOW)
    assert not decision.approved
    assert decision.reason == "daily_loss_limit_reached"
    assert rm.circuit_breaker.is_tripped(NOW)


def test_rejects_max_open_positions():
    rm = _risk_manager()
    positions = [
        OpenPositionInfo("GBP_USD", "waltervannelli", 0.25),
        OpenPositionInfo("USD_JPY", "waltervannelli", 0.25),
    ]
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(open_positions=positions), NOW)
    assert not decision.approved
    assert decision.reason == "max_open_positions_reached"


def test_rejects_source_account_exposure_limit():
    config = dict(BASE_CONFIG)
    config["max_open_positions_per_source_account"] = 1
    config["max_open_positions"] = 5
    rm = _risk_manager(config)
    positions = [OpenPositionInfo("GBP_USD", "waltervannelli", 0.25)]
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(open_positions=positions), NOW)
    assert not decision.approved
    assert decision.reason == "source_account_exposure_limit_reached"


def test_max_positions_for_source_account_override_takes_priority_over_config():
    # tracked_accounts.yaml's per-account max_open_positions_from_account is
    # passed in per-call via max_positions_for_source_account -- it must win
    # over the config-wide default (2) so a lower per-account limit isn't
    # silently ignored.
    rm = _risk_manager()
    positions = [OpenPositionInfo("GBP_USD", "waltervannelli", 0.25)]
    decision = rm.evaluate_trade(
        "EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(open_positions=positions), NOW,
        max_positions_for_source_account=1,
    )
    assert not decision.approved
    assert decision.reason == "source_account_exposure_limit_reached"


def test_max_positions_for_source_account_override_can_be_more_permissive():
    # A higher per-account override must also be respected, not just a
    # tighter one -- confirms the override genuinely replaces the config
    # default rather than only ever making the check stricter.
    config = dict(BASE_CONFIG)
    config["max_open_positions_per_source_account"] = 1
    config["max_open_positions"] = 5
    rm = _risk_manager(config)
    positions = [OpenPositionInfo("GBP_USD", "waltervannelli", 0.25)]
    decision = rm.evaluate_trade(
        "EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(open_positions=positions), NOW,
        max_positions_for_source_account=2,
    )
    assert decision.approved


def test_cooldown_after_loss_blocks_same_instrument():
    rm = _risk_manager()
    rm.record_trade_closed("EUR_USD", pnl=-10.0, now=NOW)
    later = NOW + timedelta(minutes=5)
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(), later)
    assert not decision.approved
    assert decision.reason == "cooldown_after_loss_active"


def test_consecutive_losses_trip_circuit_breaker():
    config = dict(BASE_CONFIG)
    config["max_consecutive_losses"] = 2
    config["cooldown_after_loss_minutes"] = 0
    rm = _risk_manager(config)
    rm.record_trade_closed("EUR_USD", pnl=-10.0, now=NOW)
    rm.record_trade_closed("GBP_USD", pnl=-10.0, now=NOW)
    later = NOW + timedelta(minutes=1)
    decision = rm.evaluate_trade("USD_JPY", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(), later)
    assert not decision.approved
    assert decision.reason == "consecutive_loss_cooldown_triggered"


def test_max_trades_per_hour():
    config = dict(BASE_CONFIG)
    config["max_trades_per_hour"] = 1
    rm = _risk_manager(config)
    rm.record_trade_opened(NOW)
    later = NOW + timedelta(minutes=1)
    decision = rm.evaluate_trade("GBP_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(), later)
    assert not decision.approved
    assert decision.reason == "max_trades_per_hour_reached"


def test_circuit_breaker_blocks_everything():
    from app.risk.circuit_breaker import TripReason

    rm = _risk_manager()
    rm.circuit_breaker.trip(TripReason.MANUAL, NOW, cooldown=None)
    decision = rm.evaluate_trade("EUR_USD", "waltervannelli", 0.0010, 0.0020, 1.0, 1.0850, _account_state(), NOW)
    assert not decision.approved
    assert "circuit_breaker_tripped" in decision.reason


def test_api_error_rate_trips_breaker():
    config = dict(BASE_CONFIG)
    config["api_failure_circuit_breaker_count"] = 3
    rm = _risk_manager(config)
    tripped = False
    for _ in range(3):
        tripped = rm.record_api_error(NOW)
    assert tripped
    assert rm.circuit_breaker.is_tripped(NOW)


def test_order_rejection_rate_trips_breaker():
    rm = _risk_manager()
    tripped = False
    for _ in range(3):
        tripped = rm.record_order_rejection(NOW)
    assert tripped
    assert rm.circuit_breaker.is_tripped(NOW)


def test_active_instrument_cooldowns_reports_instrument_still_cooling_down():
    rm = _risk_manager()
    rm.record_trade_closed("EUR_USD", pnl=-10.0, now=NOW)
    later = NOW + timedelta(minutes=5)
    cooldowns = rm.active_instrument_cooldowns(later)
    assert len(cooldowns) == 1
    assert cooldowns[0]["instrument"] == "EUR_USD"
    assert cooldowns[0]["ends_at"] == NOW + timedelta(minutes=BASE_CONFIG["cooldown_after_loss_minutes"])


def test_active_instrument_cooldowns_excludes_expired_and_wins():
    rm = _risk_manager()
    rm.record_trade_closed("EUR_USD", pnl=-10.0, now=NOW)
    rm.record_trade_closed("GBP_USD", pnl=5.0, now=NOW)  # a win never starts a cooldown
    after_expiry = NOW + timedelta(minutes=BASE_CONFIG["cooldown_after_loss_minutes"] + 1)
    assert rm.active_instrument_cooldowns(after_expiry) == []


def test_margin_cap_clamps_position_size():
    config = dict(BASE_CONFIG)
    config["risk_per_trade_percent"] = 5
    config["max_exposure_per_instrument_percent"] = 100
    config["max_exposure_per_source_account_percent"] = 100
    config["max_total_exposure_percent"] = 100
    config["max_position_margin_percent"] = 8
    rm = _risk_manager(config)
    decision = rm.evaluate_trade(
        "EUR_USD", "waltervannelli", 0.002, 0.003, 1.0, 1.14239, _account_state(equity=10_576, balance=10_576),
        NOW, margin_rate=0.0333,
    )
    assert decision.approved
    assert decision.position_size.margin_capped
    implied_margin = decision.position_size.units * 1.14239 * 0.0333
    assert implied_margin <= 10_576 * 0.08 + 1e-6
