from datetime import datetime, timedelta, timezone

import pytest

from app.monitoring.metrics import calculate_metrics, period_bounds
from app.storage.database import create_db_engine
from app.storage.repository import Repository

NOW = datetime(2026, 7, 15, 14, 30, 0, tzinfo=timezone.utc)  # Wednesday


@pytest.fixture
def repository() -> Repository:
    return Repository(create_db_engine("sqlite:///:memory:"))


def _seed_trade(repository, trade_id, instrument, open_time, close_time=None, realized_pl=None):
    repository.insert_order(
        {
            "order_id": f"o-{trade_id}", "signal_id": f"o-{trade_id}", "oanda_order_id": f"ord-{trade_id}",
            "instrument": instrument, "units": 1000, "status": "filled",
            "submitted_at": open_time, "broker_response_json": "{}",
        }
    )
    repository.insert_trade(
        {
            "oanda_trade_id": trade_id, "order_id": f"o-{trade_id}", "instrument": instrument, "direction": "long",
            "open_price": 1.10, "close_price": 1.11 if close_time else None, "open_time": open_time,
            "close_time": close_time, "realized_pl": realized_pl, "exit_reason": "take_profit" if close_time else None,
        }
    )


def test_period_bounds_today_starts_at_midnight():
    start, end = period_bounds("today", NOW)
    assert start == NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    assert end == NOW


def test_period_bounds_week_starts_monday():
    start, _ = period_bounds("week", NOW)  # NOW is a Wednesday
    assert start.weekday() == 0
    assert start.date() < NOW.date()


def test_period_bounds_month_starts_first_of_month():
    start, _ = period_bounds("month", NOW)
    assert start.day == 1


def test_period_bounds_all_has_no_start():
    start, _ = period_bounds("all", NOW)
    assert start is None


def test_period_bounds_rejects_unknown_period():
    with pytest.raises(ValueError):
        period_bounds("yesterday", NOW)


def test_calculate_metrics_empty_database(repository):
    m = calculate_metrics(repository, "today", now=NOW)
    assert m.trades_copied == 0
    assert m.closed_trades == 0
    assert m.win_rate is None
    assert m.total_pnl == 0
    assert m.open_trades == 0


def test_calculate_metrics_counts_wins_losses_and_pnl(repository):
    today_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    _seed_trade(repository, "t1", "EUR_USD", today_start, close_time=NOW, realized_pl=50.0)
    _seed_trade(repository, "t2", "EUR_USD", today_start, close_time=NOW, realized_pl=-20.0)
    _seed_trade(repository, "t3", "GBP_USD", today_start, close_time=NOW, realized_pl=10.0)

    m = calculate_metrics(repository, "today", now=NOW)

    assert m.trades_copied == 3
    assert m.closed_trades == 3
    assert m.wins == 2
    assert m.losses == 1
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.total_pnl == 40.0


def test_calculate_metrics_open_trades_not_counted_as_closed(repository):
    today_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    _seed_trade(repository, "t1", "EUR_USD", today_start)  # still open

    m = calculate_metrics(repository, "today", now=NOW)

    assert m.trades_copied == 1
    assert m.closed_trades == 0
    assert m.win_rate is None
    assert m.open_trades == 1


def test_calculate_metrics_excludes_trades_outside_period(repository):
    last_month = NOW.replace(day=1) - timedelta(days=5)
    _seed_trade(repository, "t1", "EUR_USD", last_month, close_time=last_month, realized_pl=100.0)

    m = calculate_metrics(repository, "today", now=NOW)

    assert m.trades_copied == 0
    assert m.closed_trades == 0
    assert m.total_pnl == 0


def test_calculate_metrics_all_period_includes_everything(repository):
    last_month = NOW.replace(day=1) - timedelta(days=5)
    _seed_trade(repository, "t1", "EUR_USD", last_month, close_time=last_month, realized_pl=100.0)

    m = calculate_metrics(repository, "all", now=NOW)

    assert m.trades_copied == 1
    assert m.closed_trades == 1
    assert m.total_pnl == 100.0


def test_calculate_metrics_rejects_unknown_period(repository):
    with pytest.raises(ValueError):
        calculate_metrics(repository, "quarter", now=NOW)
