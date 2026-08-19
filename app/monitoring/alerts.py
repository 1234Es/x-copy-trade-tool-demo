"""Pluggable alert delivery -- logs by default, optionally also POSTs to a
webhook. Delivery failures are swallowed (logged, not raised): a broken
alert channel must never be able to crash the pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import requests

from app.monitoring.logging import get_logger

log = get_logger("alerts")


class AlertSink(Protocol):
    def send(self, event_type: str, message: str, context: dict[str, Any]) -> None: ...


class CompositeAlertSink:
    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url or None

    def send(self, event_type: str, message: str, context: dict[str, Any]) -> None:
        payload = {
            "event_type": event_type,
            "message": message,
            "context": context,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        log.info("alert", **payload)
        if self.webhook_url:
            try:
                requests.post(self.webhook_url, json=payload, timeout=5)
            except requests.RequestException as exc:
                log.warning("alert_delivery_failed", error=str(exc), event_type=event_type)
