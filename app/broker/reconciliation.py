"""Compares locally-tracked open trades against OANDA's own view.

A trade that's open locally but no longer open at the broker is expected,
routine behavior -- every trade eventually hits its stop, its target, or gets
closed manually. This module's job is to find out which of those happened
and sync the local record (close_price, close_time, realized_pl,
exit_reason) via the trade's own closing transaction -- not to treat every
closure as an incident.

Only a genuinely *unexplained* mismatch (a trade OANDA has that we don't know
about, or a trade we can't get a clean CLOSED state for) trips the circuit
breaker (Phase 9's "broker-state reconciliation" / Phase 11 audit trail).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.broker.base_broker import BaseBroker
from app.risk.risk_manager import RiskManager
from app.storage.repository import Repository

_EXIT_REASON_LABELS = {
    "STOP_LOSS_ORDER": "stop_loss",
    "TAKE_PROFIT_ORDER": "take_profit",
    "MARKET_ORDER_TRADE_CLOSE": "manual_close",
    "MARKET_ORDER_POSITION_CLOSEOUT": "position_closeout",
    "MARKET_ORDER_MARGIN_CLOSEOUT": "margin_closeout",
    "MARKET_ORDER_DELAYED_TRADE_CLOSE": "delayed_close",
}


@dataclass
class ReconciliationSummary:
    closed_synced: list[str] = field(default_factory=list)
    unexplained: list[str] = field(default_factory=list)
    open_at_broker_not_local: list[str] = field(default_factory=list)

    @property
    def has_unexplained_mismatch(self) -> bool:
        return bool(self.unexplained or self.open_at_broker_not_local)


class Reconciler:
    def __init__(self, broker: BaseBroker, repository: Repository):
        self.broker = broker
        self.repository = repository

    def reconcile(self, risk_manager: RiskManager | None = None, now: datetime | None = None) -> ReconciliationSummary:
        now = now or datetime.now(timezone.utc)
        broker_trade_ids = {t["id"] for t in self.broker.get_open_trades()}
        local_open = self.repository.get_open_trades()
        local_by_id = {t["oanda_trade_id"]: t for t in local_open}
        local_trade_ids = set(local_by_id)

        summary = ReconciliationSummary(
            open_at_broker_not_local=sorted(broker_trade_ids - local_trade_ids)
        )

        for trade_id in sorted(local_trade_ids - broker_trade_ids):
            trade = self.broker.get_trade(trade_id)
            if trade is None or trade.get("state") != "CLOSED":
                summary.unexplained.append(trade_id)
                continue

            exit_reason = self._resolve_exit_reason(trade)
            realized_pl = float(trade["realizedPL"])
            self.repository.close_trade(
                oanda_trade_id=trade_id,
                close_price=float(trade["averageClosePrice"]),
                close_time=_parse_oanda_time(trade["closeTime"]),
                realized_pl=realized_pl,
                exit_reason=exit_reason,
            )
            if risk_manager is not None:
                risk_manager.record_trade_closed(local_by_id[trade_id]["instrument"], realized_pl, now)
            summary.closed_synced.append(trade_id)

        if summary.has_unexplained_mismatch:
            discrepancy = {
                "open_at_broker_not_local": summary.open_at_broker_not_local,
                "unexplained_locally_not_broker": summary.unexplained,
            }
            self.repository.insert_reconciliation_log(
                now, {"local_trade_ids": sorted(local_trade_ids)}, {"broker_trade_ids": sorted(broker_trade_ids)}, discrepancy
            )
            self.repository.insert_circuit_breaker_event("reconciliation_failure", discrepancy, now)

        return summary

    def _resolve_exit_reason(self, trade: dict) -> str:
        closing_ids = trade.get("closingTransactionIDs") or []
        if not closing_ids:
            return "unknown"
        txn = self.broker.get_transaction(closing_ids[-1])
        if txn is None:
            return "unknown"
        reason = txn.get("reason", "unknown")
        return _EXIT_REASON_LABELS.get(reason, reason.lower())


def _parse_oanda_time(value: str) -> datetime:
    # OANDA timestamps are RFC3339 with nanosecond precision (e.g.
    # "2026-07-14T13:05:00.123456789Z"), which Python's fromisoformat can't
    # parse directly (max 6 fractional digits) -- truncate to microseconds.
    if "." in value:
        head, frac_and_zone = value.split(".", 1)
        frac = frac_and_zone.rstrip("Z")[:6]
        value = f"{head}.{frac}+00:00"
    else:
        value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)
