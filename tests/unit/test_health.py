from datetime import datetime, timedelta, timezone

from app.monitoring.health import STALE_POLL_AFTER_SECONDS, XPollState, check_x_api

NOW = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)


def test_credits_depleted_reports_disconnected_distinctly():
    state = XPollState(credits_depleted=True, last_error="X API error 402: credits depleted")
    status = check_x_api("token", state, now=NOW)
    assert not status.connected
    assert "credits depleted" in status.detail.lower()


def test_no_poll_has_ever_succeeded_reports_disconnected():
    state = XPollState(last_error="connection reset")
    status = check_x_api("token", state, now=NOW)
    assert not status.connected
    assert "connection reset" in status.detail


def test_recent_successful_poll_reports_connected():
    state = XPollState(last_success_at=NOW - timedelta(seconds=30))
    status = check_x_api("token", state, now=NOW)
    assert status.connected
    assert "polling normally" in status.detail


def test_stale_poll_reports_disconnected_even_after_past_successes():
    # Regression: the check only reported trouble when NO poll had ever
    # succeeded, so a silently dead poll thread kept reporting "connected"
    # forever. Ingestion was dead from 2026-09-11 to 2026-09-13 with the
    # dashboard showing x_api healthy the whole time.
    state = XPollState(last_success_at=NOW - timedelta(days=2))
    status = check_x_api("token", state, now=NOW)
    assert not status.connected
    assert "stopped" in status.detail
    assert "2880 min" in status.detail


def test_staleness_boundary_is_not_tripped_just_between_cycles():
    # Polling runs every 60s; being a few minutes late must not read as dead.
    state = XPollState(last_success_at=NOW - timedelta(seconds=STALE_POLL_AFTER_SECONDS - 1))
    assert check_x_api("token", state, now=NOW).connected


def test_recent_success_with_a_transient_error_still_reports_connected():
    state = XPollState(last_success_at=NOW - timedelta(seconds=30), last_error="timeout on one account")
    status = check_x_api("token", state, now=NOW)
    assert status.connected
    assert "timeout on one account" in status.detail


def test_no_poll_state_falls_back_to_token_presence():
    # Preserved for any caller without a live XPollState to pass.
    assert check_x_api("token").connected


def test_no_token_reports_not_configured_regardless_of_poll_state():
    status = check_x_api("", XPollState(credits_depleted=True), now=NOW)
    assert not status.connected
    assert "not configured" in status.detail
