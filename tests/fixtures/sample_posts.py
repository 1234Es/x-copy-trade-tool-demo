"""Reusable sample post payloads for tests (Phase 14 test list)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.sources.base_source import Post

NOW = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)


def make_post(text: str, **overrides) -> Post:
    base = dict(
        post_id="post-1",
        author="waltervannelli",
        text=text,
        posted_at=NOW,
        source="manual",
    )
    base.update(overrides)
    return Post(**base)


UNRELATED_POST = make_post("Just had the best coffee of my life this morning.", post_id="post-unrelated")

EXPLICIT_LONG_POST = make_post(
    "Long EURUSD here. Entry 1.0850, stop 1.0800, target 1.0950. Swing setup.", post_id="post-long"
)

EXPLICIT_SHORT_POST = make_post(
    "Short GBPUSD, entry 1.2650, sl 1.2700, tp 1.2550.", post_id="post-short"
)

CRYPTIC_POST = make_post("Adding here. Same setup.", post_id="post-cryptic")

MISSING_INSTRUMENT_POST = make_post("Going long here, stop below recent low, target the highs.", post_id="post-noinstrument")

MISSING_STOP_POST = make_post("Long EURUSD at 1.0850, targeting 1.0950.", post_id="post-nostop")

THREAD_REPLY_POST = make_post(
    "Moving stops to breakeven.", post_id="post-reply", reply_to_id="post-long"
)

STALE_POST = make_post(
    "Long EURUSD, entry 1.0850, stop 1.0800, target 1.0950.",
    post_id="post-stale",
    posted_at=NOW.replace(hour=10),  # 4 hours before NOW, well past a 10-minute staleness window
)
