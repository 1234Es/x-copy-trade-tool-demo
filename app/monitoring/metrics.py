"""Trade performance metrics for the dashboard's Metrics tab.

"Trades copied" counts by when a trade was OPENED; win rate and P/L count by
when a trade CLOSED (that's when the outcome became known) -- a trade opened
in one period can close in a later one, so these two counts are deliberately
independent, not the same trades sliced two ways.

All period boundaries are computed in UTC, matching every other timestamp in
this system (posts, signals, orders). `now` must be passed in as UTC-aware --
this module never assumes the caller's local timezone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.storage.repository import Repository

VALID_PERIODS = ("today", "week", "month", "all")


@dataclass(frozen=True)
class Metrics:
    period: str
    period_start: datetime | None
    period_end: datetime
    trades_copied: int
    closed_trades: int
    wins: int
    losses: int
    win_rate: float | None  # None if no closed trades in period, not 0 -- "no data" isn't "0% wins"
    total_pnl: float
    open_trades: int  # current snapshot, not period-filtered -- "open" isn't a period-scoped concept


def period_bounds(period: str, now: datetime) -> tuple[datetime | None, datetime]:
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "all":
        start = None
    else:
        raise ValueError(f"Unknown period: {period!r}. Valid: {VALID_PERIODS}")
    return start, now


def calculate_metrics(repository: Repository, period: str, now: datetime | None = None) -> Metrics:
    now = now or datetime.now(timezone.utc)
    start, end = period_bounds(period, now)

    all_trades = repository.get_all_trades(limit=100_000)

    trades_copied = sum(1 for t in all_trades if start is None or (t["open_time"] and t["open_time"] >= start))

    closed_in_period = [
        t for t in all_trades if t["close_time"] is not None and (start is None or t["close_time"] >= start)
    ]
    wins = sum(1 for t in closed_in_period if t["realized_pl"] is not None and t["realized_pl"] > 0)
    losses = sum(1 for t in closed_in_period if t["realized_pl"] is not None and t["realized_pl"] < 0)
    win_rate = wins / len(closed_in_period) if closed_in_period else None
    total_pnl = sum(t["realized_pl"] for t in closed_in_period if t["realized_pl"] is not None)

    open_trades = sum(1 for t in all_trades if t["close_time"] is None)

    return Metrics(
        period=period,
        period_start=start,
        period_end=end,
        trades_copied=trades_copied,
        closed_trades=len(closed_in_period),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_pnl=round(total_pnl, 2),
        open_trades=open_trades,
    )
