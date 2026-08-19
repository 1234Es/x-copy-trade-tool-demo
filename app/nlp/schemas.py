"""The structured signal schema (DESIGN.md Section 4) as both a Pydantic
model (for validating/typing the parsed result) and an explicit JSON Schema
dict (for OpenAI's `strict: true` structured-output contract).

These are kept as two definitions rather than one auto-generated from the
other because OpenAI's strict mode has specific requirements (every
property listed in `required`, `additionalProperties: false` at every
object level, nullable fields expressed as `type: [T, "null"]` plus `null`
in any `enum`) that are easy to get subtly wrong via automatic JSON-Schema
generation from a Python type -- writing the API-facing schema by hand
keeps it auditable and guaranteed-correct independent of Pydantic's
internal schema generator version.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class PostCategory(str, Enum):
    EXPLICIT_TRADE_ENTRY = "explicit_trade_entry"
    TRADE_UPDATE = "trade_update"
    STOP_LOSS_ADJUSTMENT = "stop_loss_adjustment"
    TAKE_PROFIT_ADJUSTMENT = "take_profit_adjustment"
    PARTIAL_CLOSE_INSTRUCTION = "partial_close_instruction"
    FULL_CLOSE_INSTRUCTION = "full_close_instruction"
    MARKET_OBSERVATION = "market_observation"
    EDUCATIONAL = "educational"
    PROMOTIONAL = "promotional"
    UNRELATED = "unrelated"
    TOO_AMBIGUOUS = "too_ambiguous"


# Only these categories are allowed to proceed to signal extraction (Phase 3:
# "Only explicit trade instructions or clearly linked trade-management
# updates may proceed to execution analysis.")
ACTIONABLE_CATEGORIES = frozenset(
    {
        PostCategory.EXPLICIT_TRADE_ENTRY,
        PostCategory.TRADE_UPDATE,
        PostCategory.STOP_LOSS_ADJUSTMENT,
        PostCategory.TAKE_PROFIT_ADJUSTMENT,
        PostCategory.PARTIAL_CLOSE_INSTRUCTION,
        PostCategory.FULL_CLOSE_INSTRUCTION,
    }
)


class SignalType(str, Enum):
    NEW_TRADE = "new_trade"
    UPDATE_STOP = "update_stop"
    UPDATE_TARGET = "update_target"
    PARTIAL_CLOSE = "partial_close"
    FULL_CLOSE = "full_close"
    OBSERVATION = "observation"
    NO_TRADE = "no_trade"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TradeSignal(BaseModel):
    """Parsed & validated form of the OpenAI structured-extraction result."""

    post_id: str
    author: str
    signal_type: SignalType
    instrument: str | None = None
    direction: Direction | None = None
    order_type: OrderType | None = None
    entry_price: float | None = None
    entry_zone_low: float | None = None
    entry_zone_high: float | None = None
    stop_loss: float | None = None
    take_profit: list[float] = Field(default_factory=list)
    timeframe: str | None = None
    valid_until: datetime | None = None
    referenced_trade_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    requires_human_review: bool
    reasoning_summary: str | None = None


TRADE_SIGNAL_JSON_SCHEMA: dict = {
    "name": "trade_signal",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "post_id": {"type": "string"},
            "author": {"type": "string"},
            "signal_type": {
                "type": "string",
                "enum": [t.value for t in SignalType],
            },
            "instrument": {"type": ["string", "null"], "description": "Raw symbol/ticker as written, not yet mapped to a broker instrument."},
            "direction": {"type": ["string", "null"], "enum": ["long", "short", None]},
            "order_type": {"type": ["string", "null"], "enum": ["market", "limit", "stop", None]},
            "entry_price": {"type": ["number", "null"]},
            "entry_zone_low": {"type": ["number", "null"]},
            "entry_zone_high": {"type": ["number", "null"]},
            "stop_loss": {"type": ["number", "null"]},
            "take_profit": {"type": "array", "items": {"type": "number"}},
            "timeframe": {"type": ["string", "null"]},
            "valid_until": {"type": ["string", "null"], "description": "ISO-8601 datetime, or null if the post gives no explicit expiry."},
            "referenced_trade_id": {"type": ["string", "null"], "description": "Our internal signal_id this post refers to, if the linkage is clear -- filled in by the context engine's candidate list, not invented."},
            "confidence": {"type": "number", "description": "0 to 1."},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact substrings copied verbatim from the post text -- never paraphrased.",
            },
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
            "requires_human_review": {"type": "boolean"},
            "reasoning_summary": {"type": ["string", "null"]},
        },
        "required": [
            "post_id", "author", "signal_type", "instrument", "direction", "order_type",
            "entry_price", "entry_zone_low", "entry_zone_high", "stop_loss", "take_profit",
            "timeframe", "valid_until", "referenced_trade_id", "confidence", "evidence",
            "assumptions", "missing_fields", "requires_human_review", "reasoning_summary",
        ],
        "additionalProperties": False,
    },
}


CLASSIFICATION_JSON_SCHEMA: dict = {
    "name": "post_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [c.value for c in PostCategory]},
            "reasoning": {"type": "string", "description": "One sentence, non-sensitive."},
        },
        "required": ["category", "reasoning"],
        "additionalProperties": False,
    },
}


class PostClassification(BaseModel):
    category: PostCategory
    reasoning: str
