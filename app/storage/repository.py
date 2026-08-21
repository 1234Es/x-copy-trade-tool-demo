"""The only module that writes SQL. Everything else goes through here.

Every write is a plain, explicit `insert`/`update` statement -- no hidden
ORM lazy-loading or session state to reason about, which matters for an
audit trail: what got written, and when, should be traceable by reading
this file top to bottom.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.storage.models import (
    account_snapshots,
    author_glossary,
    circuit_breaker_events,
    classifications,
    cursors,
    orders,
    proposals,
    raw_posts,
    reconciliation_log,
    signals,
    trades,
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Converts a SQLAlchemy row mapping to a plain dict, reattaching UTC
    tzinfo to any naive datetime value.

    SQLite has no native timestamp type -- it stores datetimes as ISO
    strings and does NOT round-trip timezone info even when the column is
    declared `DateTime(timezone=True)`. Every datetime written by this
    repository is UTC (always constructed via `datetime.now(timezone.utc)`
    upstream), so it's safe to reattach UTC on the way out rather than
    leaving callers to compare naive vs. aware datetimes and crash.
    """
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        result[key] = value
    return result


def _dialect_insert(engine: Engine, table: Table):
    """SQLite (local dev) and PostgreSQL (production -- see DATABASE_URL /
    DESIGN.md's ephemeral-filesystem note) have differently-named upsert
    constructs, but sqlalchemy's dialect-specific Insert subclasses expose
    the identical on_conflict_do_nothing/on_conflict_do_update API, so a
    single call site can stay dialect-agnostic via this switch."""
    if engine.dialect.name == "postgresql":
        return pg_insert(table)
    return sqlite_insert(table)


class Repository:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ---- raw posts -----------------------------------------------------------

    def insert_raw_post(self, post: dict[str, Any]) -> bool:
        """Returns False if the post_id already existed (dedup), True if newly inserted."""
        stmt = _dialect_insert(self.engine, raw_posts).values(**post).on_conflict_do_nothing(index_elements=["post_id"])
        with self.engine.begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_raw_post(self, post_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(raw_posts).where(raw_posts.c.post_id == post_id)).mappings().first()
            return _row_to_dict(row) if row else None

    def mark_post_superseded(self, old_post_id: str, new_post_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(raw_posts).where(raw_posts.c.post_id == old_post_id).values(superseded_by_post_id=new_post_id)
            )

    def get_recent_posts(self, limit: int = 20) -> list[dict[str, Any]]:
        """All tracked accounts together, newest post first by when it was
        actually posted (not when we happened to ingest it) -- so accounts
        interleave by real chronology instead of grouping by poll batch."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(raw_posts).order_by(raw_posts.c.posted_at.desc()).limit(limit)
            ).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def get_recent_posts_by_author(self, author: str, before: datetime, limit: int = 10) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(raw_posts)
                .where(raw_posts.c.author == author, raw_posts.c.posted_at < before)
                .order_by(raw_posts.c.posted_at.desc())
                .limit(limit)
            ).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def get_thread_ancestors(self, post_id: str, max_depth: int = 5) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        current_id: str | None = post_id
        for _ in range(max_depth):
            post = self.get_raw_post(current_id) if current_id else None
            if post is None:
                break
            parent_id = post.get("reply_to_id")
            if not parent_id:
                break
            parent = self.get_raw_post(parent_id)
            if parent is None:
                break
            chain.append(parent)
            current_id = parent_id
        return chain

    # ---- classifications -------------------------------------------------------

    def insert_classification(
        self, post_id: str, category: str, rule_based_signals: dict, openai_request_id: str | None
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                classifications.insert().values(
                    post_id=post_id,
                    category=category,
                    rule_based_signals_json=json.dumps(rule_based_signals),
                    openai_request_id=openai_request_id,
                    classified_at=datetime.now(timezone.utc),
                )
            )

    def get_classification(self, post_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(classifications)
                .where(classifications.c.post_id == post_id)
                .order_by(classifications.c.id.desc())
            ).mappings().first()
            return _row_to_dict(row) if row else None

    # ---- signals ----------------------------------------------------------------

    def insert_signal(self, signal_row: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(signals.insert().values(**signal_row))

    def update_signal_validation(self, signal_id: str, status: str, reason: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(signals)
                .where(signals.c.signal_id == signal_id)
                .values(validation_status=status, rejection_reason=reason)
            )

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(signals).where(signals.c.signal_id == signal_id)).mappings().first()
            return _row_to_dict(row) if row else None

    def get_open_signals_by_author(self, author: str) -> list[dict[str, Any]]:
        """Signals that resulted in a still-open trade -- used for conflict
        detection (Phase 10) and "referenced_trade_id" resolution."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(signals, trades.c.close_time)
                .join(orders, orders.c.signal_id == signals.c.signal_id)
                .join(trades, trades.c.order_id == orders.c.order_id)
                .where(signals.c.author == author, trades.c.close_time.is_(None))
            ).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def get_signal_for_post(self, post_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(signals).where(signals.c.post_id == post_id).order_by(signals.c.created_at.desc())
            ).mappings().first()
            return _row_to_dict(row) if row else None

    def has_signal_for_post(self, post_id: str) -> bool:
        with self.engine.connect() as conn:
            row = conn.execute(select(signals.c.signal_id).where(signals.c.post_id == post_id)).first()
            return row is not None

    # ---- proposals ----------------------------------------------------------------

    def insert_proposal(self, proposal_row: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(proposals.insert().values(**proposal_row))

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(proposals).where(proposals.c.proposal_id == proposal_id)).mappings().first()
            return _row_to_dict(row) if row else None

    def update_proposal_status(self, proposal_id: str, status: str, decided_by: str | None, decided_at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(proposals)
                .where(proposals.c.proposal_id == proposal_id)
                .values(status=status, decided_by=decided_by, decided_at=decided_at)
            )

    def get_pending_proposals(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(proposals).where(proposals.c.status == "pending")).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def expire_stale_proposals(self, now: datetime) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(proposals)
                .where(proposals.c.status == "pending", proposals.c.expires_at < now)
                .values(status="expired", decided_at=now)
            )
            return result.rowcount

    # ---- orders / trades -------------------------------------------------------------

    def has_order_for_signal(self, signal_id: str) -> bool:
        with self.engine.connect() as conn:
            row = conn.execute(select(orders.c.order_id).where(orders.c.signal_id == signal_id)).first()
            return row is not None

    def insert_order(self, order_row: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(orders.insert().values(**order_row))

    def insert_trade(self, trade_row: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(trades.insert().values(**trade_row))

    def close_trade(self, oanda_trade_id: str, close_price: float, close_time: datetime, realized_pl: float, exit_reason: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(trades)
                .where(trades.c.oanda_trade_id == oanda_trade_id)
                .values(close_price=close_price, close_time=close_time, realized_pl=realized_pl, exit_reason=exit_reason)
            )

    def get_open_trades(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(trades).where(trades.c.close_time.is_(None))).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def get_open_trades_with_source(self) -> list[dict[str, Any]]:
        """Open trades joined through to the signal author that produced
        them -- unlike get_open_trades(), which callers use for broker
        reconciliation and don't need attribution for. Used by
        execution_engine._build_account_state() so per-source-account risk
        limits are checked against who actually opened each position, not
        whoever's signal is currently being evaluated. Left join (not
        inner) so a trade without a resolvable signal still counts toward
        max_open_positions -- it just has source_account=None."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(trades, signals.c.author.label("source_account"))
                .join(orders, trades.c.order_id == orders.c.order_id)
                .outerjoin(signals, orders.c.signal_id == signals.c.signal_id)
                .where(trades.c.close_time.is_(None))
            ).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def get_all_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        """Open and closed trades together, most recently opened first --
        backs the dashboard's Trades tab (full history, not just open
        positions). Joined with orders to include `units` -- trades itself
        doesn't store position size, only the order that created it does.
        Also joined through to signals to include `author` -- the tracked
        X account whose post produced the trade -- so the dashboard can
        show who's responsible for each position."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(trades, orders.c.units, signals.c.author)
                .join(orders, trades.c.order_id == orders.c.order_id)
                .outerjoin(signals, orders.c.signal_id == signals.c.signal_id)
                .order_by(trades.c.open_time.desc())
                .limit(limit)
            ).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def get_open_trade_for_signal(self, signal_id: str) -> dict[str, Any] | None:
        """The still-open trade produced by a given signal_id, if any --
        resolves a close/stop-move/target-move signal's `referenced_trade_id`
        (itself a signal_id, per DESIGN.md Section 4, not an OANDA trade id)
        to the actual open trade it should act on."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(trades, orders.c.units)
                .join(orders, trades.c.order_id == orders.c.order_id)
                .where(orders.c.signal_id == signal_id, trades.c.close_time.is_(None))
            ).mappings().first()
            return _row_to_dict(row) if row else None

    def get_open_trades_for_instrument(self, instrument: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(trades).where(trades.c.instrument == instrument, trades.c.close_time.is_(None))
            ).mappings().all()
            return [_row_to_dict(r) for r in rows]

    # ---- author glossary (human-confirmed only) ------------------------------------

    def get_glossary_term(self, author: str, term: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(author_glossary).where(
                    author_glossary.c.author == author,
                    author_glossary.c.term == term,
                    author_glossary.c.confirmed_by_human.is_(True),
                )
            ).mappings().first()
            return _row_to_dict(row) if row else None

    def get_all_glossary_terms(self, author: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(author_glossary).where(
                    author_glossary.c.author == author, author_glossary.c.confirmed_by_human.is_(True)
                )
            ).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def propose_glossary_term(self, author: str, term: str, meaning: str) -> int:
        """Inserted unconfirmed -- only usable by the context engine after a
        human confirms it via `confirm_glossary_term`."""
        with self.engine.begin() as conn:
            result = conn.execute(
                author_glossary.insert().values(
                    author=author, term=term, meaning=meaning, confirmed_by_human=False, confirmed_at=None
                )
            )
            return result.inserted_primary_key[0]

    def confirm_glossary_term(self, term_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(author_glossary)
                .where(author_glossary.c.id == term_id)
                .values(confirmed_by_human=True, confirmed_at=datetime.now(timezone.utc))
            )

    # ---- circuit breaker / reconciliation / snapshots -------------------------------

    def insert_circuit_breaker_event(self, trigger_type: str, details: dict, now: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                circuit_breaker_events.insert().values(
                    timestamp_utc=now, trigger_type=trigger_type, details_json=json.dumps(details, default=str)
                )
            )

    def insert_reconciliation_log(self, now: datetime, local_state: dict, broker_state: dict, discrepancy: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                reconciliation_log.insert().values(
                    timestamp_utc=now,
                    local_state_json=json.dumps(local_state, default=str),
                    broker_state_json=json.dumps(broker_state, default=str),
                    discrepancy_json=json.dumps(discrepancy, default=str),
                )
            )

    def insert_account_snapshot(self, snapshot: dict[str, Any]) -> None:
        stmt = _dialect_insert(self.engine, account_snapshots).values(**snapshot).on_conflict_do_update(
            index_elements=["timestamp_utc"], set_=snapshot
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    # ---- cursors (resume-safety) -----------------------------------------------------

    def get_last_processed_post_id(self, source_key: str) -> str | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(cursors).where(cursors.c.source_key == source_key)).mappings().first()
            return row["last_processed_post_id"] if row else None

    def set_last_processed_post_id(self, source_key: str, post_id: str, now: datetime) -> None:
        stmt = _dialect_insert(self.engine, cursors).values(
            source_key=source_key, last_processed_post_id=post_id, updated_at=now
        ).on_conflict_do_update(
            index_elements=["source_key"], set_={"last_processed_post_id": post_id, "updated_at": now}
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)
