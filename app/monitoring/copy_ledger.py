"""Copy-accuracy ledger: for each trade we took, what the source did vs what we got.

The source reports his own result on every close, in a very regular form:

    "eur/usd closed short from 1.1489 at 1.1470 +19 pips"

so the ledger can answer the question the rest of the system can't: does
copying him actually work, and if not, where does the gap come from --
a worse entry (delay, or the market moving before we filled), or a worse
exit (our fallback stop or target firing before he closed)?

Close posts are parsed with a deterministic pattern rather than by asking
the extraction model for extra fields. That keeps the live trading path
untouched (a prompt change affects every signal), works retroactively on
already-stored posts, and returns the same numbers for the same post every
time at no per-post cost.

Pairing uses only the source's own numbers: his "from 1.1489" on the close
matches his own "sold 2.5 lots at 1.1489" on the open. Our fill prices play
no part in the match, so slippage can't stop a pair from being found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.storage.repository import Repository

# How close the source's stated close-from price must be to his stated entry
# for the two posts to count as the same trade, in pips. Tight on purpose: a
# wrong pairing is worse than none, since it would put a false number in a
# report whose whole purpose is to be trusted.
PAIRING_TOLERANCE_PIPS = 1.0

_CLOSE_PATTERN = re.compile(
    r"\b(?:closed|take\s+off|reduced)\b"            # the close verb
    r"[^0-9]*?(?:\d+(?:\.\d+)?\s*lo?ts?\b)?[^0-9]*?"  # optional size, e.g. "2.5 lots", "5 lots short"
    r"\bfr\w{0,2}m\s+(average\s+)?(\d+\.\d+)"         # from [average] OPEN  (tolerates "frfom")
    r"\s*(?:&amp;|&)?\s*(?:closed\s+)?at\s*(\d+\.\d+)"  # [& closed] at CLOSE (tolerates "1.1580at")
    r"(?:\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*pips?)?",      # optional stated result, e.g. "+19 pips"
    re.IGNORECASE,
)
# Timezone-aware bounds: every stored timestamp is UTC-aware, and comparing
# one against the naive datetime.min/max raises TypeError.
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)
_LATEST = datetime.max.replace(tzinfo=timezone.utc)

_DIRECTION_PATTERN = re.compile(r"\b(short|long)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedClose:
    open_price: float
    close_price: float
    is_average: bool  # "from average X": his whole blended position, not one entry
    stated_pips: float | None  # sign as written; unsigned values are left positive
    direction: str | None


def parse_close(text: str) -> ParsedClose | None:
    """The source's own account of a close, or None if the post isn't one.

    Only the FIRST "from X at Y" is taken. Later numbers in the same post
    ("daily total -211 pips", "ratio -186 pips") are running totals, not
    this trade's result."""
    match = _CLOSE_PATTERN.search(text)
    if match is None:
        return None
    average, open_s, close_s, sign, pips_s = match.groups()
    stated = None
    if pips_s is not None:
        stated = float(pips_s) * (-1 if sign == "-" else 1)
    direction = _DIRECTION_PATTERN.search(text)
    return ParsedClose(
        open_price=float(open_s), close_price=float(close_s), is_average=average is not None,
        stated_pips=stated, direction=direction.group(1).lower() if direction else None,
    )


def default_pip_size(instrument: str) -> float:
    """Fallback when the broker can't be asked: JPY pairs and gold quote to
    two decimals on this account, everything else to four."""
    return 0.01 if ("JPY" in instrument or instrument.startswith("XAU")) else 0.0001


def pips_between(open_price: float, close_price: float, direction: str, pip_size: float) -> float:
    """Profit in pips for `direction` -- positive is a gain."""
    move = (close_price - open_price) if direction == "long" else (open_price - close_price)
    return round(move / pip_size, 1)


@dataclass(frozen=True)
class LedgerRow:
    oanda_trade_id: str
    instrument: str
    direction: str
    status: str  # "open" | "closed"
    source_post_id: str | None
    source_posted_at: datetime | None
    # entry
    source_entry: float | None
    our_entry: float | None
    entry_slippage_pips: float | None  # positive = we got a WORSE price than he did
    entry_delay_seconds: float | None  # his post -> our fill
    # exit
    our_exit: float | None
    our_exit_reason: str | None
    our_pips: float | None
    our_pl: float | None
    source_close_post_id: str | None
    source_exit: float | None
    source_pips: float | None  # from his own prices, not his stated number
    source_stated_pips: float | None
    pips_gap: float | None  # ours minus his; negative = we did worse


@dataclass(frozen=True)
class LedgerSummary:
    trades: int
    paired: int  # trades with a matching source close found
    avg_entry_slippage_pips: float | None
    avg_entry_delay_seconds: float | None
    our_total_pips: float  # over paired, closed trades
    source_total_pips: float  # same trades, his result
    stopped_out_while_source_profited: int  # our stop fired, he closed green


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def build_ledger(
    repository: Repository, pip_size_for: Callable[[str], float] = default_pip_size
) -> tuple[list[LedgerRow], LedgerSummary]:
    trades = repository.get_trades_with_source_signals()

    # Every candidate close post per author, oldest first, parsed once.
    closes_by_author: dict[str, list[tuple[dict, ParsedClose]]] = {}
    for author in {t["author"] for t in trades if t["author"]}:
        parsed = []
        for post in reversed(repository.get_recent_posts_by_author(author, before=_LATEST, limit=100_000)):
            close = parse_close(post["text"])
            if close is not None and not close.is_average:
                parsed.append((post, close))
        closes_by_author[author] = parsed

    used_close_posts: set[str] = set()
    rows: list[LedgerRow] = []
    for t in sorted(trades, key=lambda t: t["open_time"] or _EARLIEST):
        pip = pip_size_for(t["instrument"])
        direction = t["direction"]
        source_entry = t["source_entry_price"]

        slippage = None
        if source_entry is not None and t["open_price"] is not None:
            # How far price had already moved in the trade's favour between
            # his entry and ours -- profit he has that we never will. Positive
            # means worse for us: paying more on a long, receiving less on a
            # short. (Not negated: an earlier draft negated this and flipped
            # every figure; the sign is pinned by a test.)
            slippage = pips_between(source_entry, t["open_price"], direction, pip)
        delay = None
        if t["open_time"] and t["source_posted_at"]:
            delay = (t["open_time"] - t["source_posted_at"]).total_seconds()

        our_pips = None
        if t["close_price"] is not None and t["open_price"] is not None:
            our_pips = pips_between(t["open_price"], t["close_price"], direction, pip)

        # His close for this trade: first unused close after his open whose
        # "from" price is his own stated entry.
        match = None
        if source_entry is not None:
            for post, close in closes_by_author.get(t["author"], []):
                if post["post_id"] in used_close_posts:
                    continue
                if t["source_posted_at"] and post["posted_at"] <= t["source_posted_at"]:
                    continue
                if abs(close.open_price - source_entry) / pip <= PAIRING_TOLERANCE_PIPS:
                    match = (post, close)
                    used_close_posts.add(post["post_id"])
                    break

        source_pips = source_exit = stated = gap = None
        close_post_id = None
        if match is not None:
            post, close = match
            close_post_id = post["post_id"]
            source_exit = close.close_price
            stated = close.stated_pips
            source_pips = pips_between(close.open_price, close.close_price, direction, pip)
            if our_pips is not None:
                gap = round(our_pips - source_pips, 1)

        rows.append(LedgerRow(
            oanda_trade_id=t["oanda_trade_id"], instrument=t["instrument"], direction=direction,
            status="closed" if t["close_time"] else "open",
            source_post_id=t["source_post_id"], source_posted_at=t["source_posted_at"],
            source_entry=source_entry, our_entry=t["open_price"],
            entry_slippage_pips=slippage, entry_delay_seconds=delay,
            our_exit=t["close_price"], our_exit_reason=t["exit_reason"], our_pips=our_pips,
            our_pl=t["realized_pl"], source_close_post_id=close_post_id, source_exit=source_exit,
            source_pips=source_pips, source_stated_pips=stated, pips_gap=gap,
        ))

    paired_closed = [r for r in rows if r.pips_gap is not None]
    summary = LedgerSummary(
        trades=len(rows),
        paired=sum(1 for r in rows if r.source_close_post_id),
        avg_entry_slippage_pips=_avg([r.entry_slippage_pips for r in rows if r.entry_slippage_pips is not None]),
        avg_entry_delay_seconds=_avg([r.entry_delay_seconds for r in rows if r.entry_delay_seconds is not None]),
        our_total_pips=round(sum(r.our_pips for r in paired_closed), 1),
        source_total_pips=round(sum(r.source_pips for r in paired_closed), 1),
        stopped_out_while_source_profited=sum(
            1 for r in paired_closed if r.our_exit_reason == "stop_loss" and (r.source_pips or 0) > 0
        ),
    )
    return list(reversed(rows)), summary  # newest first for display
