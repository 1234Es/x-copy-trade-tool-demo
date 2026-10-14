import json
from datetime import datetime, timedelta, timezone

import pytest

from app.monitoring.rejection_report import build_rejection_report, normalise_reason
from app.storage.database import create_db_engine
from app.storage.repository import Repository

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo() -> Repository:
    return Repository(create_db_engine("sqlite:///:memory:"))


def _seed(repo: Repository, n: int, *, status: str, reason: str | None = None, signal_type: str = "new_trade",
          with_order: bool = False, created_at: datetime = NOW, text: str = "Eur/usd sold 2.5 lots at 1.1493") -> None:
    post_id = f"p{n}"
    repo.insert_raw_post({
        "post_id": post_id, "author": "waltervannelli", "text": text, "posted_at": created_at, "source": "x_api",
        "reply_to_id": None, "quoted_post_id": None, "is_repost": False, "media_json": "[]",
        "ingested_at": created_at, "raw_payload_json": None, "superseded_by_post_id": None,
    })
    repo.insert_signal({
        "signal_id": f"s{n}", "post_id": post_id, "author": "waltervannelli", "signal_type": signal_type,
        "instrument": "eur/usd", "direction": "short", "order_type": "market", "entry_price": None,
        "entry_zone_low": None, "entry_zone_high": None, "stop_loss": None, "take_profit_json": "[]",
        "timeframe": None, "valid_until": None, "referenced_trade_id": None, "confidence": 0.9,
        "evidence_json": "[]", "assumptions_json": "[]", "missing_fields_json": "[]",
        "requires_human_review": True, "reasoning_summary": None, "openai_request_id": None,
        "validation_status": status, "rejection_reason": reason, "created_at": created_at,
    })
    if with_order:
        repo.insert_order({
            "order_id": f"o{n}", "signal_id": f"s{n}", "oanda_order_id": "1", "instrument": "EUR_USD",
            "units": -21000, "status": "filled", "submitted_at": created_at, "broker_response_json": None,
        })


def _bucket(report, key):
    return next((b for b in report.buckets if b.key == key), None)


def test_executed_signal_is_counted_and_not_bucketed(repo):
    _seed(repo, 1, status="approved", with_order=True)
    report = build_rejection_report(repo, "all", now=NOW)
    assert report.executed == 1
    assert report.buckets == []


def test_approved_signal_with_no_order_is_flagged(repo):
    # The Sept 17-21 failure: auto-approve was off, approved signals became
    # proposals and expired, and validation_status still read "approved".
    _seed(repo, 1, status="approved", with_order=False)
    report = build_rejection_report(repo, "all", now=NOW)

    bucket = _bucket(report, "approved_no_order")
    assert bucket is not None and bucket.count == 1
    assert bucket.needs_attention
    assert "auto-approve" in bucket.hint
    assert report.executed == 0


def test_approved_close_without_an_order_is_not_misreported(repo):
    # A full close updates the existing trade and writes no order row; it
    # must not be read as a missed signal.
    _seed(repo, 1, status="approved", signal_type="full_close", with_order=False)
    report = build_rejection_report(repo, "all", now=NOW)
    assert report.handled_management == 1
    assert _bucket(report, "approved_no_order") is None


def test_like_reasons_group_together_but_keep_their_detail(repo):
    _seed(repo, 1, status="rejected", reason="signal_too_stale:46.0min")
    _seed(repo, 2, status="rejected", reason="signal_too_stale:1350.9min")
    report = build_rejection_report(repo, "all", now=NOW)

    bucket = _bucket(report, "signal_too_stale")
    assert bucket.count == 2
    assert {e.detail for e in bucket.examples} == {"signal_too_stale:46.0min", "signal_too_stale:1350.9min"}


def test_circuit_breaker_buckets_keep_which_breaker_tripped(repo):
    _seed(repo, 1, status="rejected", reason="circuit_breaker_tripped:reconciliation_mismatch")
    _seed(repo, 2, status="rejected", reason="circuit_breaker_tripped:api_error_rate")
    report = build_rejection_report(repo, "all", now=NOW)
    assert _bucket(report, "circuit_breaker_tripped:reconciliation_mismatch").count == 1
    assert _bucket(report, "circuit_breaker_tripped:api_error_rate").count == 1
    assert "reconciliation_mismatch" in _bucket(report, "circuit_breaker_tripped:reconciliation_mismatch").label


def test_signal_that_never_finished_processing_is_flagged(repo):
    _seed(repo, 1, status="pending")
    bucket = _bucket(build_rejection_report(repo, "all", now=NOW), "never_finished")
    assert bucket.count == 1 and bucket.needs_attention


def test_too_ambiguous_posts_appear_even_though_they_never_became_signals(repo):
    repo.insert_raw_post({
        "post_id": "amb", "author": "waltervannelli", "text": "Eur/usd sold 2.5 lots at 1.1705 not yet on position",
        "posted_at": NOW, "source": "x_api", "reply_to_id": None, "quoted_post_id": None, "is_repost": False,
        "media_json": "[]", "ingested_at": NOW, "raw_payload_json": None, "superseded_by_post_id": None,
    })
    repo.insert_classification("amb", "too_ambiguous", {}, None)

    bucket = _bucket(build_rejection_report(repo, "all"), "too_ambiguous")
    assert bucket.count == 1
    assert "not yet on position" in bucket.examples[0].text


def test_period_excludes_older_signals(repo):
    _seed(repo, 1, status="rejected", reason="missing_instrument", created_at=NOW - timedelta(days=40))
    _seed(repo, 2, status="rejected", reason="missing_instrument", created_at=NOW)
    assert _bucket(build_rejection_report(repo, "week", now=NOW), "missing_instrument").count == 1


def test_buckets_needing_attention_sort_before_routine_ones(repo):
    for n in range(5):
        _seed(repo, n, status="rejected", reason="not_actionable")
    _seed(repo, 99, status="rejected", reason="unmapped_instrument:euer/usd")
    report = build_rejection_report(repo, "all", now=NOW)
    # Five routine rejections must not bury one that needs fixing.
    assert report.buckets[0].key == "unmapped_instrument"
    assert report.buckets[-1].key == "not_actionable"


def test_examples_are_capped(repo):
    for n in range(10):
        _seed(repo, n, status="rejected", reason="missing_instrument")
    bucket = _bucket(build_rejection_report(repo, "all", now=NOW), "missing_instrument")
    assert bucket.count == 10
    assert len(bucket.examples) == 3


@pytest.mark.parametrize("raw,expected", [
    ("signal_too_stale:46.0min", "signal_too_stale"),
    ("entry_too_far_from_market:0.87%", "entry_too_far_from_market"),
    ("unmapped_instrument:euer/usd", "unmapped_instrument"),
    ("re_validation_failed:signal_too_stale:12.0min", "signal_too_stale"),
    ("circuit_breaker_tripped:consecutive_losses", "circuit_breaker_tripped:consecutive_losses"),
    ("not_actionable", "not_actionable"),
])
def test_normalise_reason(raw, expected):
    assert normalise_reason(raw) == expected


def test_endpoint_returns_json_and_rejects_bad_periods(repo):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.auth import AuthState

    _seed(repo, 1, status="approved", with_order=False)
    context = MagicMock()
    context.settings.operator_password = ""
    context.repository = repo
    context.auth_state = AuthState()
    client = TestClient(create_app(context))

    body = client.get("/api/reports/rejections?period=all").json()
    assert body["buckets"][0]["key"] == "approved_no_order"
    json.dumps(body)  # fully serialisable, datetimes included
    assert client.get("/api/reports/rejections?period=fortnight").status_code == 400
