"""Phase 6: deterministic, content-level validation of an extracted signal.

Split of responsibility from risk_manager.py: this module validates that
the *signal itself* makes sense (fresh enough, instrument maps to
something OANDA actually offers on this account, direction/stop/target are
logically placed, no duplicate or conflicting position) -- account-level
numeric thresholds (position sizing, exposure limits, minimum R:R, max
spread) live in risk_manager.py so those thresholds are defined in exactly
one place. A signal must pass both to result in an order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.broker.base_broker import BaseBroker
from app.nlp.schemas import Direction, SignalType, TradeSignal
from app.storage.repository import Repository


@dataclass(frozen=True)
class ValidationResult:
    approved: bool
    reason: str | None
    oanda_instrument: str | None = None
    stop_distance_price: float | None = None
    target_distance_price: float | None = None
    entry_reference_price: float | None = None
    current_spread_pips: float | None = None
    effective_stop_loss_price: float | None = None
    effective_take_profit_price: float | None = None
    stop_loss_is_fallback: bool = False
    take_profit_is_fallback: bool = False
    margin_rate: float | None = None


class SignalValidator:
    def __init__(
        self,
        repository: Repository,
        broker: BaseBroker,
        instrument_aliases: dict[str, str],
        known_unsupported: set[str],
        max_signal_age_minutes: int,
        max_entry_distance_percent: float = 0.5,
        missing_stop_loss_behavior: str = "human_review",
        fallback_stop_distance_pips: dict[str, float] | None = None,
        fallback_reward_to_risk_multiple: float = 1.5,
    ):
        self.repository = repository
        self.broker = broker
        self.instrument_aliases = instrument_aliases
        self.known_unsupported = known_unsupported
        self.max_signal_age_minutes = max_signal_age_minutes
        self.max_entry_distance_percent = max_entry_distance_percent
        self.missing_stop_loss_behavior = missing_stop_loss_behavior
        self.fallback_stop_distance_pips = fallback_stop_distance_pips or {}
        self.fallback_reward_to_risk_multiple = fallback_reward_to_risk_multiple

    def validate(self, signal: TradeSignal, post_posted_at: datetime, now: datetime) -> ValidationResult:
        age_minutes = (now - post_posted_at).total_seconds() / 60.0
        if age_minutes > self.max_signal_age_minutes:
            return self._reject(f"signal_too_stale:{age_minutes:.1f}min")

        if signal.valid_until is not None and now > signal.valid_until:
            return self._reject("signal_expired_per_author_stated_validity")

        if signal.signal_type in (SignalType.OBSERVATION, SignalType.NO_TRADE):
            return self._reject("not_actionable")

        if signal.instrument is None:
            return self._reject("missing_instrument")

        alias_key = signal.instrument.strip().lower().lstrip("$")
        if alias_key in self.known_unsupported:
            return self._reject(f"known_unsupported_instrument:{alias_key}")

        oanda_instrument = self.instrument_aliases.get(alias_key)
        if oanda_instrument is None:
            return self._reject(f"unmapped_instrument:{signal.instrument}")

        metadata = self.broker.get_instrument_metadata(oanda_instrument)
        if metadata is None:
            return self._reject(f"instrument_unavailable_on_account:{oanda_instrument}")

        if signal.signal_type == SignalType.NEW_TRADE and signal.direction is None:
            return self._reject("missing_direction")

        tick = self.broker.get_pricing(oanda_instrument)
        if tick is None:
            return self._reject("market_data_unavailable")
        if not tick.tradeable:
            return self._reject("market_closed")
        if tick.ask <= tick.bid:
            return self._reject("inconsistent_bid_ask")

        current_price = tick.ask if signal.direction == Direction.LONG else tick.bid
        spread_pips = (tick.ask - tick.bid) / metadata.pip_size

        reference_entry = signal.entry_price
        if reference_entry is None and signal.entry_zone_low is not None and signal.entry_zone_high is not None:
            reference_entry = (signal.entry_zone_low + signal.entry_zone_high) / 2
        if reference_entry is None:
            reference_entry = current_price  # market-order style signal with no stated entry level

        distance_percent = abs(current_price - reference_entry) / reference_entry * 100.0
        if distance_percent > self.max_entry_distance_percent:
            return self._reject(f"entry_too_far_from_market:{distance_percent:.2f}%")

        effective_stop_loss = signal.stop_loss
        stop_loss_is_fallback = False
        if signal.signal_type == SignalType.NEW_TRADE and effective_stop_loss is None:
            if self.missing_stop_loss_behavior != "apply_risk_model":
                return self._reject("missing_stop_loss_requires_human_review")
            distance_pips = self.fallback_stop_distance_pips.get(oanda_instrument)
            if distance_pips is None:
                return self._reject(f"no_fallback_stop_distance_configured_for:{oanda_instrument}")
            distance_price = distance_pips * metadata.pip_size
            effective_stop_loss = current_price - distance_price if signal.direction == Direction.LONG else current_price + distance_price
            stop_loss_is_fallback = True

        if effective_stop_loss is not None:
            if signal.direction == Direction.LONG and effective_stop_loss >= current_price:
                return self._reject("stop_loss_wrong_side_for_long")
            if signal.direction == Direction.SHORT and effective_stop_loss <= current_price:
                return self._reject("stop_loss_wrong_side_for_short")

        for tp in signal.take_profit:
            if signal.direction == Direction.LONG and tp <= current_price:
                return self._reject("take_profit_wrong_side_for_long")
            if signal.direction == Direction.SHORT and tp >= current_price:
                return self._reject("take_profit_wrong_side_for_short")

        effective_take_profit = list(signal.take_profit)
        take_profit_is_fallback = False
        if (
            not effective_take_profit
            and self.missing_stop_loss_behavior == "apply_risk_model"
            and effective_stop_loss is not None
            and signal.signal_type == SignalType.NEW_TRADE
        ):
            stop_dist = abs(current_price - effective_stop_loss)
            target_dist = stop_dist * self.fallback_reward_to_risk_multiple
            fallback_tp = current_price + target_dist if signal.direction == Direction.LONG else current_price - target_dist
            effective_take_profit = [fallback_tp]
            take_profit_is_fallback = True

        stop_distance_price = abs(current_price - effective_stop_loss) if effective_stop_loss is not None else None
        target_distance_price = abs(effective_take_profit[0] - current_price) if effective_take_profit else None

        # Duplicate-post prevention already happened at the raw_posts stage
        # (post_id is a primary key -- execution_engine.process_post returns
        # "duplicate_post" before this validator ever runs a second time for
        # the same post). Duplicate-*order* prevention happens in
        # order_manager.py, keyed by post_id+instrument. A "has a signal row
        # already been written for this post_id" check does NOT belong here:
        # execution_engine inserts the signal row for this very attempt
        # before calling validate(), so that check would always find itself.

        if signal.signal_type == SignalType.NEW_TRADE:
            open_trades = self.repository.get_open_trades_for_instrument(oanda_instrument)
            for trade in open_trades:
                if trade["direction"] != signal.direction.value:
                    return self._reject("conflicting_open_position_opposite_direction")

        return ValidationResult(
            approved=True,
            reason=None,
            oanda_instrument=oanda_instrument,
            stop_distance_price=stop_distance_price,
            target_distance_price=target_distance_price,
            entry_reference_price=current_price,
            current_spread_pips=spread_pips,
            effective_stop_loss_price=effective_stop_loss,
            effective_take_profit_price=effective_take_profit[0] if effective_take_profit else None,
            stop_loss_is_fallback=stop_loss_is_fallback,
            take_profit_is_fallback=take_profit_is_fallback,
            margin_rate=metadata.margin_rate,
        )

    @staticmethod
    def _reject(reason: str) -> ValidationResult:
        return ValidationResult(approved=False, reason=reason)
