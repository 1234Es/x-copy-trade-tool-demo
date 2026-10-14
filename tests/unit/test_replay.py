"""Covers the prompt replay: rebuilt context, comparison, and the run itself.
OpenAI is mocked -- these tests never make a paid call."""
from unittest.mock import MagicMock

from app.nlp.classifier import Classifier
from app.nlp.context_engine import ContextEngine
from app.nlp.openai_client import StructuredCompletionResult
from app.nlp.replay import (
    build_context, compare, estimate_calls, item_to_post, needs_openai, render_report, replay,
)
from app.nlp.signal_extractor import SignalExtractor


def _item(post_id, text, posted_at, *, author="waltervannelli", category=None, signal=None, reply_to=None):
    return {
        "post": {"post_id": post_id, "author": author, "text": text, "posted_at": posted_at,
                 "source": "x_api", "reply_to_id": reply_to},
        "classification": {"category": category} if category else None,
        "signal": signal,
    }


def _signal(**overrides):
    base = {"signal_type": "new_trade", "instrument": "eur/usd", "direction": "short", "order_type": "market",
            "entry_price": 1.1489, "stop_loss": None, "referenced_trade_id": None, "requires_human_review": True}
    base.update(overrides)
    return base


def _extraction(post_id, text, **overrides):
    parsed = {
        "post_id": post_id, "author": "waltervannelli", "signal_type": "new_trade", "instrument": "eur/usd",
        "direction": "short", "order_type": "market", "entry_price": 1.1489, "entry_zone_low": None,
        "entry_zone_high": None, "stop_loss": None, "take_profit": [], "timeframe": None, "valid_until": None,
        "referenced_trade_id": None, "confidence": 0.9, "evidence": [text], "assumptions": [],
        "missing_fields": ["stop_loss"], "requires_human_review": True, "reasoning_summary": "clear entry",
    }
    parsed.update(overrides)
    return parsed


def _engines(responses):
    client = MagicMock()
    client.structured_completion.side_effect = [
        StructuredCompletionResult(request_id=f"r{n}", parsed=p, raw_content="{}") for n, p in enumerate(responses)
    ]
    return Classifier(client), SignalExtractor(client, ContextEngine(repository=None), 0.9,
                                               missing_stop_loss_behavior="apply_risk_model"), client


# ---- context ------------------------------------------------------------------

def test_context_holds_only_the_authors_earlier_posts_newest_first():
    items = [
        _item("a", "first", "2026-09-21T10:00:00+00:00"),
        _item("b", "second", "2026-09-21T11:00:00+00:00"),
        _item("other", "someone else", "2026-09-21T11:30:00+00:00", author="trynahustle"),
        _item("c", "the post", "2026-09-21T12:00:00+00:00"),
        _item("d", "later", "2026-09-21T13:00:00+00:00"),
    ]
    context = build_context(item_to_post(items[3]), items)
    # Never the future, never another author: the live engine sees neither.
    assert [c.post_id for c in context.recent_author_posts] == ["b", "a"]
    assert context.recent_author_posts[0].minutes_before_current_post == 60.0


def test_context_is_capped_like_the_live_engine():
    items = [_item(f"p{n}", "x", f"2026-09-21T{10 + n:02d}:00:00+00:00") for n in range(8)]
    assert len(build_context(item_to_post(items[-1]), items).recent_author_posts) == 5


def test_context_resolves_a_parent_post_present_in_the_set():
    items = [_item("parent", "the parent", "2026-09-21T10:00:00+00:00"),
             _item("reply", "the reply", "2026-09-21T10:05:00+00:00", reply_to="parent")]
    assert build_context(item_to_post(items[1]), items).parent_post.post_id == "parent"


# ---- comparison ---------------------------------------------------------------

def test_compare_reports_a_changed_field():
    item = _item("p", "x", "2026-09-21T10:00:00+00:00", category="explicit_trade_entry",
                 signal=_signal(order_type="limit"))
    changes = compare(item, "explicit_trade_entry", _signal(order_type="market"), None)
    # The exact regression that opened an untracked trade on 2026-08-26.
    assert [(c.field, c.before, c.after) for c in changes] == [("order_type", "limit", "market")]


def test_compare_ignores_instrument_spelling():
    item = _item("p", "x", "2026-09-21T10:00:00+00:00", category="explicit_trade_entry",
                 signal=_signal(instrument="Eur/usd"))
    assert compare(item, "explicit_trade_entry", _signal(instrument="EURUSD"), None) == []


def test_compare_reports_a_signal_that_disappears_and_why():
    item = _item("p", "x", "2026-09-21T10:00:00+00:00", category="explicit_trade_entry", signal=_signal())
    changes = compare(item, "too_ambiguous", None, None)
    assert ("category", "explicit_trade_entry", "too_ambiguous") in [(c.field, c.before, c.after) for c in changes]
    assert any(c.field == "signal" and c.after.startswith("no signal") for c in changes)


def test_compare_treats_a_number_appearing_as_a_change():
    item = _item("p", "x", "2026-09-21T10:00:00+00:00", category="explicit_trade_entry",
                 signal=_signal(entry_price=None))
    assert [c.field for c in compare(item, "explicit_trade_entry", _signal(entry_price=1.1549), None)] == ["entry_price"]


# ---- the run ------------------------------------------------------------------

def test_replay_counts_unchanged_and_lists_changed():
    text = "eur/usd sold 2.5 lots at 1.1489"
    items = [
        _item("same", text, "2026-09-21T10:00:00+00:00", category="explicit_trade_entry", signal=_signal()),
        _item("flip", text, "2026-09-21T11:00:00+00:00", category="explicit_trade_entry",
              signal=_signal(order_type="limit")),
    ]
    classifier, extractor, _ = _engines([
        {"category": "explicit_trade_entry", "reasoning": "entry"}, _extraction("same", text),
        {"category": "explicit_trade_entry", "reasoning": "entry"}, _extraction("flip", text),
    ])

    result = replay(items, classifier, extractor)
    assert result.replayed == 2 and result.unchanged == 1
    assert [o.post_id for o in result.changed] == ["flip"]


def test_posts_with_no_trading_words_never_reach_the_model():
    # Exactly as in production: free to replay, and must not be miscounted.
    items = [_item("chat", "On #mentoring....Later!", "2026-09-21T10:00:00+00:00", category="unrelated")]
    classifier, extractor, client = _engines([])

    result = replay(items, classifier, extractor)
    client.structured_completion.assert_not_called()
    assert result.unchanged == 1
    assert not needs_openai(items[0]) and estimate_calls(items) == 0


def test_one_failing_call_does_not_stop_the_rest():
    # A paid run must not be lost to one timeout halfway through. A RuntimeError
    # isn't one of the errors the classifier absorbs, so this exercises the
    # replay's own guard rather than the pipeline's.
    text = "eur/usd sold 2.5 lots at 1.1489"
    items = [
        _item("bad", text, "2026-09-21T10:00:00+00:00", category="explicit_trade_entry", signal=_signal()),
        _item("good", text, "2026-09-21T11:00:00+00:00", category="explicit_trade_entry", signal=_signal()),
    ]
    client = MagicMock()
    client.structured_completion.side_effect = [
        RuntimeError("read timeout"),
        StructuredCompletionResult(request_id="r1", parsed={"category": "explicit_trade_entry", "reasoning": "x"},
                                   raw_content="{}"),
        StructuredCompletionResult(request_id="r2", parsed=_extraction("good", text), raw_content="{}"),
    ]
    extractor = SignalExtractor(client, ContextEngine(repository=None), 0.9,
                                missing_stop_loss_behavior="apply_risk_model")

    result = replay(items, Classifier(client), extractor)
    assert [o.post_id for o in result.errors] == ["bad"]
    assert "read timeout" in result.errors[0].error
    assert result.replayed == 1 and result.unchanged == 1


def test_a_malformed_response_shows_up_as_a_lost_signal():
    # The extractor absorbs a schema failure itself; the replay must surface
    # it as the signal disappearing, since that's what it would do live.
    text = "eur/usd sold 2.5 lots at 1.1489"
    items = [_item("p", text, "2026-09-21T10:00:00+00:00", category="explicit_trade_entry", signal=_signal())]
    classifier, extractor, _ = _engines([{"category": "explicit_trade_entry", "reasoning": "entry"},
                                         {"not": "a valid signal"}])
    result = replay(items, classifier, extractor)
    change = next(c for c in result.changed[0].changes if c.field == "signal")
    assert change.after.startswith("no signal (schema_validation_failed")


def test_estimate_counts_classification_plus_previously_actionable_extraction():
    items = [
        _item("entry", "eur/usd sold 2.5 lots at 1.1489", "2026-09-21T10:00:00+00:00", category="explicit_trade_entry"),
        _item("obs", "eur/usd short still in position", "2026-09-21T11:00:00+00:00", category="market_observation"),
        _item("chat", "On #mentoring....Later!", "2026-09-21T12:00:00+00:00", category="unrelated"),
    ]
    assert estimate_calls(items) == 3  # 2 classifications + 1 extraction


def test_report_lists_each_change():
    text = "eur/usd sold 2.5 lots at 1.1489"
    items = [_item("flip", text, "2026-09-21T11:00:00+00:00", category="explicit_trade_entry",
                   signal=_signal(order_type="limit"))]
    classifier, extractor, _ = _engines([{"category": "explicit_trade_entry", "reasoning": "x"},
                                         _extraction("flip", text)])
    report = render_report(replay(items, classifier, extractor), "history.json")
    assert "**1 changed**" in report
    assert "| order_type | limit | market |" in report
    assert text in report
