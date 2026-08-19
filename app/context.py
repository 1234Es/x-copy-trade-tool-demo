"""Wires together one shared application context: config, storage, NLP,
broker, risk, and execution objects. `main.py` builds this once at
startup; `api/server.py` reads it to serve the dashboard and handle
manual/webhook post submission and proposal approval.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.auth import AuthState
from app.broker.base_broker import BaseBroker, NullBroker
from app.broker.oanda_practice import OandaPracticeBroker, OandaPracticeConfig
from app.broker.order_manager import OrderManager
from app.broker.reconciliation import Reconciler
from app.config.settings import (
    Settings,
    load_instrument_map,
    load_risk_config,
    load_settings,
    load_tracked_accounts,
)
from app.execution.approval_workflow import ApprovalWorkflow
from app.execution.execution_engine import ExecutionEngine
from app.execution.signal_validator import SignalValidator
from app.monitoring.alerts import CompositeAlertSink
from app.monitoring.logging import configure_logging
from app.nlp.classifier import Classifier
from app.nlp.context_engine import ContextEngine
from app.nlp.openai_client import OpenAIClient
from app.nlp.signal_extractor import SignalExtractor
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.risk_manager import RiskManager
from app.sources.manual_source import ManualSource
from app.sources.webhook_source import WebhookSource
from app.storage.database import create_db_engine
from app.storage.repository import Repository


@dataclass
class AppContext:
    settings: Settings
    repository: Repository
    broker: BaseBroker
    risk_manager: RiskManager
    circuit_breaker: CircuitBreaker
    order_manager: OrderManager
    reconciler: Reconciler
    manual_source: ManualSource
    webhook_source: WebhookSource
    execution_engine: ExecutionEngine
    approval_workflow: ApprovalWorkflow
    alert_sink: CompositeAlertSink
    instrument_aliases: dict[str, str]
    known_unsupported: set[str]
    tracked_accounts: list[dict]
    auth_state: AuthState


def build_context(settings: Settings | None = None) -> AppContext:
    settings = settings or load_settings()
    configure_logging(level="INFO", directory="logs")

    engine = create_db_engine(settings.database_url)
    repository = Repository(engine)

    if settings.oanda_api_token and settings.oanda_account_id:
        broker: BaseBroker = OandaPracticeBroker(
            OandaPracticeConfig(api_token=settings.oanda_api_token, account_id=settings.oanda_account_id),
            environment=settings.oanda_environment,
        )
    else:
        broker = NullBroker()

    circuit_breaker = CircuitBreaker()
    risk_config = load_risk_config()
    risk_manager = RiskManager(risk_config, circuit_breaker)
    order_manager = OrderManager(broker, repository)
    reconciler = Reconciler(broker, repository)

    manual_source = ManualSource()
    webhook_source = WebhookSource(settings.webhook_shared_secret)

    instrument_aliases, known_unsupported = load_instrument_map()
    tracked_accounts = load_tracked_accounts()

    alert_sink = CompositeAlertSink(webhook_url=None)

    openai_client = OpenAIClient(api_key=settings.openai_api_key, model=settings.openai_model) if settings.openai_api_key else None
    classifier = Classifier(openai_client) if openai_client else _NullClassifier()
    context_engine = ContextEngine(repository)
    signal_extractor = SignalExtractor(openai_client, context_engine, settings.min_signal_confidence) if openai_client else None

    signal_validator = SignalValidator(
        repository=repository,
        broker=broker,
        instrument_aliases=instrument_aliases,
        known_unsupported=known_unsupported,
        max_signal_age_minutes=settings.max_signal_age_minutes,
        missing_stop_loss_behavior=risk_config.get("missing_stop_loss_behavior", "human_review"),
        fallback_stop_distance_pips=risk_config.get("fallback_stop_distance_pips", {}),
        fallback_reward_to_risk_multiple=risk_config.get("fallback_reward_to_risk_multiple", 1.5),
    )
    approval_workflow = ApprovalWorkflow(
        repository=repository,
        app_mode=settings.app_mode,
        proposal_expiry_minutes=settings.proposal_expiry_minutes,
        min_signal_confidence=settings.min_signal_confidence,
        auto_approve=settings.auto_approve_proposals,
    )

    execution_engine = ExecutionEngine(
        repository=repository,
        classifier=classifier,
        context_engine=context_engine,
        signal_extractor=signal_extractor or _NullExtractor(),
        signal_validator=signal_validator,
        risk_manager=risk_manager,
        approval_workflow=approval_workflow,
        order_manager=order_manager,
        broker=broker,
        alert_sink=alert_sink,
        tracked_accounts=tracked_accounts,
    )

    return AppContext(
        settings=settings,
        repository=repository,
        broker=broker,
        risk_manager=risk_manager,
        circuit_breaker=circuit_breaker,
        order_manager=order_manager,
        reconciler=reconciler,
        manual_source=manual_source,
        webhook_source=webhook_source,
        execution_engine=execution_engine,
        approval_workflow=approval_workflow,
        alert_sink=alert_sink,
        instrument_aliases=instrument_aliases,
        known_unsupported=known_unsupported,
        tracked_accounts=tracked_accounts,
        auth_state=AuthState(),
    )


class _NullClassifier:
    """Used only if OPENAI_API_KEY is missing -- everything is classified
    too_ambiguous rather than the app crashing at startup, so the rest of
    the system (dashboard, manual post submission UI) is still inspectable
    before a key is configured.
    """

    def classify(self, post, context):  # noqa: ANN001 -- matches Classifier.classify's duck-typed signature
        from app.nlp.classifier import ClassificationResult, RuleBasedSignals
        from app.nlp.schemas import PostCategory, PostClassification

        return ClassificationResult(
            classification=PostClassification(
                category=PostCategory.TOO_AMBIGUOUS,
                reasoning="OPENAI_API_KEY is not configured -- classification cannot run.",
            ),
            rule_based_signals=RuleBasedSignals(has_cashtag=False, matched_keywords=()),
            openai_request_id=None,
        )


class _NullExtractor:
    def extract(self, post, context):  # noqa: ANN001
        from app.nlp.signal_extractor import ExtractionResult

        return ExtractionResult(None, None, "openai_not_configured")
