"""Gathers context for a post without ever assuming two posts are about the
same trade just because they're close in time or topic.

Every piece of context returned here comes with an explicit `link_reason`
explaining *why* it's included -- "this post's reply_to_id points here" is a
clear reason; "posted by the same author two hours ago" is only ever
labeled as general background (style/glossary awareness), never as
evidence the posts share a trade. That distinction is what the extraction
prompt relies on to resolve `referenced_trade_id` only from explicit
candidates, never from proximity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.sources.base_source import Post
from app.storage.repository import Repository


@dataclass(frozen=True)
class ContextItem:
    post_id: str
    author: str
    text: str
    posted_at: datetime
    link_reason: str


@dataclass(frozen=True)
class OpenSignalCandidate:
    signal_id: str
    instrument: str | None
    direction: str | None
    signal_type: str
    created_at: datetime
    link_reason: str = "open signal from this author -- candidate only, not assumed to match"


@dataclass(frozen=True)
class PostContext:
    parent_post: ContextItem | None
    thread_ancestors: list[ContextItem] = field(default_factory=list)
    recent_author_posts: list[ContextItem] = field(default_factory=list)
    open_signal_candidates: list[OpenSignalCandidate] = field(default_factory=list)
    glossary_terms: dict[str, str] = field(default_factory=dict)


class ContextEngine:
    def __init__(self, repository: Repository):
        self.repository = repository

    def build_context(self, post: Post, max_recent: int = 5) -> PostContext:
        parent_post = None
        if post.reply_to_id:
            parent_row = self.repository.get_raw_post(post.reply_to_id)
            if parent_row:
                parent_post = ContextItem(
                    post_id=parent_row["post_id"],
                    author=parent_row["author"],
                    text=parent_row["text"],
                    posted_at=parent_row["posted_at"],
                    link_reason="this post is a direct reply to it (reply_to_id)",
                )

        thread_ancestors = [
            ContextItem(
                post_id=row["post_id"],
                author=row["author"],
                text=row["text"],
                posted_at=row["posted_at"],
                link_reason="earlier post in the same reply chain",
            )
            for row in self.repository.get_thread_ancestors(post.post_id)
        ]

        recent_author_posts = [
            ContextItem(
                post_id=row["post_id"],
                author=row["author"],
                text=row["text"],
                posted_at=row["posted_at"],
                link_reason="recent post by the same author -- background only, NOT an assumed link to this post's subject",
            )
            for row in self.repository.get_recent_posts_by_author(post.author, before=post.posted_at, limit=max_recent)
        ]

        open_signal_candidates = [
            OpenSignalCandidate(
                signal_id=row["signal_id"],
                instrument=row.get("instrument"),
                direction=row.get("direction"),
                signal_type=row["signal_type"],
                created_at=row["created_at"],
            )
            for row in self.repository.get_open_signals_by_author(post.author)
        ]

        glossary_rows = self.repository.get_all_glossary_terms(post.author)
        glossary_terms = {row["term"]: row["meaning"] for row in glossary_rows}

        return PostContext(
            parent_post=parent_post,
            thread_ancestors=thread_ancestors,
            recent_author_posts=recent_author_posts,
            open_signal_candidates=open_signal_candidates,
            glossary_terms=glossary_terms,
        )

    @staticmethod
    def format_for_prompt(context: PostContext) -> str:
        """Renders context as plain text for the OpenAI user message --
        explicit about link_reason for every item so the model can weigh
        relevance itself rather than being told what's "the same trade."
        """
        lines: list[str] = []
        if context.parent_post:
            lines.append(f"PARENT POST ({context.parent_post.link_reason}):")
            lines.append(f'  [{context.parent_post.post_id}] @{context.parent_post.author}: "{context.parent_post.text}"')
        if context.thread_ancestors:
            lines.append("THREAD ANCESTORS (oldest first):")
            for item in context.thread_ancestors:
                lines.append(f'  [{item.post_id}] @{item.author}: "{item.text}"')
        if context.recent_author_posts:
            lines.append("RECENT POSTS BY THIS AUTHOR (background only -- do not assume these share a trade with the current post unless the current post's text itself makes the link clear):")
            for item in context.recent_author_posts:
                lines.append(f'  [{item.post_id}] {item.posted_at.isoformat()}: "{item.text}"')
        if context.open_signal_candidates:
            lines.append("CANDIDATE OPEN SIGNALS FROM THIS AUTHOR (only use one of these exact signal_ids for referenced_trade_id, and only if the post clearly refers to it):")
            for c in context.open_signal_candidates:
                lines.append(f"  signal_id={c.signal_id} instrument={c.instrument} direction={c.direction} type={c.signal_type} opened={c.created_at.isoformat()}")
        if context.glossary_terms:
            lines.append("CONFIRMED AUTHOR-SPECIFIC GLOSSARY:")
            for term, meaning in context.glossary_terms.items():
                lines.append(f'  "{term}" -> {meaning}')
        if not lines:
            lines.append("(no additional context available for this post)")
        return "\n".join(lines)
