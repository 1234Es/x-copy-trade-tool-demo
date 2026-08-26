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
    adopted_from_resting_order: list[str] = field(default_factory=list)

    @property
    def has_unexplained_mismatch(self) -> bool:
        # An adopted trade is explained -- we placed the order that became
        # it -- so it deliberately doesn't count here.
        return bool(self.unexplained or self.open_at_broker_not_local)


class Reconciler:
    def __init__(self, broker: BaseBroker, repository: Repository):
        self.broker = broker
        self.repository = repository

    def reconcile(self, risk_manager: RiskManager | None = None, now: datetime | None = None) -> ReconciliationSummary:
        now = now or datetime.now(timezone.utc)
        broker_trades = self.broker.get_open_trades()
        broker_by_id = {t["id"]: t for t in broker_trades}
        broker_trade_ids = set(broker_by_id)
        local_open = self.repository.get_open_trades()
        local_by_id = {t["oanda_trade_id"]: t for t in local_open}
        local_trade_ids = set(local_by_id)

        summary = ReconciliationSummary()
        # A trade the broker has and we don't is not automatically a
        # mystery: a resting limit/stop order we placed becomes a trade
        # whenever its level is hit, with no call of ours to hang the
        # bookkeeping off. Claim those against the order that produced them
        # before treating anything as an unexplained mismatch -- otherwise
        # every limit order that fills halts trading (see chat log
        # 2026-08-26, trade 653).
        unclaimed_orders = self.repository.get_orders_without_trades()
        for trade_id in sorted(broker_trade_ids - local_trade_ids):
            order = self._match_resting_order(broker_by_id[trade_id], unclaimed_orders)
            if order is None:
                summary.open_at_broker_not_local.append(trade_id)
                continue
            self._record_adopted_trade(trade_id, broker_by_id[trade_id], order)
            unclaimed_orders.remove(order)
            summary.adopted_from_resting_order.append(trade_id)

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
            # Actually halt trading, not just write a row about it. These two
            # repository calls only record that a mismatch happened -- for a
            # long time nothing tripped the breaker itself, so a dashboard
            # showing "circuit breaker: clear" was not evidence local and
            # broker state agreed, and the system would happily keep opening
            # new positions while its own view of the account was known-wrong
            # (DESIGN.md Section 5: "auto-shutdown after reconciliation
            # failure"). No cooldown -- an operator must look at the account
            # and clear this deliberately.
            if risk_manager is not None:
                risk_manager.record_reconciliation_mismatch(
                    now,
                    f"open_at_broker_not_local={summary.open_at_broker_not_local or 'none'}; "
                    f"unexplained_locally_not_broker={summary.unexplained or 'none'}",
                )

        return summary

    @staticmethod
    def _match_resting_order(broker_trade: dict, unclaimed_orders: list[dict]) -> dict | None:
        """The order that became this trade, or None if we didn't place it.

        Matched on instrument AND exact signed units: units are computed by
        position sizing to a specific integer, so an accidental match with an
        unrelated position placed by hand in the OANDA UI is implausible
        without it having the identical instrument and size. Deliberately
        strict -- adopting the wrong trade would attribute a position to a
        signal that didn't produce it, and silently satisfy a reconciliation
        check that exists precisely to catch that. When in doubt this
        returns None and the trade is reported as a mismatch, which is the
        safe direction.
        """
        try:
            units = int(float(broker_trade["currentUnits"]))
        except (KeyError, TypeError, ValueError):
            return None
        instrument = broker_trade.get("instrument")
        for order in unclaimed_orders:
            if order["instrument"] == instrument and order["units"] == units:
                return order
        return None

    def _record_adopted_trade(self, trade_id: str, broker_trade: dict, order: dict) -> None:
        self.repository.insert_trade(
            {
                "oanda_trade_id": trade_id,
                "order_id": order["order_id"],
                "instrument": order["instrument"],
                "direction": "long" if order["units"] > 0 else "short",
                "open_price": float(broker_trade["price"]) if broker_trade.get("price") else None,
                "close_price": None,
                "open_time": _parse_oanda_time(broker_trade["openTime"]) if broker_trade.get("openTime") else None,
                "close_time": None,
                "realized_pl": None,
                "exit_reason": None,
            }
        )

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
