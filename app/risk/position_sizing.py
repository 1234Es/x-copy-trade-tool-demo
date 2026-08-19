"""Position sizing: the only place trade size is computed. Always derived
from (equity, risk %, stop distance) -- never a fixed size, never
increased after a loss, no martingale.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSizeResult:
    units: int
    risk_amount: float
    rejected_reason: str | None = None
    margin_capped: bool = False

    @property
    def is_valid(self) -> bool:
        return self.rejected_reason is None and self.units > 0


def calculate_position_size(
    equity: float,
    risk_per_trade_percent: float,
    stop_distance_price: float,
    price: float,
    min_units: int = 1,
    max_units: int | None = None,
    margin_rate: float | None = None,
    max_margin_percent_of_equity: float | None = None,
) -> PositionSizeResult:
    if equity <= 0:
        return PositionSizeResult(0, 0.0, "non_positive_equity")
    if stop_distance_price <= 0:
        return PositionSizeResult(0, 0.0, "non_positive_stop_distance")
    if stop_distance_price >= price:
        return PositionSizeResult(0, 0.0, "stop_distance_exceeds_price")

    risk_amount = equity * (risk_per_trade_percent / 100.0)
    units = int(risk_amount / stop_distance_price)

    if units < min_units:
        return PositionSizeResult(0, risk_amount, "below_minimum_trade_size")
    if max_units is not None and units > max_units:
        units = max_units

    margin_capped = False
    # A tight stop makes risk-based sizing alone produce an arbitrarily large,
    # highly leveraged position for the same dollar risk -- this cap bounds
    # margin used by *this* position independent of that math, clamping units
    # down (and so realized risk below risk_per_trade_percent) rather than
    # rejecting the trade outright.
    if margin_rate is not None and max_margin_percent_of_equity is not None and margin_rate > 0:
        max_margin_allowed = equity * (max_margin_percent_of_equity / 100.0)
        implied_margin = units * price * margin_rate
        if implied_margin > max_margin_allowed:
            units = int(max_margin_allowed / (price * margin_rate))
            margin_capped = True
            if units < min_units:
                return PositionSizeResult(0, risk_amount, "margin_cap_below_minimum_trade_size", margin_capped=True)

    return PositionSizeResult(units, risk_amount, None, margin_capped=margin_capped)
