"""SQLAlchemy Core table definitions -- see DESIGN.md Section 7.

Core (not the ORM) is used deliberately: this project's access patterns are
simple inserts/selects driven entirely from `repository.py`, and Core keeps
the schema declaration and the query code both explicit and easy to audit,
which matters for an audit-trail-heavy system like this one.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

raw_posts = Table(
    "raw_posts",
    metadata,
    Column("post_id", String, primary_key=True),
    Column("author", String, nullable=False, index=True),
    Column("text", Text, nullable=False),
    Column("posted_at", DateTime(timezone=True), nullable=False),
    Column("source", String, nullable=False),  # manual | json | webhook | x_api
    Column("reply_to_id", String, nullable=True),
    Column("quoted_post_id", String, nullable=True),
    Column("is_repost", Boolean, nullable=False, default=False),
    Column("media_json", Text, nullable=True),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("raw_payload_json", Text, nullable=True),
    Column("superseded_by_post_id", String, nullable=True),  # edit/delete tracking
)

classifications = Table(
    "classifications",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("post_id", String, nullable=False, index=True),
    Column("category", String, nullable=False),
    Column("rule_based_signals_json", Text, nullable=True),
    Column("openai_request_id", String, nullable=True),
    Column("classified_at", DateTime(timezone=True), nullable=False),
)

signals = Table(
    "signals",
    metadata,
    Column("signal_id", String, primary_key=True),
    Column("post_id", String, nullable=False, index=True),
    Column("author", String, nullable=False, index=True),
    Column("signal_type", String, nullable=False),
    Column("instrument", String, nullable=True),
    Column("direction", String, nullable=True),
    Column("order_type", String, nullable=True),
    Column("entry_price", Float, nullable=True),
    Column("entry_zone_low", Float, nullable=True),
    Column("entry_zone_high", Float, nullable=True),
    Column("stop_loss", Float, nullable=True),
    Column("take_profit_json", Text, nullable=True),  # JSON list of numbers
    Column("timeframe", String, nullable=True),
    Column("valid_until", DateTime(timezone=True), nullable=True),
    Column("referenced_trade_id", String, nullable=True),
    Column("confidence", Float, nullable=False),
    Column("evidence_json", Text, nullable=False),
    Column("assumptions_json", Text, nullable=False),
    Column("missing_fields_json", Text, nullable=False),
    Column("requires_human_review", Boolean, nullable=False),
    Column("reasoning_summary", Text, nullable=True),
    Column("openai_request_id", String, nullable=True),
    Column("validation_status", String, nullable=False, default="pending"),  # pending|approved|rejected
    Column("rejection_reason", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

proposals = Table(
    "proposals",
    metadata,
    Column("proposal_id", String, primary_key=True),
    Column("signal_id", String, nullable=False, index=True),
    Column("status", String, nullable=False, default="pending"),  # pending|approved|rejected|expired
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("decided_by", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

orders = Table(
    "orders",
    metadata,
    Column("order_id", String, primary_key=True),  # also used as OANDA clientExtensions.id
    Column("signal_id", String, nullable=False, index=True),
    Column("oanda_order_id", String, nullable=True),
    Column("instrument", String, nullable=False),
    Column("units", Integer, nullable=False),
    Column("status", String, nullable=False),  # filled|rejected
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("broker_response_json", Text, nullable=True),
)

trades = Table(
    "trades",
    metadata,
    Column("oanda_trade_id", String, primary_key=True),
    Column("order_id", String, nullable=False, index=True),
    Column("instrument", String, nullable=False),
    Column("direction", String, nullable=False),
    Column("open_price", Float, nullable=True),
    Column("close_price", Float, nullable=True),
    Column("open_time", DateTime(timezone=True), nullable=True),
    Column("close_time", DateTime(timezone=True), nullable=True),
    Column("realized_pl", Float, nullable=True),
    Column("exit_reason", String, nullable=True),
)

author_glossary = Table(
    "author_glossary",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("author", String, nullable=False, index=True),
    Column("term", String, nullable=False),
    Column("meaning", Text, nullable=False),
    Column("confirmed_by_human", Boolean, nullable=False, default=False),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
)

circuit_breaker_events = Table(
    "circuit_breaker_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("trigger_type", String, nullable=False),
    Column("details_json", Text, nullable=True),
    Column("cleared_at", DateTime(timezone=True), nullable=True),
)

reconciliation_log = Table(
    "reconciliation_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("local_state_json", Text, nullable=True),
    Column("broker_state_json", Text, nullable=True),
    Column("discrepancy_json", Text, nullable=True),
)

cursors = Table(
    "cursors",
    metadata,
    Column("source_key", String, primary_key=True),  # e.g. "x_api:waltervannelli"
    Column("last_processed_post_id", String, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("timestamp_utc", DateTime(timezone=True), primary_key=True),
    Column("balance", Float, nullable=False),
    Column("equity", Float, nullable=False),
    Column("margin_used", Float, nullable=False),
    Column("margin_available", Float, nullable=False),
    Column("open_position_count", Integer, nullable=False),
    Column("daily_pl", Float, nullable=False),
    Column("weekly_pl", Float, nullable=False),
)
