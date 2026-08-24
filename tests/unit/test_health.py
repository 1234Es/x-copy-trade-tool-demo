from datetime import datetime, timezone

from app.monitoring.health import XPollState, check_x_api


def test_credits_depleted_reports_disconnected_distinctly():
    state = XPollState(credits_depleted=True, last_error="X API error 402: credits depleted")
    status = check_x_api("token", state)
    assert not status.connected
    assert "credits depleted" in status.detail.lower()


def test_polling_failure_without_prior_success_reports_disconnected():
    state = XPollState(last_error="connection reset")
    status = check_x_api("token", state)
    assert not status.connected
    assert "connection reset" in status.detail


def test_successful_poll_reports_connected():
    state = XPollState(last_success_at=datetime.now(timezone.utc))
    status = check_x_api("token", state)
    assert status.connected


def test_no_poll_state_falls_back_to_token_presence():
    # Regression: check_x_api() used to only confirm the token was a
    # non-empty string -- this preserves that behavior for any caller that
    # doesn't have a live XPollState to pass (or hasn't polled yet).
    status = check_x_api("token")
    assert status.connected


def test_no_token_reports_not_configured_regardless_of_poll_state():
    status = check_x_api("", XPollState(credits_depleted=True))
    assert not status.connected
    assert "not configured" in status.detail
