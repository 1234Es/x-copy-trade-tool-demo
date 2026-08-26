"""Idempotent order submission -- the only place a validated, risk-approved
signal actually becomes a broker call. Duplicate-order prevention lives
here: a signal_id that already has an order row never gets submitted
twice, even if called again after a crash/restart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.broker.base_broker import BaseBroker, InstrumentMetadata, OrderResult
from app.nlp.schemas import Direction, TradeSignal
from app.storage.repository import Repository


@dataclass(frozen=True)
class OrderSubmissionOutcome:
    submitted: bool
    result: OrderResult | None
    skipped_reason: str | None


@dataclass(frozen=True)
class CloseOutcome:
    success: bool
    realized_pl: float | None
    close_price: float | None
    rejection_reason: str | None


@dataclass(frozen=True)
class ModifyOutcome:
    success: bool
    rejection_reason: str | None


class OrderManager:
    def __init__(self, broker: BaseBroker, repository: Repository):
        self.broker = broker
        self.repository = repository

    def submit(
        self,
        signal: TradeSignal,
        signal_id: str,
        units: int,
        stop_loss_price: float,
        take_profit_price: float | None,
        limit_or_stop_price: float | None,
        oanda_instrument: str,
    ) -> OrderSubmissionOutcome:
        # client_order_id is deliberately post_id+instrument, NOT signal_id --
        # it's the dedup/idempotency key (stable across a crash/restart that
        # reprocesses the same post, which would generate a *different*
        # random signal_id each time), also sent to OANDA as
        # clientExtensions.id. orders.signal_id below is a separate concern:
        # the real foreign key back to the signals table, needed so open
        # positions can be attributed to their actual source account (see
        # repository.get_open_trades_with_source()) -- these two were
        # previously conflated (signal_id column was set to client_order_id),
        # which meant that join could never resolve a real author for any trade.
        client_order_id = f"{signal.post_id}:{oanda_instrument}"
        if self.repository.has_order_for_signal(client_order_id):
            return OrderSubmissionOutcome(False, None, "duplicate_order_for_signal")
        signed_units = units if signal.direction == Direction.LONG else -units
        order_type = signal.order_type.value if signal.order_type else "market"

        result = self.broker.submit_order(
            client_order_id=client_order_id,
            instrument=oanda_instrument,
            units=signed_units,
            order_type=order_type,
            price=limit_or_stop_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )

        now = datetime.now(timezone.utc)
        self.repository.insert_order(
            {
                "order_id": client_order_id,
                "signal_id": signal_id,
                "oanda_order_id": result.oanda_order_id,
                "instrument": oanda_instrument,
                "units": signed_units,
                "status": "filled" if result.success else "rejected",
                "submitted_at": now,
                "broker_response_json": _safe_json(result.raw_response),
            }
        )

        if result.success and result.oanda_trade_id:
            self.repository.insert_trade(
                {
                    "oanda_trade_id": result.oanda_trade_id,
                    "order_id": client_order_id,
                    "instrument": oanda_instrument,
                    "direction": signal.direction.value if signal.direction else "unknown",
                    "open_price": result.fill_price,
                    "close_price": None,
                    "open_time": now,
                    "close_time": None,
                    "realized_pl": None,
                    "exit_reason": None,
                }
            )

        return OrderSubmissionOutcome(True, result, None)

    def close_position(
        self, oanda_trade_id: str, now: datetime, exit_reason: str = "signal_full_close"
    ) -> CloseOutcome:
        """Closes an existing OANDA trade in full, in response to a
        full_close signal. Unlike submit(), there's no client-side
        idempotency key to dedup on here -- the caller (execution_engine)
        already resolves the trade via get_open_trade_for_signal()
        immediately beforehand, and a post can only ever be processed once
        (raw_posts.post_id is the dedup key), so a double-close of the same
        signal isn't reachable in normal operation.

        `exit_reason` is recorded on the trade so the dashboard can tell a
        close the pipeline decided on apart from one an operator clicked
        (see /api/trades/{id}/close) -- they are very different things when
        reading back what happened to a position."""
        try:
            response = self.broker.close_trade(oanda_trade_id)
        except Exception as exc:  # noqa: BLE001 -- a broker/network error must not crash the pipeline
            return CloseOutcome(False, None, None, f"broker_error:{exc}")

        if "orderRejectTransaction" in response:
            reason = response["orderRejectTransaction"].get("rejectReason", "unknown_rejection")
            return CloseOutcome(False, None, None, reason)

        fill = response.get("orderFillTransaction")
        if fill is None:
            return CloseOutcome(False, None, None, "no_fill_transaction_in_close_response")

        close_price = float(fill["price"])
        realized_pl = float(fill.get("pl", 0.0))
        self.repository.close_trade(
            oanda_trade_id=oanda_trade_id,
            close_price=close_price,
            close_time=now,
            realized_pl=realized_pl,
            exit_reason=exit_reason,
        )
        return CloseOutcome(True, realized_pl, close_price, None)

    def modify_position(
        self, oanda_trade_id: str, stop_loss_price: float | None, take_profit_price: float | None
    ) -> ModifyOutcome:
        """Moves the stop-loss and/or take-profit on an existing OANDA trade,
        in response to an update_stop/update_target signal."""
        try:
            response = self.broker.modify_trade(oanda_trade_id, stop_loss_price, take_profit_price)
        except Exception as exc:  # noqa: BLE001 -- a broker/network error must not crash the pipeline
            return ModifyOutcome(False, f"broker_error:{exc}")

        reject_keys = [k for k in response if k.endswith("RejectTransaction")]
        if reject_keys:
            return ModifyOutcome(False, ",".join(reject_keys))
        return ModifyOutcome(True, None)


def _safe_json(data: dict) -> str:
    import json

    return json.dumps(data, default=str)
