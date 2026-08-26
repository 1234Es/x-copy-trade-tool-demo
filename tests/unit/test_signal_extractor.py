from unittest.mock import MagicMock

from app.nlp.context_engine import OpenSignalCandidate, PostContext
from app.nlp.openai_client import MalformedOpenAIResponseError, StructuredCompletionResult
from app.nlp.signal_extractor import SignalExtractor
from tests.fixtures.sample_posts import EXPLICIT_LONG_POST, NOW, make_post

EMPTY_CONTEXT = PostContext(parent_post=None)


def _valid_parsed(**overrides) -> dict:
    base = {
        "post_id": "post-long",
        "author": "waltervannelli",
        "signal_type": "new_trade",
        "instrument": "EURUSD",
        "direction": "long",
        "order_type": "limit",
        "entry_price": 1.0850,
        "entry_zone_low": None,
        "entry_zone_high": None,
        "stop_loss": 1.0800,
        "take_profit": [1.0950],
        "timeframe": None,
        "valid_until": None,
        "referenced_trade_id": None,
        "confidence": 0.95,
        "evidence": ["Long EURUSD here", "Entry 1.0850", "stop 1.0800", "target 1.0950"],
        "assumptions": [],
        "missing_fields": [],
        "requires_human_review": False,
        "reasoning_summary": "Clear entry with explicit stop and target.",
    }
    base.update(overrides)
    return base


def _make_extractor(parsed: dict, min_confidence: float = 0.9) -> SignalExtractor:
    mock_client = MagicMock()
    mock_client.structured_completion.return_value = StructuredCompletionResult(
        request_id="req-1", parsed=parsed, raw_content="{}"
    )
    context_engine = MagicMock()
    context_engine.format_for_prompt.return_value = "(no context)"
    return SignalExtractor(mock_client, context_engine, min_confidence)


def test_valid_extraction_accepted():
    extractor = _make_extractor(_valid_parsed())
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.accepted
    assert result.signal.instrument == "EURUSD"
    assert result.signal.requires_human_review is False


def test_rejects_evidence_not_found_verbatim_in_post():
    extractor = _make_extractor(_valid_parsed(evidence=["this text is not in the post"]))
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert not result.accepted
    assert "evidence_not_verbatim" in result.rejection_reason


def test_accepts_evidence_that_only_differs_by_whitespace_collapse():
    # Regression test: a real waltervannelli post had a double space
    # ("extesion  only") that the model harmlessly collapsed to a single
    # space when quoting it back as evidence -- this must not be treated as
    # a fabricated quote.
    post = make_post(
        "Eur/usd still short 10 lots at the average 1.1419 + 2.5 lots short at 1.1458..."
        "ready for extesion  only above 1.1485/95...........Later!",
        post_id="post-doublespace",
    )
    extractor = _make_extractor(
        _valid_parsed(
            post_id="post-doublespace",
            evidence=["ready for extesion only above 1.1485/95"],  # single space
        )
    )
    result = extractor.extract(post, EMPTY_CONTEXT)

    assert result.accepted


def test_still_rejects_evidence_with_genuinely_different_words():
    post = make_post("Long EURUSD here. Entry 1.0850, stop 1.0800, target 1.0950.", post_id="post-long2")
    extractor = _make_extractor(
        _valid_parsed(post_id="post-long2", evidence=["Long GBPUSD here"])  # wrong instrument entirely
    )
    result = extractor.extract(post, EMPTY_CONTEXT)

    assert not result.accepted
    assert "evidence_not_verbatim" in result.rejection_reason


def test_rejects_malformed_openai_response():
    mock_client = MagicMock()
    mock_client.structured_completion.side_effect = MalformedOpenAIResponseError("truncated")
    context_engine = MagicMock()
    context_engine.format_for_prompt.return_value = "(no context)"
    extractor = SignalExtractor(mock_client, context_engine, 0.9)

    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert not result.accepted
    assert "malformed_openai_response" in result.rejection_reason


def test_rejects_invented_referenced_trade_id():
    extractor = _make_extractor(_valid_parsed(referenced_trade_id="made-up-id-not-in-candidates"))
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert not result.accepted
    assert "invalid_referenced_trade_id" in result.rejection_reason


def test_accepts_referenced_trade_id_when_it_matches_a_candidate():
    context = PostContext(
        parent_post=None,
        open_signal_candidates=[
            OpenSignalCandidate(signal_id="real-candidate-id", instrument="EUR_USD", direction="long", signal_type="new_trade", created_at=NOW)
        ],
    )
    extractor = _make_extractor(_valid_parsed(referenced_trade_id="real-candidate-id"))
    result = extractor.extract(EXPLICIT_LONG_POST, context)

    assert result.accepted
    assert result.signal.referenced_trade_id == "real-candidate-id"


def test_forces_human_review_when_confidence_below_threshold():
    extractor = _make_extractor(_valid_parsed(confidence=0.5, requires_human_review=False), min_confidence=0.9)
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.accepted
    assert result.signal.requires_human_review is True


def test_forces_human_review_when_new_trade_missing_stop_loss():
    extractor = _make_extractor(_valid_parsed(stop_loss=None, requires_human_review=False))
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.accepted
    assert result.signal.requires_human_review is True


def test_forces_human_review_when_missing_fields_present():
    extractor = _make_extractor(_valid_parsed(missing_fields=["timeframe"], requires_human_review=False))
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.accepted
    assert result.signal.requires_human_review is True


def test_missing_entry_price_alone_does_not_force_human_review():
    # A null entry_price is a deliberate, safe simplification meaning
    # "market order at the prevailing price" (prompts.py rule 12: a stated
    # number framed as the position's blended running average, not a fresh
    # fill, must not be used as entry_price) -- not missing information
    # that needs a human's judgment call, so it shouldn't trip the generic
    # missing_fields review rule the way an actually-missing field does.
    extractor = _make_extractor(
        _valid_parsed(entry_price=None, missing_fields=["entry_price"], requires_human_review=False)
    )
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.accepted
    assert result.signal.requires_human_review is False


def test_missing_entry_price_alongside_another_missing_field_still_forces_review():
    extractor = _make_extractor(
        _valid_parsed(entry_price=None, missing_fields=["entry_price", "timeframe"], requires_human_review=False)
    )
    result = extractor.extract(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.accepted
    assert result.signal.requires_human_review is True
