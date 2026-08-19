from app.auth import LOCKOUT_WINDOW_SECONDS, MAX_FAILED_ATTEMPTS, SESSION_TTL_SECONDS, AuthState


def test_create_session_is_valid():
    state = AuthState()
    token = state.create_session()

    assert state.is_valid(token)


def test_is_valid_rejects_unknown_token():
    state = AuthState()

    assert not state.is_valid("not-a-real-token")


def test_is_valid_rejects_missing_token():
    state = AuthState()

    assert not state.is_valid(None)


def test_is_valid_rejects_expired_session(monkeypatch):
    state = AuthState()
    token = state.create_session()

    real_time = __import__("time").time

    def frozen_later():
        return real_time() + SESSION_TTL_SECONDS + 1

    monkeypatch.setattr("app.auth.time.time", frozen_later)

    assert not state.is_valid(token)
    assert token not in state.sessions  # expired sessions are pruned on check


def test_invalidate_clears_session():
    state = AuthState()
    token = state.create_session()

    state.invalidate(token)

    assert not state.is_valid(token)


def test_invalidate_unknown_token_is_a_no_op():
    state = AuthState()

    state.invalidate("nosuchtoken")  # must not raise


def test_lockout_after_max_failed_attempts():
    state = AuthState()
    now = 1_000_000.0

    for _ in range(MAX_FAILED_ATTEMPTS):
        assert not state.is_locked_out("1.2.3.4", now)
        state.record_failed_attempt("1.2.3.4", now)

    assert state.is_locked_out("1.2.3.4", now)


def test_lockout_is_per_ip():
    state = AuthState()
    now = 1_000_000.0

    for _ in range(MAX_FAILED_ATTEMPTS):
        state.record_failed_attempt("1.2.3.4", now)

    assert state.is_locked_out("1.2.3.4", now)
    assert not state.is_locked_out("5.6.7.8", now)


def test_lockout_expires_after_window():
    state = AuthState()
    now = 1_000_000.0

    for _ in range(MAX_FAILED_ATTEMPTS):
        state.record_failed_attempt("1.2.3.4", now)
    assert state.is_locked_out("1.2.3.4", now)

    later = now + LOCKOUT_WINDOW_SECONDS + 1
    assert not state.is_locked_out("1.2.3.4", later)
