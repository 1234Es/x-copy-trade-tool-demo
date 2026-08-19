from unittest.mock import MagicMock

from app.nlp.classifier import Classifier, rule_based_prefilter
from app.nlp.context_engine import PostContext
from app.nlp.openai_client import MalformedOpenAIResponseError, StructuredCompletionResult
from app.nlp.schemas import PostCategory
from tests.fixtures.sample_posts import EXPLICIT_LONG_POST, UNRELATED_POST

EMPTY_CONTEXT = PostContext(parent_post=None)


def test_rule_based_prefilter_detects_keywords():
    signals = rule_based_prefilter("Long EURUSD here, stop 1.08, target 1.09")
    assert signals.worth_llm_review
    assert "long" in signals.matched_keywords
    assert "stop" in signals.matched_keywords


def test_rule_based_prefilter_detects_cashtag():
    signals = rule_based_prefilter("Watching $AAPL today")
    assert signals.has_cashtag
    assert signals.worth_llm_review


def test_rule_based_prefilter_no_signal_for_unrelated_text():
    signals = rule_based_prefilter("Just had the best coffee of my life this morning.")
    assert not signals.worth_llm_review


def test_classifier_skips_openai_call_for_unrelated_post():
    mock_client = MagicMock()
    classifier = Classifier(mock_client)

    result = classifier.classify(UNRELATED_POST, EMPTY_CONTEXT)

    assert result.classification.category == PostCategory.UNRELATED
    assert result.openai_request_id is None
    mock_client.structured_completion.assert_not_called()


def test_classifier_calls_openai_for_trading_looking_post():
    mock_client = MagicMock()
    mock_client.structured_completion.return_value = StructuredCompletionResult(
        request_id="req-1",
        parsed={"category": "explicit_trade_entry", "reasoning": "clear entry with stop and target"},
        raw_content="{}",
    )
    classifier = Classifier(mock_client)

    result = classifier.classify(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.classification.category == PostCategory.EXPLICIT_TRADE_ENTRY
    assert result.openai_request_id == "req-1"
    mock_client.structured_completion.assert_called_once()


def test_classifier_falls_back_to_too_ambiguous_on_malformed_response():
    mock_client = MagicMock()
    mock_client.structured_completion.side_effect = MalformedOpenAIResponseError("bad json")
    classifier = Classifier(mock_client)

    result = classifier.classify(EXPLICIT_LONG_POST, EMPTY_CONTEXT)

    assert result.classification.category == PostCategory.TOO_AMBIGUOUS
    assert result.openai_request_id is None
