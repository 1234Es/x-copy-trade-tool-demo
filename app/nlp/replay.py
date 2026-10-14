"""Replay stored posts through the CURRENT classification and extraction
prompts, and report every decision that would now come out differently.

The extraction prompt has changed about six times, and each change was
checked against a single live post before being deployed. A change that
fixed one phrasing could quietly break another, and the first sign would
be a missed trade days later. Replaying the stored history first turns
that into a list of exactly which past decisions a change would flip.

Scope, deliberately: only the language stages are replayed -- the
classifier and the extractor, which are what a prompt change affects.
Validation and risk depend on the live market and account state at the
moment a post arrived (prices, staleness, open positions), which can't be
reconstructed after the fact, so re-running them would report differences
that have nothing to do with the prompt.

Context is rebuilt from the replay set itself, as of each post's own time:
the author's previous posts, and the parent post when it's in the set. Two
things can't be rebuilt and are left out, so treat any difference in
`referenced_trade_id` (and close posts that depend on it) with care: the
open-trade candidates that existed at the time, and the confirmed author
glossary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.nlp.classifier import Classifier, rule_based_prefilter
from app.nlp.context_engine import ContextItem, PostContext
from app.nlp.schemas import ACTIONABLE_CATEGORIES
from app.nlp.signal_extractor import SignalExtractor
from app.sources.base_source import Post

# Context depth matches ContextEngine.build_context's default, so a replayed
# post sees what it would have seen live.
RECENT_POSTS_IN_CONTEXT = 5

# The fields a prompt change can move, in rough order of how much a change
# matters: what kind of signal, which market, which way, how it's entered.
COMPARED_SIGNAL_FIELDS = (
    "signal_type", "instrument", "direction", "order_type", "entry_price", "stop_loss",
    "referenced_trade_id", "requires_human_review",
)

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class ReplayOutcome:
    post_id: str
    author: str
    posted_at: str
    text: str
    changes: list[FieldChange] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReplayResult:
    replayed: int = 0
    unchanged: int = 0
    changed: list[ReplayOutcome] = field(default_factory=list)
    errors: list[ReplayOutcome] = field(default_factory=list)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def item_to_post(item: dict) -> Post:
    p = item["post"]
    return Post(
        post_id=p["post_id"], author=p["author"], text=p["text"], posted_at=_parse_time(p["posted_at"]),
        source=p.get("source") or "replay", reply_to_id=p.get("reply_to_id"),
        quoted_post_id=p.get("quoted_post_id"), is_repost=bool(p.get("is_repost")),
    )


def build_context(post: Post, items: list[dict]) -> PostContext:
    """What the live context engine would have assembled for `post`, using
    only the replay set: the author's posts strictly before it, newest first,
    and its parent if present."""
    by_id = {i["post"]["post_id"]: i["post"] for i in items}
    earlier = sorted(
        (i["post"] for i in items
         if i["post"]["author"] == post.author and _parse_time(i["post"]["posted_at"]) < post.posted_at),
        key=lambda p: p["posted_at"], reverse=True,
    )[:RECENT_POSTS_IN_CONTEXT]
    recent = [
        ContextItem(
            post_id=p["post_id"], author=p["author"], text=p["text"], posted_at=_parse_time(p["posted_at"]),
            link_reason="recent post by the same author -- background only, NOT an assumed link to this post's subject",
            minutes_before_current_post=(post.posted_at - _parse_time(p["posted_at"])).total_seconds() / 60.0,
        )
        for p in earlier
    ]
    parent = None
    if post.reply_to_id and post.reply_to_id in by_id:
        pp = by_id[post.reply_to_id]
        parent = ContextItem(
            post_id=pp["post_id"], author=pp["author"], text=pp["text"], posted_at=_parse_time(pp["posted_at"]),
            link_reason="this post is a direct reply to it (reply_to_id)",
        )
    return PostContext(parent_post=parent, recent_author_posts=recent)


def needs_openai(item: dict) -> bool:
    """Posts with no trading vocabulary are classified locally with no API
    call, exactly as in production -- so they cost nothing to replay."""
    return rule_based_prefilter(item["post"]["text"]).worth_llm_review


def estimate_calls(items: list[dict]) -> int:
    """Upper-bound-ish estimate: one classification per post that reaches the
    model, plus one extraction for each that was actionable last time."""
    classify = sum(1 for i in items if needs_openai(i))
    extract = sum(
        1 for i in items
        if needs_openai(i) and i.get("classification")
        and i["classification"]["category"] in {c.value for c in ACTIONABLE_CATEGORIES}
    )
    return classify + extract


def _same(field_name: str, before: Any, after: Any) -> bool:
    if before is None or after is None:
        return before is after
    if field_name == "instrument":
        return _NON_ALPHANUMERIC.sub("", str(before).lower()) == _NON_ALPHANUMERIC.sub("", str(after).lower())
    if isinstance(before, (int, float)) and isinstance(after, (int, float)) and not isinstance(before, bool):
        return abs(float(before) - float(after)) < 1e-9
    return before == after


def compare(item: dict, category: str, signal: dict | None, rejection: str | None) -> list[FieldChange]:
    """Differences between the stored decision and the replayed one."""
    changes: list[FieldChange] = []
    stored_category = item["classification"]["category"] if item.get("classification") else None
    if stored_category != category:
        changes.append(FieldChange("category", stored_category, category))

    stored = item.get("signal")
    if stored is None and signal is None:
        return changes
    if stored is None or signal is None:
        before = stored["signal_type"] if stored else "no signal"
        after = signal["signal_type"] if signal else f"no signal ({rejection or 'not actionable'})"
        changes.append(FieldChange("signal", before, after))
        return changes
    for name in COMPARED_SIGNAL_FIELDS:
        if not _same(name, stored.get(name), signal.get(name)):
            changes.append(FieldChange(name, stored.get(name), signal.get(name)))
    return changes


def replay(items: list[dict], classifier: Classifier, extractor: SignalExtractor) -> ReplayResult:
    """Re-run each post through `classifier` and `extractor`. Read-only:
    nothing is written anywhere. An error on one post is recorded and the
    rest still run."""
    result = ReplayResult()
    for item in items:
        post = item_to_post(item)
        p = item["post"]
        try:
            context = build_context(post, items)
            classified = classifier.classify(post, context)
            category = classified.classification.category.value
            signal = rejection = None
            if classified.classification.category in ACTIONABLE_CATEGORIES:
                extracted = extractor.extract(post, context)
                if extracted.accepted:
                    signal = extracted.signal.model_dump(mode="json")
                else:
                    rejection = extracted.rejection_reason
            changes = compare(item, category, signal, rejection)
        except Exception as exc:  # noqa: BLE001 -- one bad post mustn't sink a paid run
            result.errors.append(ReplayOutcome(p["post_id"], p["author"], p["posted_at"], p["text"], error=str(exc)))
            continue
        result.replayed += 1
        if changes:
            result.changed.append(ReplayOutcome(p["post_id"], p["author"], p["posted_at"], p["text"], changes))
        else:
            result.unchanged += 1
    return result


def render_report(result: ReplayResult, source: str) -> str:
    """Markdown report of what changed, most consequential fields first."""
    lines = [
        "# Prompt replay report",
        "",
        f"Source: `{source}`  ",
        f"Replayed {result.replayed} post(s): **{len(result.changed)} changed**, {result.unchanged} unchanged"
        + (f", {len(result.errors)} errored" if result.errors else "") + ".",
        "",
        "Only the classifier and extractor are replayed. Validation and risk depend on live market state "
        "at the time and are not re-run. Differences in `referenced_trade_id` are unreliable: the open-trade "
        "candidates that existed at the time can't be reconstructed.",
        "",
    ]
    for outcome in result.changed:
        lines.append(f"## {outcome.posted_at[:16]} · @{outcome.author} · `{outcome.post_id}`")
        lines.append("")
        lines.append(f"> {outcome.text.replace(chr(10), ' ')}")
        lines.append("")
        lines.append("| field | before | after |")
        lines.append("|---|---|---|")
        for c in outcome.changes:
            lines.append(f"| {c.field} | {c.before} | {c.after} |")
        lines.append("")
    if result.errors:
        lines.append("## Errors")
        lines.append("")
        for outcome in result.errors:
            lines.append(f"- `{outcome.post_id}`: {outcome.error}")
    return "\n".join(lines) + "\n"
