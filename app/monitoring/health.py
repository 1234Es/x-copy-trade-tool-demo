"""Connection-status checks for the three external dependencies, surfaced
on the dashboard (Phase 13). Each check is independent and swallows its
own errors into a status string -- a dead OpenAI key must not prevent the
dashboard from reporting that OANDA is fine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.broker.base_broker import BaseBroker
from app.monitoring.logging import get_logger

log = get_logger("health")


@dataclass(frozen=True)
class ConnectionStatus:
    name: str
    connected: bool
    detail: str


@dataclass
class XPollState:
    """Mutable, shared between main.py's background X-polling loop (the
    writer) and the /api/status route (the reader) -- check_x_api() used to
    only confirm X_BEARER_TOKEN was a non-empty string, which reported
    "connected" even while every poll was failing (e.g. the X API account's
    pay-per-use credits ran out, HTTP 402) since a live call was
    deliberately never made from a health check (see check_openai's same
    reasoning). This tracks the real last-poll outcome instead."""

    credits_depleted: bool = False
    last_error: str | None = None
    last_success_at: datetime | None = None


def check_oanda(broker: BaseBroker | None) -> ConnectionStatus:
    if broker is None:
        return ConnectionStatus("oanda", False, "not configured (missing OANDA_API_TOKEN/OANDA_ACCOUNT_ID)")
    try:
        snapshot = broker.get_account_snapshot()
        return ConnectionStatus("oanda", True, f"connected, equity={snapshot.equity:.2f} {snapshot.currency}")
    except Exception as exc:  # noqa: BLE001 -- health check must never raise
        log.warning("oanda_health_check_failed", error=str(exc))
        return ConnectionStatus("oanda", False, f"error: {exc}")


def check_openai(api_key: str) -> ConnectionStatus:
    if not api_key:
        return ConnectionStatus("openai", False, "not configured (missing OPENAI_API_KEY)")
    # A live round-trip call here would cost money on every dashboard
    # refresh -- we only confirm a key is present. Real connectivity is
    # implicitly proven the moment a classification/extraction call
    # succeeds, which is logged separately.
    return ConnectionStatus("openai", True, "API key configured (connectivity confirmed on first successful call)")


def check_x_api(bearer_token: str, poll_state: XPollState | None = None) -> ConnectionStatus:
    if not bearer_token:
        return ConnectionStatus(
            "x_api", False, "not configured -- using manual/webhook input adapter by default (this is expected, not an error)"
        )
    if poll_state is not None:
        if poll_state.credits_depleted:
            return ConnectionStatus(
                "x_api", False,
                "X API credits depleted (HTTP 402) -- polling has stopped until the account is topped up at developer.x.com",
            )
        if poll_state.last_error and poll_state.last_success_at is None:
            return ConnectionStatus("x_api", False, f"bearer token configured, but polling is failing: {poll_state.last_error}")
    return ConnectionStatus("x_api", True, "bearer token configured")
