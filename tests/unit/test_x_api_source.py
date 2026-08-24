from unittest.mock import MagicMock

import pytest

from app.sources.x_api_source import XApiCreditsDepletedError, XApiError, XApiSource


def _response(status_code: int, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = text
    return resp


def test_402_raises_credits_depleted_not_generic_x_api_error():
    # Regression: a plain XApiError would have been swallowed by main.py's
    # broad "one bad poll must not kill the loop" except block same as any
    # other failure, giving no way to tell "billing account is out of
    # credits" apart from a transient network blip in the logs or dashboard.
    source = XApiSource(bearer_token="t")
    source._session.get = MagicMock(
        return_value=_response(402, text='{"detail":"credits depleted","status":402}')
    )
    with pytest.raises(XApiCreditsDepletedError):
        source._resolve_user_id("waltervannelli")


def test_402_is_not_retried():
    source = XApiSource(bearer_token="t")
    mock_get = MagicMock(return_value=_response(402, text="credits depleted"))
    source._session.get = mock_get
    with pytest.raises(XApiCreditsDepletedError):
        source._resolve_user_id("waltervannelli")
    assert mock_get.call_count == 1


def test_other_4xx_still_raises_plain_x_api_error():
    source = XApiSource(bearer_token="t")
    source._session.get = MagicMock(return_value=_response(404, text="not found"))
    with pytest.raises(XApiError) as exc_info:
        source._resolve_user_id("waltervannelli")
    assert not isinstance(exc_info.value, XApiCreditsDepletedError)
