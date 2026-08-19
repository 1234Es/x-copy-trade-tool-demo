from app.risk.position_sizing import calculate_position_size


def test_basic_sizing():
    result = calculate_position_size(equity=10_000, risk_per_trade_percent=0.25, stop_distance_price=0.0010, price=1.0850)
    assert result.is_valid
    assert result.risk_amount == 25.0
    assert result.units == 25_000


def test_rejects_non_positive_equity():
    result = calculate_position_size(equity=0, risk_per_trade_percent=0.25, stop_distance_price=0.001, price=1.08)
    assert not result.is_valid
    assert result.rejected_reason == "non_positive_equity"


def test_rejects_non_positive_stop_distance():
    result = calculate_position_size(equity=10_000, risk_per_trade_percent=0.25, stop_distance_price=0, price=1.08)
    assert not result.is_valid
    assert result.rejected_reason == "non_positive_stop_distance"


def test_rejects_stop_distance_exceeding_price():
    result = calculate_position_size(equity=10_000, risk_per_trade_percent=0.25, stop_distance_price=2.0, price=1.08)
    assert not result.is_valid
    assert result.rejected_reason == "stop_distance_exceeds_price"


def test_rejects_below_minimum_units():
    result = calculate_position_size(equity=1, risk_per_trade_percent=0.25, stop_distance_price=1.0, price=1.5, min_units=1)
    assert not result.is_valid


def test_caps_at_max_units():
    result = calculate_position_size(equity=10_000_000, risk_per_trade_percent=0.25, stop_distance_price=0.0001, price=1.08, max_units=5_000)
    assert result.is_valid
    assert result.units == 5_000


def test_margin_cap_clamps_units_for_tight_stop():
    # Regression test for the real incident: risk_per_trade_percent=5 with a
    # 20-pip EUR_USD fallback stop produced a ~264k-unit position eating
    # ~71% of equity in margin. A margin cap should clamp units well below
    # the pure risk-based size in this situation.
    uncapped = calculate_position_size(equity=10_576, risk_per_trade_percent=5, stop_distance_price=0.002, price=1.14239)
    assert uncapped.units > 260_000  # confirms the uncapped math still produces the oversized position

    capped = calculate_position_size(
        equity=10_576, risk_per_trade_percent=5, stop_distance_price=0.002, price=1.14239,
        margin_rate=0.0333, max_margin_percent_of_equity=8,
    )
    assert capped.is_valid
    assert capped.margin_capped
    assert capped.units < uncapped.units
    implied_margin = capped.units * 1.14239 * 0.0333
    assert implied_margin <= 10_576 * 0.08 + 1e-6  # +epsilon for float rounding


def test_margin_cap_not_applied_when_not_provided():
    result = calculate_position_size(equity=10_576, risk_per_trade_percent=5, stop_distance_price=0.002, price=1.14239)
    assert not result.margin_capped


def test_margin_cap_rejects_when_clamped_below_minimum():
    result = calculate_position_size(
        equity=100, risk_per_trade_percent=5, stop_distance_price=0.002, price=1.14239,
        margin_rate=0.0333, max_margin_percent_of_equity=0.001, min_units=1,
    )
    assert not result.is_valid
    assert result.rejected_reason == "margin_cap_below_minimum_trade_size"
    assert result.margin_capped
