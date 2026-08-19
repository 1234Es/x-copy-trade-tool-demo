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


def _safe_json(data: dict) -> str:
    import json

    return json.dumps(data, default=str)
