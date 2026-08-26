"""Covers POST /api/trades/{id}/close -- in particular that the operator
gate is enforced server-side, not merely by disabling a button.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.auth import AuthState
from app.broker.order_manager import OrderManager
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.risk_manager import RiskManager
from app.storage.database import create_db_engine
from app.storage.repository import Repository

NOW = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
PASSWORD = "operator-password"


def _context(broker: MagicMock, operator_password: str = PASSWORD):
    repository = Repository(create_db_engine("sqlite:///:memory:"))
    settings = MagicMock()
    settings.operator_password = operator_password
    settings.secure_cookies = False
    settings.app_mode = "practice_auto"
    settings.oanda_api_token = "token"
    settings.openai_api_key = "key"
    settings.x_bearer_token = "bearer"

    context = MagicMock()
    context.settings = settings
    context.repository = repository
    context.broker = broker
    context.auth_state = AuthState()
    context.order_manager = OrderManager(broker, repository)
    context.risk_manager = RiskManager({"cooldown_after_loss_minutes": 60}, CircuitBreaker())
    return context


def _seed_open_trade(repository: Repository, trade_id: str = "648") -> None:
    repository.insert_order(
        {
            "order_id": f"o-{trade_id}", "signal_id": f"s-{trade_id}", "oanda_order_id": None,
            "instrument": "EUR_USD", "units": -21025, "status": "filled",
            "submitted_at": NOW, "broker_response_json": None,
        }
    )
    repository.insert_trade(
        {
            "oanda_trade_id": trade_id, "order_id": f"o-{trade_id}", "instrument": "EUR_USD",
            "direction": "short", "open_price": 1.16649, "close_price": None, "open_time": NOW,
            "close_time": None, "realized_pl": None, "exit_reason": None,
        }
    )


@pytest.fixture
def broker() -> MagicMock:
    broker = MagicMock()
    broker.close_trade.return_value = {"orderFillTransaction": {"price": "1.16500", "pl": "30.02"}}
    return broker


def _login(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def test_close_is_refused_when_not_logged_in(broker):
    context = _context(broker)
    _seed_open_trade(context.repository)
    client = TestClient(create_app(context))

    response = client.post("/api/trades/648/close")

    assert response.status_code == 401
    # The server must refuse the action itself -- a disabled button in the
    # dashboard is a courtesy to the user, never the control.
    broker.close_trade.assert_not_called()
    assert context.repository.get_open_trades()[0]["close_time"] is None


def test_close_succeeds_for_a_logged_in_operator(broker):
    context = _context(broker)
    _seed_open_trade(context.repository)
    client = TestClient(create_app(context))
    _login(client)

    response = client.post("/api/trades/648/close")

    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    broker.close_trade.assert_called_once_with("648")
    assert context.repository.get_open_trades() == []


def test_closed_trade_is_recorded_as_a_manual_close(broker):
    context = _context(broker)
    _seed_open_trade(context.repository)
    client = TestClient(create_app(context))
    _login(client)

    client.post("/api/trades/648/close")

    trade = context.repository.get_all_trades()[0]
    # Distinguishable from a pipeline-decided close when reading back what
    # actually happened to the position.
    assert trade["exit_reason"] == "manual_close_by_operator"
    assert trade["close_price"] == 1.16500
    assert trade["realized_pl"] == 30.02


def test_manual_close_of_a_loser_still_feeds_risk_state(broker):
    broker.close_trade.return_value = {"orderFillTransaction": {"price": "1.17000", "pl": "-45.00"}}
    context = _context(broker)
    _seed_open_trade(context.repository)
    client = TestClient(create_app(context))
    _login(client)

    client.post("/api/trades/648/close")

    # Closing by hand must not be a way to sidestep the consecutive-loss
    # counter and per-instrument cooldown a losing trade would otherwise
    # trigger.
    assert context.risk_manager._consecutive_losses == 1
    assert "EUR_USD" in context.risk_manager._last_loss_time_by_instrument


def test_closing_an_unknown_trade_is_a_404(broker):
    context = _context(broker)
    client = TestClient(create_app(context))
    _login(client)

    response = client.post("/api/trades/does-not-exist/close")

    assert response.status_code == 404
    broker.close_trade.assert_not_called()


def test_broker_refusal_surfaces_as_an_error_not_a_false_success(broker):
    broker.close_trade.return_value = {"orderRejectTransaction": {"rejectReason": "MARKET_HALTED"}}
    context = _context(broker)
    _seed_open_trade(context.repository)
    client = TestClient(create_app(context))
    _login(client)

    response = client.post("/api/trades/648/close")

    assert response.status_code == 502
    assert "MARKET_HALTED" in response.json()["detail"]
    # The local record must not claim the position closed when it didn't.
    assert context.repository.get_open_trades()[0]["close_time"] is None


def test_close_is_open_when_no_operator_password_is_configured(broker):
    # Local-dev default: OPERATOR_PASSWORD unset means every write route is
    # open, exactly as before operator auth existed.
    context = _context(broker, operator_password="")
    _seed_open_trade(context.repository)
    client = TestClient(create_app(context))

    response = client.post("/api/trades/648/close")

    assert response.status_code == 200
    broker.close_trade.assert_called_once_with("648")
