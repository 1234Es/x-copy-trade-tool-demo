"""Phase 4: converts a candidate post into the strict structured signal.

Two safeguards are enforced in code, independent of what the model claims:
  1. Every `evidence` fragment must appear verbatim in the source post text
     -- a fabricated or paraphrased quote fails the whole extraction.
  2. `requires_human_review` is recomputed server-side and OR'd with the
     model's own value -- the model can only make this stricter by being
     honest about it, never looser than what our own rules require.

A missing `stop_loss` is the one exception to "stricter only": when
`missing_stop_loss_behavior` (risk.yaml) is `apply_risk_model`, a missing
stop_loss alone does not force review here, because signal_validator.py
will compute a fallback stop instead of rejecting. Forcing review here
regardless would silently defeat that config -- every new_trade signal
from an account that never states a stop would always become a proposal
requiring a human click, even in practice_auto mode, no matter what
missing_stop_loss_behavior says.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.nlp.context_engine import ContextEngine, PostContext
from app.nlp.openai_client import MalformedOpenAIResponseError, OpenAIClient
from app.nlp.prompts import EXTRACTION_SYSTEM_PROMPT
from app.nlp.schemas import TRADE_SIGNAL_JSON_SCHEMA, SignalType, TradeSignal
from app.sources.base_source import Post

_WHITESPACE_RUN = re.compile(r"\s+")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


def _normalize_symbol(text: str) -> str:
    """Lowercases and strips separators so "EUR/USD", "Eur/usd", "eur usd"
    and "EURUSD" all compare equal -- the same author spells a pair several
    different ways from post to post."""
    return _NON_ALPHANUMERIC.sub("", text.lower())


def _normalize_whitespace(text: str) -> str:
    """Collapses runs of whitespace to a single space for the evidence
    verbatim check only -- source tweets sometimes have double spaces/typos
    that a model harmlessly normalizes when quoting them back (e.g. "extesion
    only" vs the source's "extesion  only"). The check must still catch a
    genuinely fabricated or paraphrased quote; it just shouldn't punish
    whitespace collapsing that doesn't change the actual words."""
    return _WHITESPACE_RUN.sub(" ", text)


@dataclass(frozen=True)
class ExtractionResult:
    signal: TradeSignal | None
    openai_request_id: str | None
    rejection_reason: str | None

    @property
    def accepted(self) -> bool:
        return self.signal is not None


class SignalExtractor:
    def __init__(
        self,
        openai_client: OpenAIClient,
        context_engine: ContextEngine,
        min_confidence: float,
        missing_stop_loss_behavior: str = "human_review",
    ):
        self.openai_client = openai_client
        self.context_engine = context_engine
        self.min_confidence = min_confidence
        self.missing_stop_loss_behavior = missing_stop_loss_behavior

    def extract(self, post: Post, context: PostContext) -> ExtractionResult:
        user_content = self._build_user_content(post, context)

        try:
            result = self.openai_client.structured_completion(
                EXTRACTION_SYSTEM_PROMPT, user_content, TRADE_SIGNAL_JSON_SCHEMA
            )
        except MalformedOpenAIResponseError as exc:
            return ExtractionResult(None, None, f"malformed_openai_response: {exc}")

        try:
            signal = TradeSignal.model_validate(result.parsed)
        except ValidationError as exc:
            return ExtractionResult(None, result.request_id, f"schema_validation_failed: {exc}")

        for fragment in signal.evidence:
            if fragment and _normalize_whitespace(fragment) not in _normalize_whitespace(post.text):
                return ExtractionResult(
                    None, result.request_id, f"evidence_not_verbatim: fragment not found in source post: {fragment!r}"
                )

        if signal.referenced_trade_id is not None:
            valid_ids = {c.signal_id for c in context.open_signal_candidates}
            if signal.referenced_trade_id not in valid_ids:
                return ExtractionResult(
                    None, result.request_id,
                    f"invalid_referenced_trade_id: {signal.referenced_trade_id!r} was not one of the given candidates",
                )

        signal = self._enforce_human_review(signal, post.text)
        return ExtractionResult(signal, result.request_id, None)

    def _enforce_human_review(self, signal: TradeSignal, post_text: str) -> TradeSignal:
        stop_loss_auto_fillable = self.missing_stop_loss_behavior == "apply_risk_model"

        must_review = signal.requires_human_review
        if signal.confidence < self.min_confidence:
            must_review = True
        # Deterministic backstop for prompts.py rule 13's narrow
        # instrument-inheritance exception: an instrument that doesn't
        # appear in this post's own text was inherited from surrounding
        # context rather than stated here, so it goes to a human regardless
        # of whether the model remembered to declare that in `assumptions`.
        # Same principle as the verbatim `evidence` check above -- the
        # model's own account of what it did is never the thing relied on.
        if signal.instrument and _normalize_symbol(signal.instrument) not in _normalize_symbol(post_text):
            must_review = True
        if signal.signal_type == SignalType.NEW_TRADE and (
            signal.instrument is None
            or signal.direction is None
            or (signal.stop_loss is None and not stop_loss_auto_fillable)
        ):
            must_review = True
        # missing_fields may legitimately contain "stop_loss" (covered, and
        # possibly forgiven, by the check above) or "entry_price" -- a null
        # entry_price is a deliberate, safe simplification meaning "market
        # order at the prevailing price" (prompts.py rule 12: a stated
        # number framed as the position's blended running average, not a
        # fresh fill, must not be used as entry_price), not missing
        # information that needs a human's judgment call. Neither should
        # trip this blanket rule on its own.
        other_missing_fields = [
            f for f in signal.missing_fields
            if not (f == "stop_loss" and stop_loss_auto_fillable) and f != "entry_price"
        ]
        if other_missing_fields:
            must_review = True
        if must_review != signal.requires_human_review:
            return signal.model_copy(update={"requires_human_review": must_review})
        return signal

    def _build_user_content(self, post: Post, context: PostContext) -> str:
        return (
            f"POST by @{post.author} (post_id={post.post_id}, posted_at={post.posted_at.isoformat()}):\n"
            f'"{post.text}"\n\n'
            f"CONTEXT:\n{self.context_engine.format_for_prompt(context)}\n\n"
            "Produce the structured signal now. post_id must be exactly the value given above, "
            f"and author must be exactly \"{post.author}\"."
        )
