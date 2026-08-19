from datetime import timedelta

from app.risk.circuit_breaker import CircuitBreaker, TripReason
from tests.fixtures.sample_posts import NOW


def test_trip_and_is_tripped():
    cb = CircuitBreaker()
    cb.trip(TripReason.DAILY_LOSS_LIMIT, NOW, cooldown=None, details="test")
    assert cb.is_tripped(NOW)
    assert TripReason.DAILY_LOSS_LIMIT in cb.active_reasons(NOW)


def test_time_based_trip_expires():
    cb = CircuitBreaker()
    cb.trip(TripReason.CONSECUTIVE_LOSSES, NOW, cooldown=timedelta(minutes=10))
    assert cb.is_tripped(NOW)
    assert not cb.is_tripped(NOW + timedelta(minutes=11))


def test_kill_switch_requires_manual_clear():
    cb = CircuitBreaker()
    cb.kill_switch(NOW)
    later = NOW + timedelta(days=30)
    assert cb.is_tripped(later)
    cb.manual_clear(TripReason.KILL_SWITCH)
    assert not cb.is_tripped(later)


def test_not_tripped_by_default():
    cb = CircuitBreaker()
    assert not cb.is_tripped(NOW)


def test_active_events_includes_clears_at():
    cb = CircuitBreaker()
    cb.trip(TripReason.CONSECUTIVE_LOSSES, NOW, cooldown=timedelta(minutes=10), details="3 losses")
    events = cb.active_events(NOW)
    assert len(events) == 1
    assert events[0].reason == TripReason.CONSECUTIVE_LOSSES
    assert events[0].clears_at == NOW + timedelta(minutes=10)


def test_active_events_excludes_expired():
    cb = CircuitBreaker()
    cb.trip(TripReason.CONSECUTIVE_LOSSES, NOW, cooldown=timedelta(minutes=10))
    assert cb.active_events(NOW + timedelta(minutes=11)) == []
