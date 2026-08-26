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


def test_repeated_manual_clear_trips_do_not_stack_up():
    # Regression: callers fire from polling loops -- the reconciler re-checks
    # every 30s and record_api_error re-trips on every failed poll -- so an
    # unresolved condition appended a fresh identical event each pass. Seen
    # live as 40 identical reconciliation_mismatch entries in 20 minutes,
    # all describing one problem.
    cb = CircuitBreaker()
    for minute in range(5):
        cb.trip(TripReason.RECONCILIATION_MISMATCH, NOW + timedelta(minutes=minute), cooldown=None, details="trade 648")

    assert len(cb.active_events(NOW + timedelta(minutes=5))) == 1
    assert cb.active_reasons(NOW + timedelta(minutes=5)) == [TripReason.RECONCILIATION_MISMATCH]
    # tripped_at must still report when the condition actually began, not
    # whenever the loop last happened to notice it.
    assert cb.active_events(NOW + timedelta(minutes=5))[0].tripped_at == NOW


def test_distinct_reasons_still_stack_independently():
    cb = CircuitBreaker()
    cb.trip(TripReason.RECONCILIATION_MISMATCH, NOW, cooldown=None)
    cb.trip(TripReason.API_ERROR_RATE, NOW, cooldown=None)

    assert len(cb.active_events(NOW)) == 2
    cb.manual_clear(TripReason.RECONCILIATION_MISMATCH)
    assert cb.active_reasons(NOW) == [TripReason.API_ERROR_RATE]


def test_cooldown_trips_still_re_trip_to_extend_the_window():
    # Deliberate exception to the dedupe: a cooldown-bearing trip re-tripping
    # is how repeated consecutive losses push the window further out.
    cb = CircuitBreaker()
    cb.trip(TripReason.CONSECUTIVE_LOSSES, NOW, cooldown=timedelta(minutes=10))
    cb.trip(TripReason.CONSECUTIVE_LOSSES, NOW + timedelta(minutes=5), cooldown=timedelta(minutes=10))

    assert cb.is_tripped(NOW + timedelta(minutes=12))


def test_re_trips_after_a_manual_clear_if_condition_persists():
    cb = CircuitBreaker()
    cb.trip(TripReason.RECONCILIATION_MISMATCH, NOW, cooldown=None, details="trade 648")
    cb.manual_clear()
    assert not cb.is_tripped(NOW)

    later = NOW + timedelta(seconds=30)
    cb.trip(TripReason.RECONCILIATION_MISMATCH, later, cooldown=None, details="trade 648")
    assert cb.is_tripped(later)
    assert cb.active_events(later)[0].tripped_at == later
