"""Webhook input adapter -- for an approved third-party feed or a browser
extension that POSTs the currently-viewed post to this local application.

Requests must include the configured shared secret; if none is configured
the webhook route is disabled entirely (see api/server.py), not silently
left open.
"""
from __future__ import annotations

import hmac
from typing import Any

from app.sources.base_source import BaseSource, Post, parse_post_payload


class WebhookAuthError(Exception):
    pass


class WebhookSource(BaseSource):
    name = "webhook"

    def __init__(self, shared_secret: str):
        self._shared_secret = shared_secret
        self._queue: list[Post] = []

    @property
    def enabled(self) -> bool:
        return bool(self._shared_secret)

    def verify_secret(self, provided_secret: str | None) -> bool:
        if not self.enabled:
            return False
        if not provided_secret:
            return False
        return hmac.compare_digest(provided_secret, self._shared_secret)

    def ingest(self, payload: dict[str, Any], provided_secret: str | None) -> Post:
        if not self.verify_secret(provided_secret):
            raise WebhookAuthError("Invalid or missing webhook secret.")
        post = parse_post_payload(payload, source=self.name)
        self._queue.append(post)
        return post

    def fetch_new_posts(self, since_post_id: str | None) -> list[Post]:
        drained, self._queue = self._queue, []
        return drained
