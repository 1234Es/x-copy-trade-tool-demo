"""Why signals didn't become trades, grouped by reason, with example posts.

Built because the two worst extraction gaps so far -- restated averages
being read as fresh entry prices, and past-tense fills being read as limit
orders -- were each found by hand, only after trades had already been
missed. A tally that names the reason and shows the posts behind it makes
that kind of pattern visible as it happens instead of in hindsight.

Two buckets matter more than any rejection reason because nothing else
would ever show them:

- approved_no_order: a signal that passed every check but never reached the
  broker. Its validation_status reads "approved", so it looks fine anywhere
  else. This is what happened 2026-09-17 to 2026-09-21, when auto-approve
  had been reset to off and approved signals expired as proposals.
- never_finished: a signal recorded and then never resolved, because the
  pipeline errored part-way through.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.monitoring.metrics import period_bounds
from app.storage.repository import Repository

EXAMPLES_PER_BUCKET = 3

# Close/adjust signals act on a trade that already exists, so they are
# judged by whether they were rejected, not by whether they produced an
# order: a full close updates the trade row and writes no order at all.
_MANAGEMENT_TYPES = {"full_close", "partial_close", "update_stop", "update_target"}

# key -> (label, hint, needs_attention)
_DESCRIPTIONS: dict[str, tuple[str, str, bool]] = {
    "approved_no_order": (
        "Passed every check, but no order was sent",
        "Almost always auto-approve being off: the signal became a proposal and expired after 5 minutes.",
        True,
    ),
    "never_finished": (
        "Processing stopped part-way",
        "The pipeline errored after recording the signal. Check the logs around that time.",
        True,
    ),
    "too_ambiguous": (
        "Classifier couldn't tell what the post meant",
        "Read the examples: if any are real instructions, the classification prompt needs work.",
        True,
    ),
    "signal_too_stale": (
        "Processed too long after it was posted",
        "Usually ingestion lag, or a backlog being worked through after an outage.",
        True,
    ),
    "entry_too_far_from_market": (
        "Stated price too far from the live market",
        "Often a restated running average rather than a fresh fill.",
        True,
    ),
    "missing_instrument": (
        "No instrument named in the post",
        "Bare corrections like \"sry 20 lots short\" land here.",
        True,
    ),
    "unmapped_instrument": (
        "Instrument spelling not recognised",
        "Add the spelling to app/config/instruments.yaml.",
        True,
    ),
    "no_referenced_trade_to_act_on": (
        "Close or adjust post not matched to an open trade",
        "Expected when our position had already closed on its own stop or target.",
        False,
    ),
    "not_actionable": (
        "Commentary, not an instruction",
        "Normally fine -- this is the filter doing its job.",
        False,
    ),
    "source_account_exposure_limit_reached": (
        "Per-account position cap reached",
        "Raise max_open_positions_from_account if you want more concurrent positions.",
        False,
    ),
    "consecutive_loss_cooldown_triggered": (
        "Halted after consecutive losses",
        "Lifts on its own once the cooldown has passed.",
        False,
    ),
    "missing_stop_loss_requires_human_review": (
        "No stop loss stated, and the fallback risk model is off",
        "Set missing_stop_loss_behavior: apply_risk_model in risk.yaml to compute one.",
        True,
    ),
}


@dataclass(frozen=True)
class ReportExample:
    post_id: str
    author: str
    text: str
    posted_at: datetime | None
    detail: str  # the full, un-normalised reason, e.g. "signal_too_stale:46.0min"


@dataclass(frozen=True)
class ReportBucket:
    key: str
    label: str
    hint: str
    needs_attention: bool
    count: int
    examples: list[ReportExample]


@dataclass(frozen=True)
class RejectionReport:
    period: str
    period_start: datetime | None
    period_end: datetime
    total_signals: int
    executed: int
    handled_management: int  # approved close/adjust signals
    buckets: list[ReportBucket] = field(default_factory=list)


def normalise_reason(reason: str) -> str:
    """Strips the per-instance detail so like reasons group together:
    "signal_too_stale:46.0min" -> "signal_too_stale". Circuit-breaker trips
    keep theirs, since WHICH breaker tripped is the useful part."""
    if reason.startswith("circuit_breaker_tripped:"):
        return reason
    if reason.startswith("re_validation_failed:"):
        return normalise_reason(reason.split(":", 1)[1])
    return reason.split(":", 1)[0]


def _describe(key: str) -> tuple[str, str, bool]:
    if key.startswith("circuit_breaker_tripped:"):
        which = key.split(":", 1)[1].replace(",", ", ")
        return (f"Trading was halted ({which})", "Clear the circuit breaker once the cause is fixed.", True)
    return _DESCRIPTIONS.get(key, (key.replace("_", " "), "", False))


def _bucket_key(row: dict) -> str | None:
    """Which bucket a signal falls in, or None if it simply succeeded."""
    status = row["validation_status"]
    if status == "rejected":
        return normalise_reason(row["rejection_reason"] or "unknown")
    if status == "pending":
        return "never_finished"
    if row["signal_type"] in _MANAGEMENT_TYPES:
        return None  # approved close/adjust: counted as handled
    return None if row["has_order"] else "approved_no_order"


def build_rejection_report(repository: Repository, period: str, now: datetime | None = None) -> RejectionReport:
    now = now or datetime.now(timezone.utc)
    start, end = period_bounds(period, now)

    rows = repository.get_signal_outcomes(since=start)
    grouped: dict[str, list[ReportExample]] = {}
    executed = handled_management = 0
    for row in rows:
        key = _bucket_key(row)
        if key is None:
            if row["signal_type"] in _MANAGEMENT_TYPES:
                handled_management += 1
            else:
                executed += 1
            continue
        grouped.setdefault(key, []).append(
            ReportExample(
                post_id=row["post_id"], author=row["author"], text=row["text"], posted_at=row["posted_at"],
                detail=row["rejection_reason"] or key,
            )
        )

    # too_ambiguous posts never become signals, so they'd otherwise be
    # invisible here -- yet they're where real instructions have been lost.
    ambiguous = repository.get_posts_with_category("too_ambiguous", since=start)
    if ambiguous:
        grouped["too_ambiguous"] = [
            ReportExample(post_id=p["post_id"], author=p["author"], text=p["text"], posted_at=p["posted_at"],
                          detail="too_ambiguous")
            for p in ambiguous
        ]

    buckets = []
    for key, examples in grouped.items():
        label, hint, attention = _describe(key)
        buckets.append(ReportBucket(
            key=key, label=label, hint=hint, needs_attention=attention, count=len(examples),
            examples=examples[:EXAMPLES_PER_BUCKET],
        ))
    # Things worth acting on first, then the biggest buckets.
    buckets.sort(key=lambda b: (not b.needs_attention, -b.count, b.key))

    return RejectionReport(
        period=period, period_start=start, period_end=end, total_signals=len(rows),
        executed=executed, handled_management=handled_management, buckets=buckets,
    )
