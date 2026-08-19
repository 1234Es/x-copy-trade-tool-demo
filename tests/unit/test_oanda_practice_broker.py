from unittest.mock import MagicMock

from app.broker.base_broker import InstrumentMetadata
from app.broker.oanda_practice import OandaPracticeBroker, OandaPracticeConfig

METADATA = InstrumentMetadata(
    name="XAU_USD", pip_location=-2, display_precision=3, minimum_trade_size=1, margin_rate=0.0333, trade_units_precision=0
)


def _broker() -> OandaPracticeBroker:
    broker = OandaPracticeBroker(OandaPracticeConfig(api_token="t", account_id="a"), environment="practice")
    broker._instrument_cache["XAU_USD"] = METADATA
    return broker


def test_market_order_fill_reports_success_with_trade_id():
    broker = _broker()
    broker._request = MagicMock(
        return_value={
            "orderFillTransaction": {
                "price": "4012.140", "orderID": "590", "tradeOpened": {"tradeID": "591"},
            }
        }
    )
    result = broker.submit_order("post:XAU_USD", "XAU_USD", -4, "market", None, 4020.0, 4000.0)
    assert result.success
    assert result.oanda_trade_id == "591"


def test_market_order_cancelled_by_broker_is_reported_as_failure_not_success():
    # Regression test: a FOK market order that OANDA cancels (e.g. because
    # the attached stop-loss is already on the wrong side of the actual
    # fill price -- STOP_LOSS_ON_FILL_LOSS) has no orderFillTransaction.
    # This used to fall through to the "resting limit/stop order" branch
    # and get reported as success=True with no trade, so a cancelled order
    # silently looked identical to a filled one everywhere downstream
    # (orders.status="filled", "order filled" alert, risk_manager's trade
    # counters) even though no position ever opened. See chat log 2026-07-21.
    broker = _broker()
    broker._request = MagicMock(
        return_value={
            "orderCreateTransaction": {"id": "606", "type": "MARKET_ORDER"},
            "orderCancelTransaction": {"id": "607", "reason": "STOP_LOSS_ON_FILL_LOSS"},
        }
    )
    result = broker.submit_order("post:XAU_USD", "XAU_USD", -4, "market", None, 4013.66, 4012.66)
    assert not result.success
    assert result.oanda_trade_id is None
    assert result.rejection_reason == "STOP_LOSS_ON_FILL_LOSS"


def test_resting_limit_order_with_no_fill_yet_is_still_success():
    # The legitimate case the old code was trying to handle: a LIMIT/STOP
    # order that hasn't triggered yet has no orderFillTransaction AND no
    # orderCancelTransaction -- it's just resting, and that's fine.
    broker = _broker()
    broker._request = MagicMock(return_value={"orderCreateTransaction": {"id": "606", "type": "LIMIT_ORDER"}})
    result = broker.submit_order("post:XAU_USD", "XAU_USD", -4, "limit", 4020.0, 4030.0, 4000.0)
    assert result.success
    assert result.oanda_order_id == "606"
    assert result.oanda_trade_id is None
