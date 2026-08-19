"""Abstract broker interface. `oanda_practice.py` is the only concrete
implementation in this version -- kept behind an interface anyway so the
execution engine and tests can depend on a contract rather than a specific
broker's SDK shape.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InstrumentMetadata:
    name: str
    pip_location: int
    display_precision: int
    minimum_trade_size: float
    margin_rate: float
    trade_units_precision: int

    @property
    def pip_size(self) -> float:
        return 10**self.pip_location


@dataclass(frozen=True)
class PriceTick:
    instrument: str
    bid: float
    ask: float
    tradeable: bool


@dataclass(frozen=True)
class AccountSnapshot:
    balance: float
    equity: float
    margin_available: float
    margin_used: float
    open_trade_count: int
    currency: str


@dataclass(frozen=True)
class OrderResult:
    success: bool
    oanda_order_id: str | None
    oanda_trade_id: str | None
    fill_price: float | None
    rejection_reason: str | None
    raw_response: dict[str, Any]


class BaseBroker(ABC):
    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshot: ...

    @abstractmethod
    def get_instrument_metadata(self, instrument: str) -> InstrumentMetadata | None:
        """Returns None if the instrument is not available on this account
        (e.g. crypto CFDs on a UK retail account) -- callers must treat
        None as "cannot trade this," never assume defaults."""
        ...

    @abstractmethod
    def get_pricing(self, instrument: str) -> PriceTick | None: ...

    @abstractmethod
    def submit_order(
        self,
        client_order_id: str,
        instrument: str,
        units: int,
        order_type: str,
        price: float | None,
        stop_loss_price: float,
        take_profit_price: float | None,
    ) -> OrderResult: ...

    @abstractmethod
    def get_open_trades(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        """Full current state of one trade (open or closed), or None if it
        doesn't exist / can't be fetched. Used by the reconciliation loop to
        find out what happened to a trade that's no longer in
        get_open_trades() -- distinguishing a normal close (state=CLOSED,
        with closeTime/averageClosePrice/realizedPL) from something genuinely
        unexplained."""
        ...

    @abstractmethod
    def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        """Used to resolve *why* a trade closed (stop loss / take profit /
        manual close) via the closing transaction's `reason` field."""
        ...

    @abstractmethod
    def close_trade(self, trade_id: str, units: str = "ALL") -> dict[str, Any]: ...

    @abstractmethod
    def modify_trade(self, trade_id: str, stop_loss_price: float | None, take_profit_price: float | None) -> dict[str, Any]: ...


class NullBroker(BaseBroker):
    """Used when OANDA credentials aren't configured yet. Every method
    reports "unavailable" rather than raising, so the rest of the pipeline
    (classification, extraction, observe-mode logging) still works for
    testing the NLP side before OANDA is hooked up -- signal validation
    will correctly reject any would-be order with a clear reason instead
    of crashing.
    """

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(0.0, 0.0, 0.0, 0.0, 0, "N/A")

    def get_instrument_metadata(self, instrument: str) -> InstrumentMetadata | None:
        return None

    def get_pricing(self, instrument: str) -> PriceTick | None:
        return None

    def submit_order(self, *args: Any, **kwargs: Any) -> OrderResult:
        return OrderResult(False, None, None, None, "oanda_not_configured", {})

    def get_open_trades(self) -> list[dict[str, Any]]:
        return []

    def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        return None

    def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        return None

    def close_trade(self, trade_id: str, units: str = "ALL") -> dict[str, Any]:
        return {}

    def modify_trade(self, trade_id: str, stop_loss_price: float | None, take_profit_price: float | None) -> dict[str, Any]:
        return {}
