"""Manual/JSON input adapter -- the default ingestion path for this demo.

A human (via the dashboard or a direct HTTP POST / JSON file) supplies a
post's text and metadata directly. This requires no external credentials
and cannot violate any platform's terms of service, since no automated
access to X happens at all.
"""
from __future__ import annotations

from typing import Any

from app.sources.base_source import BaseSource, Post, parse_post_payload

# Re-exported for callers that catch the specific validation error by name.
from app.sources.base_source import PostPayloadValidationError as ManualSourceValidationError  # noqa: F401


class ManualSource(BaseSource):
    name = "manual"

    def __init__(self) -> None:
        self._queue: list[Post] = []

    def submit(self, payload: dict[str, Any]) -> Post:
        post = parse_post_payload(payload, source=self.name)
        self._queue.append(post)
        return post

    def fetch_new_posts(self, since_post_id: str | None) -> list[Post]:
        drained, self._queue = self._queue, []
        return drained
