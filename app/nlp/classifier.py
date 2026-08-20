"""First-stage classifier (Phase 3): deterministic rules first, OpenAI
structured classification only for posts that survive the rule-based
prefilter. This is a deliberate cost/safety measure -- obviously unrelated
posts (no trading vocabulary, no ticker-like tokens) never reach the model
at all, and are logged as `unrelated` with no OpenAI request made.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.monitoring.logging import get_logger
from app.nlp.context_engine import ContextEngine, PostContext
from app.nlp.openai_client import MalformedOpenAIResponseError, OpenAIClient
from app.nlp.prompts import CLASSIFICATION_SYSTEM_PROMPT
from app.nlp.schemas import CLASSIFICATION_JSON_SCHEMA, PostCategory, PostClassification
from app.sources.base_source import Post

log = get_logger("classifier")

_CASHTAG_PATTERN = re.compile(r"\$[A-Za-z]{1,6}\b")

_TRADING_KEYWORDS = frozenset(
    {
        "long", "short", "buy", "sell", "entry", "entered", "stop", "sl", "tp", "target",
        "targets", "risk", "r:r", "rr", "breakout", "breakdown", "support", "resistance",
        "bullish", "bearish", "bias", "setup", "trade", "trading", "position", "closed",
        "close", "invalidated", "invalidation", "runner", "scalp", "swing", "leverage",
        "pips", "lot", "lots", "chart", "level", "levels", "stopped", "profit", "loss",
    }
)


@dataclass(frozen=True)
class RuleBasedSignals:
    has_cashtag: bool
    matched_keywords: tuple[str, ...]

    @property
    def worth_llm_review(self) -> bool:
        return self.has_cashtag or len(self.matched_keywords) > 0


def rule_based_prefilter(text: str) -> RuleBasedSignals:
    has_cashtag = bool(_CASHTAG_PATTERN.search(text))
    lowered_tokens = set(re.findall(r"[a-zA-Z:]+", text.lower()))
    matched = tuple(sorted(_TRADING_KEYWORDS & lowered_tokens))
    return RuleBasedSignals(has_cashtag=has_cashtag, matched_keywords=matched)


@dataclass(frozen=True)
class ClassificationResult:
    classification: PostClassification
    rule_based_signals: RuleBasedSignals
    openai_request_id: str | None


class Classifier:
    def __init__(self, openai_client: OpenAIClient):
        self.openai_client = openai_client

    def classify(self, post: Post, context: PostContext) -> ClassificationResult:
        rule_signals = rule_based_prefilter(post.text)

        if not rule_signals.worth_llm_review:
            return ClassificationResult(
                classification=PostClassification(
                    category=PostCategory.UNRELATED,
                    reasoning="No trading-related keywords or cashtags detected; skipped LLM classification.",
                ),
                rule_based_signals=rule_signals,
                openai_request_id=None,
            )

        user_content = (
            f"POST by @{post.author} (id={post.post_id}, posted_at={post.posted_at.isoformat()}):\n"
            f'"{post.text}"\n\n'
            f"CONTEXT:\n{ContextEngine.format_for_prompt(context)}"
        )

        try:
            result = self.openai_client.structured_completion(
                CLASSIFICATION_SYSTEM_PROMPT, user_content, CLASSIFICATION_JSON_SCHEMA
            )
            classification = PostClassification.model_validate(result.parsed)
            request_id = result.request_id
        except (MalformedOpenAIResponseError, ValueError) as exc:
            # Fail safe, not silent: an unparseable classification response
            # is treated as too_ambiguous, never guessed at or defaulted to
            # something actionable. But it must be LOGGED at error level --
            # too_ambiguous from a genuine model judgment call and
            # too_ambiguous from a broken API key/model/quota look identical
            # to the dashboard otherwise.
            log.error("classification_call_failed", post_id=post.post_id, author=post.author, error=str(exc))
            classification = PostClassification(
                category=PostCategory.TOO_AMBIGUOUS,
                reasoning="Classification model response was malformed or failed schema validation.",
            )
            request_id = None

        return ClassificationResult(classification=classification, rule_based_signals=rule_signals, openai_request_id=request_id)
