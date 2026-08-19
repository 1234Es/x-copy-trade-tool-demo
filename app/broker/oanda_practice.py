"""OANDA v20 REST API client, restricted to the practice environment.

Verified against developer.oanda.com (2026-07-13, same facts as the
related oanda-spreadbet-bot project):
  - Practice REST base: https://api-fxpractice.oanda.com
  - Auth: `Authorization: Bearer <personal access token>`.
  - Rate limits: 120 requests/sec, HTTP 429 above that.
  - Order types incl. Market/Limit/Stop; stop-loss/take-profit attach via
    `stopLossOnFill` / `takeProfitOnFill` on the parent order.

This module refuses to construct a client pointed at the live endpoint --
that is a hard-coded restriction, not a configuration option, matching the
brief's "never enable live-account trading in this version."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.broker.base_broker import AccountSnapshot, BaseBroker, InstrumentMetadata, OrderResult, PriceTick

PRACTICE_REST_URL = "https://api-fxpractice.oanda.com"


class OandaApiError(Exception):
    def __init__(self, status_code: int, body: str, url: str):
        self.status_code = status_code
        self.body = body
        self.url = url
        super().__init__(f"OANDA API error {status_code} calling {url}: {body}")


class OandaTransientError(OandaApiError):
    pass


class OandaRateLimitError(OandaApiError):
    pass


class LiveTradingNotSupportedError(Exception):
    pass


@dataclass(frozen=True)
class OandaPracticeConfig:
    api_token: str
    account_id: str
    request_timeout_seconds: float = 10.0


class OandaPracticeBroker(BaseBroker):
    def __init__(self, config: OandaPracticeConfig, environment: str = "practice"):
        if environment != "practice":
            raise LiveTradingNotSupportedError(
                "This tool only supports OANDA_ENVIRONMENT=practice. Live trading has not been "
                "designed or approved -- there is no code path to the live endpoint."
            )
        if not config.api_token or not config.account_id:
            raise ValueError("OANDA_API_TOKEN and OANDA_ACCOUNT_ID must both be set.")
        self.config = config
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {config.api_token}", "Content-Type": "application/json"}
        )
        self._instrument_cache: dict[str, InstrumentMetadata | None] = {}

    @retry(
        retry=retry_if_exception_type((OandaTransientError, requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        wait=wait_exponential(multiplier=1, min=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _request(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> dict[str, Any]:
        url = f"{PRACTICE_REST_URL}{path}"
        response = self._session.request(method, url, params=params, json=json_body, timeout=self.config.request_timeout_seconds)
        if response.status_code == 429:
            raise OandaRateLimitError(response.status_code, response.text, url)
        if 500 <= response.status_code < 600:
            raise OandaTransientError(response.status_code, response.text, url)
        if not response.ok:
            raise OandaApiError(response.status_code, response.text, url)
        return response.json()

    def get_account_snapshot(self) -> AccountSnapshot:
        raw = self._request("GET", f"/v3/accounts/{self.config.account_id}/summary")["account"]
        return AccountSnapshot(
            balance=float(raw["balance"]),
            equity=float(raw["NAV"]),
            margin_available=float(raw["marginAvailable"]),
            margin_used=float(raw["marginUsed"]),
            open_trade_count=int(raw["openTradeCount"]),
            currency=raw["currency"],
        )

    def get_instrument_metadata(self, instrument: str) -> InstrumentMetadata | None:
        if instrument in self._instrument_cache:
            return self._instrument_cache[instrument]
        raw = self._request("GET", f"/v3/accounts/{self.config.account_id}/instruments")["instruments"]
        found: InstrumentMetadata | None = None
        for entry in raw:
            metadata = InstrumentMetadata(
                name=entry["name"],
                pip_location=int(entry["pipLocation"]),
                display_precision=int(entry["displayPrecision"]),
                minimum_trade_size=float(entry["minimumTradeSize"]),
                margin_rate=float(entry["marginRate"]),
                trade_units_precision=int(entry["tradeUnitsPrecision"]),
            )
            self._instrument_cache[entry["name"]] = metadata
            if entry["name"] == instrument:
                found = metadata
        if instrument not in self._instrument_cache:
            self._instrument_cache[instrument] = None
        return found

    def get_pricing(self, instrument: str) -> PriceTick | None:
        raw = self._request(
            "GET", f"/v3/accounts/{self.config.account_id}/pricing", params={"instruments": instrument}
        )
        prices = raw.get("prices", [])
        if not prices:
            return None
        entry = prices[0]
        bids = entry.get("bids", [])
        asks = entry.get("asks", [])
        if not bids or not asks:
            return None
        return PriceTick(
            instrument=instrument,
            bid=float(bids[0]["price"]),
            ask=float(asks[0]["price"]),
            tradeable=entry.get("tradeable", True),
        )

    def submit_order(
        self,
        client_order_id: str,
        instrument: str,
        units: int,
        order_type: str,
        price: float | None,
        stop_loss_price: float,
        take_profit_price: float | None,
    ) -> OrderResult:
        metadata = self.get_instrument_metadata(instrument)
        if metadata is None:
            return OrderResult(False, None, None, None, "instrument_unavailable_on_account", {})

        precision = metadata.display_precision
        payload: dict[str, Any] = {
            "instrument": instrument,
            "units": str(units),
            "clientExtensions": {"id": client_order_id, "tag": "x_copy_trade_tool"},
            "stopLossOnFill": {"price": f"{stop_loss_price:.{precision}f}", "timeInForce": "GTC"},
        }
        if take_profit_price is not None:
            payload["takeProfitOnFill"] = {"price": f"{take_profit_price:.{precision}f}", "timeInForce": "GTC"}

        if order_type == "market":
            payload["type"] = "MARKET"
            payload["timeInForce"] = "FOK"
            payload["positionFill"] = "DEFAULT"
        elif order_type in ("limit", "stop"):
            if price is None:
                return OrderResult(False, None, None, None, "limit_or_stop_order_missing_price", {})
            payload["type"] = "LIMIT" if order_type == "limit" else "STOP"
            payload["price"] = f"{price:.{precision}f}"
            payload["timeInForce"] = "GTC"
            payload["positionFill"] = "DEFAULT"
        else:
            return OrderResult(False, None, None, None, f"unsupported_order_type:{order_type}", {})

        try:
            response = self._request(
                "POST", f"/v3/accounts/{self.config.account_id}/orders", json_body={"order": payload}
            )
        except OandaApiError as exc:
            return OrderResult(False, None, None, None, f"broker_error:{exc.status_code}", {"error": exc.body})

        if "orderRejectTransaction" in response:
            reason = response["orderRejectTransaction"].get("rejectReason", "unknown_rejection")
            return OrderResult(False, None, None, None, reason, response)

        fill = response.get("orderFillTransaction")
        if fill is None:
            cancel_txn = response.get("orderCancelTransaction")
            if cancel_txn is not None:
                # A FOK market order can never legitimately "rest" -- it
                # either fills immediately or OANDA cancels it outright
                # (e.g. STOP_LOSS_ON_FILL_LOSS when a computed stop is
                # already on the wrong side of the fill price,
                # INSUFFICIENT_MARGIN, etc.). This used to fall through to
                # the "pending limit/stop order" branch below and get
                # reported as success=True with no trade -- silently
                # misreporting a cancelled order as a filled one, with
                # order_manager then recording it as "filled" in the orders
                # table and execution_engine alerting "order filled" even
                # though no position ever opened. See chat log 2026-07-21.
                return OrderResult(False, None, None, None, cancel_txn.get("reason", "order_cancelled"), response)
            create_txn = response.get("orderCreateTransaction", {})
            # A pending limit/stop order that hasn't filled yet is not a
            # failure -- it's a legitimate resting order.
            return OrderResult(True, create_txn.get("id"), None, None, None, response)

        trade_opened = fill.get("tradeOpened")
        return OrderResult(
            success=True,
            oanda_order_id=fill.get("orderID"),
            oanda_trade_id=trade_opened.get("tradeID") if trade_opened else None,
            fill_price=float(fill["price"]) if "price" in fill else None,
            rejection_reason=None,
            raw_response=response,
        )

    def get_open_trades(self) -> list[dict[str, Any]]:
        return self._request("GET", f"/v3/accounts/{self.config.account_id}/openTrades").get("trades", [])

    def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/v3/accounts/{self.config.account_id}/trades/{trade_id}").get("trade")
        except OandaApiError:
            return None

    def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/v3/accounts/{self.config.account_id}/transactions/{transaction_id}").get("transaction")
        except OandaApiError:
            return None

    def close_trade(self, trade_id: str, units: str = "ALL") -> dict[str, Any]:
        return self._request(
            "PUT", f"/v3/accounts/{self.config.account_id}/trades/{trade_id}/close", json_body={"units": units}
        )

    def modify_trade(self, trade_id: str, stop_loss_price: float | None, take_profit_price: float | None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if stop_loss_price is not None:
            body["stopLoss"] = {"price": f"{stop_loss_price:.5f}", "timeInForce": "GTC"}
        if take_profit_price is not None:
            body["takeProfit"] = {"price": f"{take_profit_price:.5f}", "timeInForce": "GTC"}
        return self._request(
            "PUT", f"/v3/accounts/{self.config.account_id}/trades/{trade_id}/orders", json_body=body
        )
