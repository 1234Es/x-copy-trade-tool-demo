"""Covers the X polling loop's resilience.

The loop runs as a daemon thread with nothing above it to restart it, so any
exception that escapes kills ingestion for the life of the process while the
HTTP server and reconciliation loop (other threads) carry on looking healthy.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app import main
from app.monitoring.health import XPollState
from app.sources.base_source import Post

NOW = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)


class _StopLoop(Exception):
    """Raised from the patched sleep to break out after one full cycle."""


def _post(post_id: str = "p1") -> Post:
    return Post(post_id=post_id, author="waltervannelli", text="Eur/usd sold 2.5 lots at 1.1653",
                posted_at=NOW, source="x_api")


def _context(posts: list[Post]) -> MagicMock:
    context = MagicMock()
    context.settings.x_bearer_token = "token"
    context.settings.x_api_base_url = "https://api.x.com"
    context.tracked_accounts = [{"username": "waltervannelli", "enabled": True}]
    context.repository.get_last_processed_post_id.return_value = None
    context.x_poll_state = XPollState()
    return context


@pytest.fixture
def patched_loop(monkeypatch):
    """Patches XApiSource and sleep so the loop runs exactly one cycle."""
    def _setup(posts: list[Post]):
        source = MagicMock()
        source.fetch_new_posts_for_username.return_value = posts
        monkeypatch.setattr(main, "XApiSource", lambda **kwargs: source)

        def _stop(_seconds):
            raise _StopLoop()

        monkeypatch.setattr(main.time, "sleep", _stop)
        return _context(posts)

    return _setup


def test_loop_survives_a_post_processing_error(patched_loop):
    # Regression: only the fetch was guarded, so an exception out of
    # process_post escaped `while True` and silently killed the thread.
    # Ingestion died on 2026-09-11 and stayed dead for two days with the
    # dashboard still reporting x_api "connected".
    context = patched_loop([_post()])
    context.execution_engine.process_post.side_effect = RuntimeError("postgres connection closed")

    # Reaching the patched sleep proves the loop completed its cycle rather
    # than dying on the exception.
    with pytest.raises(_StopLoop):
        main._x_polling_loop(context)

    context.alert_sink.send.assert_called_once()
    assert context.alert_sink.send.call_args[0][0] == "post_processing_failed"


def test_cursor_advances_past_a_failing_post(patched_loop):
    # process_post inserts the raw post before anything else, so a retry is a
    # no-op "duplicate_post" -- holding the cursor back would stall every
    # later post behind one that can never succeed.
    context = patched_loop([_post("p-poison")])
    context.execution_engine.process_post.side_effect = RuntimeError("boom")

    with pytest.raises(_StopLoop):
        main._x_polling_loop(context)

    context.repository.set_last_processed_post_id.assert_called_once()
    assert context.repository.set_last_processed_post_id.call_args[0][1] == "p-poison"


def test_one_bad_post_does_not_stop_the_rest_of_the_batch(patched_loop):
    good_first, bad, good_last = _post("p1"), _post("p2"), _post("p3")
    context = patched_loop([good_first, bad, good_last])
    context.execution_engine.process_post.side_effect = [None, RuntimeError("boom"), None]

    with pytest.raises(_StopLoop):
        main._x_polling_loop(context)

    assert context.execution_engine.process_post.call_count == 3
    advanced = [c[0][1] for c in context.repository.set_last_processed_post_id.call_args_list]
    assert advanced == ["p1", "p2", "p3"]


def test_loop_survives_the_database_failing_on_the_cursor_read(patched_loop):
    # Regression: this read sat outside every guard. When the free Postgres
    # instance expired on ~2026-09-25 the first failure here killed the
    # thread, and ingestion stayed dead for three weeks without recovering.
    context = patched_loop([_post()])
    context.repository.get_last_processed_post_id.side_effect = RuntimeError("database does not exist")

    with pytest.raises(_StopLoop):
        main._x_polling_loop(context)

    assert "database does not exist" in context.x_poll_state.last_error
    context.risk_manager.record_api_error.assert_called_once()
    # Nothing was fetched, so nothing can have been processed.
    context.execution_engine.process_post.assert_not_called()


def test_loop_survives_the_database_failing_on_the_cursor_write(patched_loop):
    # The cursor write was exposed the same way as the read. The cycle-level
    # backstop catches it, so this guards the backstop itself rather than
    # one more individually-wrapped call.
    context = patched_loop([_post()])
    context.repository.set_last_processed_post_id.side_effect = RuntimeError("connection reset")

    with pytest.raises(_StopLoop):
        main._x_polling_loop(context)

    assert "connection reset" in context.x_poll_state.last_error
    context.risk_manager.record_api_error.assert_called_once()


def test_successful_cycle_records_poll_health(patched_loop):
    context = patched_loop([_post()])
    context.execution_engine.process_post.side_effect = None

    with pytest.raises(_StopLoop):
        main._x_polling_loop(context)

    # Without this the staleness check in check_x_api has nothing to measure.
    assert context.x_poll_state.last_success_at is not None
    assert context.x_poll_state.last_error is None
    context.alert_sink.send.assert_not_called()
