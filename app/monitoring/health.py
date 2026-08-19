"""Connection-status checks for the three external dependencies, surfaced
on the dashboard (Phase 13). Each check is independent and swallows its
own errors into a status string -- a dead OpenAI key must not prevent the
dashboard from reporting that OANDA is fine.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.broker.base_broker import BaseBroker
from app.monitoring.logging import get_logger

log = get_logger("health")


@dataclass(frozen=True)
class ConnectionStatus:
    name: str
    connected: bool
    detail: str


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


def check_x_api(bearer_token: str) -> ConnectionStatus:
    if not bearer_token:
        return ConnectionStatus(
            "x_api", False, "not configured -- using manual/webhook input adapter by default (this is expected, not an error)"
        )
    return ConnectionStatus("x_api", True, "bearer token configured")
