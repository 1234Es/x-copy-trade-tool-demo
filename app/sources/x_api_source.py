"""X API v2 source adapter -- built to the real, current endpoint contracts
but NOT wired into the default run configuration (see DESIGN.md Section 2:
X's API has no free tier for new developers as of Feb 2026; this adapter
only becomes usable once you've independently signed up for paid access
and set X_BEARER_TOKEN).

Verified against docs.x.com (fetched 2026-07-13):
  - Base URL: https://api.x.com/2
  - GET /users/by/username/{username} -- resolve a handle to a user ID.
  - GET /users/{id}/tweets -- that user's own posted timeline (not mentions).
    Response envelope: {"data": [...], "includes": {...}, "meta": {...}}.
  - Auth: `Authorization: Bearer <token>` header.
  - Each tweet's `referenced_tweets` field (when present) gives
    [{"type": "replied_to"|"quoted"|"retweeted", "id": "..."}], used here to
    populate reply_to_id / quoted_post_id / is_repost.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.sources.base_source import BaseSource, MediaItem, Post


class XApiError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"X API error {status_code}: {body}")


class XApiRateLimitError(XApiError):
    pass


class XApiSource(BaseSource):
    name = "x_api"

    def __init__(self, bearer_token: str, base_url: str = "https://api.x.com", request_timeout_seconds: float = 10.0):
        if not bearer_token:
            raise ValueError(
                "X_BEARER_TOKEN is empty -- the X API adapter cannot run without paid API access. "
                "See DESIGN.md Section 2 for current pricing; use the manual/webhook adapters instead."
            )
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {bearer_token}"})
        self._timeout = request_timeout_seconds
        self._user_id_cache: dict[str, str] = {}

    @retry(
        retry=retry_if_exception_type((XApiRateLimitError, requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/2{path}"
        response = self._session.get(url, params=params, timeout=self._timeout)
        if response.status_code == 429:
            raise XApiRateLimitError(response.status_code, response.text)
        if not response.ok:
            raise XApiError(response.status_code, response.text)
        return response.json()

    def _resolve_user_id(self, username: str) -> str:
        username = username.lstrip("@")
        if username in self._user_id_cache:
            return self._user_id_cache[username]
        data = self._get(f"/users/by/username/{username}", params={})
        user_id = data["data"]["id"]
        self._user_id_cache[username] = user_id
        return user_id

    def fetch_new_posts_for_username(self, username: str, since_post_id: str | None) -> list[Post]:
        user_id = self._resolve_user_id(username)
        params: dict[str, Any] = {
            "max_results": 25,
            "tweet.fields": "created_at,referenced_tweets,attachments,text",
            "expansions": "attachments.media_keys",
            "media.fields": "type,url,alt_text",
        }
        if since_post_id:
            params["since_id"] = since_post_id

        data = self._get(f"/users/{user_id}/tweets", params=params)
        tweets = data.get("data", [])
        media_by_key = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}

        posts: list[Post] = []
        for tweet in tweets:
            reply_to_id = None
            quoted_post_id = None
            is_repost = False
            for ref in tweet.get("referenced_tweets", []):
                if ref["type"] == "replied_to":
                    reply_to_id = ref["id"]
                elif ref["type"] == "quoted":
                    quoted_post_id = ref["id"]
                elif ref["type"] == "retweeted":
                    is_repost = True

            media_keys = tweet.get("attachments", {}).get("media_keys", [])
            media = tuple(
                MediaItem(
                    media_type=media_by_key[k].get("type", "unknown"),
                    url=media_by_key[k].get("url"),
                    alt_text=media_by_key[k].get("alt_text"),
                )
                for k in media_keys
                if k in media_by_key
            )

            posts.append(
                Post(
                    post_id=tweet["id"],
                    author=username.lstrip("@").lower(),
                    text=tweet["text"],
                    posted_at=datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00")),
                    source=self.name,
                    reply_to_id=reply_to_id,
                    quoted_post_id=quoted_post_id,
                    is_repost=is_repost,
                    media=media,
                    raw_payload=tweet,
                )
            )
        # X returns newest-first; the rest of the pipeline expects oldest-first.
        return list(reversed(posts))

    def fetch_new_posts(self, since_post_id: str | None) -> list[Post]:
        raise NotImplementedError(
            "XApiSource requires a username -- call fetch_new_posts_for_username directly "
            "(the monitoring loop iterates tracked_accounts.yaml and calls it per account)."
        )
