"""Common post representation and the source-adapter interface.

Every ingestion path (manual, webhook, X API) normalizes into the same
`Post` shape before anything downstream sees it, so the rest of the
pipeline never needs to know which adapter a post came from.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class MediaItem:
    media_type: str  # "image" | "video" | "unknown"
    url: str | None = None
    alt_text: str | None = None


@dataclass(frozen=True)
class Post:
    post_id: str
    author: str
    text: str
    posted_at: datetime
    source: str  # "manual" | "webhook" | "x_api"
    reply_to_id: str | None = None
    quoted_post_id: str | None = None
    is_repost: bool = False
    media: tuple[MediaItem, ...] = field(default_factory=tuple)
    raw_payload: dict | None = None


class PostPayloadValidationError(Exception):
    pass


def parse_post_payload(payload: dict[str, Any], source: str) -> Post:
    """Shared normalization for the manual and webhook adapters -- both
    accept the same minimal JSON shape (author, text, and optional
    metadata), so the validation and defaulting logic lives in one place.
    """
    author = payload.get("author")
    text = payload.get("text")
    if not author or not isinstance(author, str):
        raise PostPayloadValidationError("`author` is required and must be a non-empty string.")
    if not text or not isinstance(text, str):
        raise PostPayloadValidationError("`text` is required and must be a non-empty string.")

    post_id = payload.get("post_id") or f"{source}-{uuid.uuid4()}"

    posted_at_raw = payload.get("posted_at")
    if posted_at_raw:
        posted_at = datetime.fromisoformat(str(posted_at_raw).replace("Z", "+00:00"))
    else:
        posted_at = datetime.now(timezone.utc)

    media = tuple(
        MediaItem(media_type=m.get("media_type", "unknown"), url=m.get("url"), alt_text=m.get("alt_text"))
        for m in payload.get("media", [])
    )

    return Post(
        post_id=post_id,
        author=author.lstrip("@").lower(),
        text=text,
        posted_at=posted_at,
        source=source,
        reply_to_id=payload.get("reply_to_id"),
        quoted_post_id=payload.get("quoted_post_id"),
        is_repost=bool(payload.get("is_repost", False)),
        media=media,
        raw_payload=payload,
    )


class BaseSource(ABC):
    """A source is pull-based: the monitoring loop calls `fetch_new_posts`
    on an interval. Push-style adapters (manual submission, webhooks) hold
    an internal queue and drain it here so they still fit this interface.
    """

    name: str

    @abstractmethod
    def fetch_new_posts(self, since_post_id: str | None) -> list[Post]:
        """Return newly available posts, oldest first. `since_post_id` is
        the last successfully processed post ID (Phase 2: resume safety) --
        implementations that can filter server-side should; others may
        return everything currently available and let the caller dedupe
        against storage.
        """
        raise NotImplementedError
