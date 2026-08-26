"""Independent circuit breaker, including a manual emergency kill switch.
A trip only ever restricts further trading -- it never adjusts risk
upward and nothing here can auto-clear a manual kill-switch trip.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class TripReason(str, Enum):
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    WEEKLY_LOSS_LIMIT = "weekly_loss_limit"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    API_ERROR_RATE = "api_error_rate"
    ORDER_REJECTION_RATE = "order_rejection_rate"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    KILL_SWITCH = "kill_switch"
    MANUAL = "manual"


@dataclass
class CircuitBreakerEvent:
    reason: TripReason
    tripped_at: datetime
    clears_at: datetime | None
    details: str = ""


class CircuitBreaker:
    def __init__(self) -> None:
        self._active_trips: list[CircuitBreakerEvent] = []
        self._history: list[CircuitBreakerEvent] = []

    def trip(self, reason: TripReason, now: datetime, cooldown: timedelta | None, details: str = "") -> CircuitBreakerEvent:
        self._expire(now)
        if cooldown is None:
            # A manual-clear trip is a latch, not a counter: while one is
            # already active for this reason, tripping again changes nothing
            # about whether trading is halted. Callers fire repeatedly from
            # polling loops (the reconciler re-checks every 30s, and
            # record_api_error re-trips on every failed poll), so without
            # this the active list grew unboundedly -- 40 identical
            # reconciliation_mismatch entries in 20 minutes, all of one
            # underlying problem, bloating /api/status and burying the
            # original trip time. The first event is returned unchanged so
            # "tripped_at" keeps saying when the condition ACTUALLY started.
            # Cooldown-bearing trips are exempt: re-tripping those
            # deliberately extends the window (see consecutive-losses).
            for existing in self._active_trips:
                if existing.reason == reason and existing.clears_at is None:
                    return existing
        clears_at = now + cooldown if cooldown is not None else None
        event = CircuitBreakerEvent(reason=reason, tripped_at=now, clears_at=clears_at, details=details)
        self._active_trips.append(event)
        self._history.append(event)
        return event

    def kill_switch(self, now: datetime, details: str = "manual emergency stop") -> CircuitBreakerEvent:
        """Requires manual clearing -- no cooldown, no automatic recovery."""
        return self.trip(TripReason.KILL_SWITCH, now, cooldown=None, details=details)

    def is_tripped(self, now: datetime) -> bool:
        self._expire(now)
        return len(self._active_trips) > 0

    def active_reasons(self, now: datetime) -> list[TripReason]:
        self._expire(now)
        return [e.reason for e in self._active_trips]

    def active_events(self, now: datetime) -> list[CircuitBreakerEvent]:
        self._expire(now)
        return list(self._active_trips)

    def manual_clear(self, reason: TripReason | None = None) -> None:
        if reason is None:
            self._active_trips.clear()
        else:
            self._active_trips = [e for e in self._active_trips if e.reason != reason]

    def _expire(self, now: datetime) -> None:
        self._active_trips = [
            e for e in self._active_trips if not (e.clears_at is not None and now >= e.clears_at)
        ]

    @property
    def history(self) -> list[CircuitBreakerEvent]:
        return list(self._history)
