"""Central, signal-independent risk manager (Phase 7). Every proposed
trade passes through `evaluate_trade` before any order is constructed --
the NLP pipeline has no way to bypass this, and nothing here ever loosens
a limit after a loss (no martingale, no revenge-sizing).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.risk.circuit_breaker import CircuitBreaker, TripReason
from app.risk.position_sizing import PositionSizeResult, calculate_position_size


@dataclass(frozen=True)
class OpenPositionInfo:
    instrument: str
    source_account: str | None
    risk_amount_pct: float


@dataclass(frozen=True)
class AccountState:
    equity: float
    balance: float
    open_positions: list[OpenPositionInfo]
    daily_starting_equity: float
    weekly_starting_equity: float


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str | None
    position_size: PositionSizeResult | None = None


class RiskManager:
    def __init__(self, config: dict, circuit_breaker: CircuitBreaker):
        self.config = config
        self.circuit_breaker = circuit_breaker
        self._trade_timestamps: list[datetime] = []
        self._consecutive_losses = 0
        self._last_loss_time_by_instrument: dict[str, datetime] = {}
        self._api_error_timestamps: list[datetime] = []
        self._consecutive_order_rejections = 0

    def evaluate_trade(
        self,
        instrument: str,
        source_account: str,
        stop_distance_price: float,
        target_distance_price: float,
        current_spread_pips: float,
        price: float,
        account_state: AccountState,
        now: datetime,
        margin_rate: float | None = None,
        max_positions_for_source_account: int | None = None,
    ) -> RiskDecision:
        if self.circuit_breaker.is_tripped(now):
            reasons = ",".join(r.value for r in self.circuit_breaker.active_reasons(now))
            return RiskDecision(False, f"circuit_breaker_tripped:{reasons}")

        if stop_distance_price <= 0:
            return RiskDecision(False, "missing_or_invalid_stop_loss")  # mandatory-stop-loss enforcement

        reward_risk = target_distance_price / stop_distance_price if stop_distance_price > 0 else 0.0
        # Tiny epsilon absorbs float rounding dust (e.g. a fallback target
        # computed as exactly stop_distance * minimum_reward_to_risk can
        # legitimately land a hair under the floor purely from floating-point
        # arithmetic, e.g. 1.4999999999999998 instead of 1.5) -- without
        # this, a trade constructed to exactly clear the floor could be
        # silently rejected by representation error, not a real shortfall.
        if reward_risk < self.config["minimum_reward_to_risk"] - 1e-9:
            return RiskDecision(False, "reward_risk_below_minimum")

        max_spread = self.config["max_spread_pips"].get(instrument)
        if max_spread is not None and current_spread_pips > max_spread:
            return RiskDecision(False, "spread_too_wide")

        daily_pl_pct = self._pct_change(account_state.daily_starting_equity, account_state.equity)
        if daily_pl_pct <= -self.config["max_daily_loss_percent"]:
            self.circuit_breaker.trip(TripReason.DAILY_LOSS_LIMIT, now, cooldown=None, details=f"{daily_pl_pct:.2f}%")
            return RiskDecision(False, "daily_loss_limit_reached")

        weekly_pl_pct = self._pct_change(account_state.weekly_starting_equity, account_state.equity)
        if weekly_pl_pct <= -self.config["max_weekly_loss_percent"]:
            self.circuit_breaker.trip(TripReason.WEEKLY_LOSS_LIMIT, now, cooldown=None, details=f"{weekly_pl_pct:.2f}%")
            return RiskDecision(False, "weekly_loss_limit_reached")

        if len(account_state.open_positions) >= self.config["max_open_positions"]:
            return RiskDecision(False, "max_open_positions_reached")

        same_source_count = sum(1 for p in account_state.open_positions if p.source_account == source_account)
        max_per_source = (
            max_positions_for_source_account
            if max_positions_for_source_account is not None
            else self.config.get("max_open_positions_per_source_account", 2)
        )
        if same_source_count >= max_per_source:
            return RiskDecision(False, "source_account_exposure_limit_reached")

        instrument_exposure_pct = sum(
            p.risk_amount_pct for p in account_state.open_positions if p.instrument == instrument
        )
        if instrument_exposure_pct + self.config["risk_per_trade_percent"] > self.config["max_exposure_per_instrument_percent"]:
            return RiskDecision(False, "instrument_exposure_limit_reached")

        source_exposure_pct = sum(
            p.risk_amount_pct for p in account_state.open_positions if p.source_account == source_account
        )
        if source_exposure_pct + self.config["risk_per_trade_percent"] > self.config["max_exposure_per_source_account_percent"]:
            return RiskDecision(False, "source_account_risk_limit_reached")

        total_exposure_pct = sum(p.risk_amount_pct for p in account_state.open_positions)
        if total_exposure_pct + self.config["risk_per_trade_percent"] > self.config["max_total_exposure_percent"]:
            return RiskDecision(False, "max_total_exposure_reached")

        last_loss = self._last_loss_time_by_instrument.get(instrument)
        if last_loss is not None:
            cooldown_end = last_loss + timedelta(minutes=self.config["cooldown_after_loss_minutes"])
            if now < cooldown_end:
                return RiskDecision(False, "cooldown_after_loss_active")

        if self._consecutive_losses >= self.config["max_consecutive_losses"]:
            self.circuit_breaker.trip(
                TripReason.CONSECUTIVE_LOSSES, now,
                cooldown=timedelta(minutes=self.config.get("cooldown_after_consecutive_losses_minutes", 120)),
                details=f"{self._consecutive_losses} consecutive losses",
            )
            return RiskDecision(False, "consecutive_loss_cooldown_triggered")

        self._prune_trade_timestamps(now)
        trades_last_hour = sum(1 for t in self._trade_timestamps if now - t <= timedelta(hours=1))
        if trades_last_hour >= self.config["max_trades_per_hour"]:
            return RiskDecision(False, "max_trades_per_hour_reached")
        trades_today = sum(1 for t in self._trade_timestamps if t.date() == now.date())
        if trades_today >= self.config["max_trades_per_day"]:
            return RiskDecision(False, "max_trades_per_day_reached")

        position_size = calculate_position_size(
            equity=account_state.equity,
            risk_per_trade_percent=self.config["risk_per_trade_percent"],
            stop_distance_price=stop_distance_price,
            price=price,
            margin_rate=margin_rate,
            max_margin_percent_of_equity=self.config.get("max_position_margin_percent"),
        )
        if not position_size.is_valid:
            return RiskDecision(False, f"position_sizing_rejected:{position_size.rejected_reason}", position_size)

        return RiskDecision(True, None, position_size)

    # ---- state updates ------------------------------------------------------------

    def record_trade_opened(self, now: datetime) -> None:
        self._trade_timestamps.append(now)

    def record_trade_closed(self, instrument: str, pnl: float, now: datetime) -> None:
        if pnl < 0:
            self._consecutive_losses += 1
            self._last_loss_time_by_instrument[instrument] = now
        else:
            self._consecutive_losses = 0

    def record_api_error(self, now: datetime) -> bool:
        self._api_error_timestamps.append(now)
        window = timedelta(minutes=self.config.get("cooldown_after_api_failures_minutes", 30))
        recent = [t for t in self._api_error_timestamps if now - t <= window]
        self._api_error_timestamps = recent
        threshold = self.config.get("api_failure_circuit_breaker_count", 5)
        if len(recent) >= threshold:
            self.circuit_breaker.trip(TripReason.API_ERROR_RATE, now, cooldown=None, details=f"{len(recent)} API errors")
            return True
        return False

    def record_order_rejection(self, now: datetime) -> bool:
        self._consecutive_order_rejections += 1
        if self._consecutive_order_rejections >= 3:
            self.circuit_breaker.trip(
                TripReason.ORDER_REJECTION_RATE, now, cooldown=None,
                details=f"{self._consecutive_order_rejections} consecutive rejections",
            )
            return True
        return False

    def record_order_accepted(self) -> None:
        self._consecutive_order_rejections = 0

    def record_reconciliation_mismatch(self, now: datetime, details: str) -> None:
        self.circuit_breaker.trip(TripReason.RECONCILIATION_MISMATCH, now, cooldown=None, details=details)

    def active_instrument_cooldowns(self, now: datetime) -> list[dict]:
        """Per-instrument cooldown-after-loss windows still in effect --
        separate from the circuit breaker, since this only blocks the
        specific instrument that just lost, not the whole account."""
        cooldown = timedelta(minutes=self.config["cooldown_after_loss_minutes"])
        active = []
        for instrument, last_loss in self._last_loss_time_by_instrument.items():
            ends_at = last_loss + cooldown
            if now < ends_at:
                active.append({"instrument": instrument, "started_at": last_loss, "ends_at": ends_at})
        return active

    def _prune_trade_timestamps(self, now: datetime) -> None:
        cutoff = now - timedelta(days=1)
        self._trade_timestamps = [t for t in self._trade_timestamps if t >= cutoff]

    @staticmethod
    def _pct_change(start: float, current: float) -> float:
        if start <= 0:
            return 0.0
        return (current - start) / start * 100.0
