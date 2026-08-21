"""Orchestrates the full pipeline for one post: classify -> extract ->
validate -> risk-check -> approval-mode gate -> (maybe) submit order.

Every stage's result is persisted before moving to the next (Phase 11
audit trail) -- a post that gets rejected at any stage has a complete,
inspectable record of exactly why.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.broker.base_broker import BaseBroker
from app.broker.order_manager import OrderManager
from app.execution.approval_workflow import ApprovalWorkflow, WorkflowAction
from app.execution.signal_validator import SignalValidator
from app.monitoring.alerts import AlertSink
from app.monitoring.logging import get_logger
from app.nlp.classifier import Classifier
from app.nlp.context_engine import ContextEngine
from app.nlp.schemas import ACTIONABLE_CATEGORIES, Direction, SignalType, TradeSignal
from app.nlp.signal_extractor import SignalExtractor
from app.risk.risk_manager import AccountState, OpenPositionInfo, RiskManager
from app.sources.base_source import Post
from app.storage.repository import Repository

# Signal types that act on an already-open trade rather than opening a new
# one -- these never go through risk_manager.evaluate_trade() (its checks --
# mandatory stop-loss, reward:risk, spread, position sizing -- are all about
# the risk of OPENING a position, none of them make sense against a trade
# that's being closed or adjusted) and never call order_manager.submit().
_POSITION_MANAGEMENT_TYPES = frozenset(
    {SignalType.UPDATE_STOP, SignalType.UPDATE_TARGET, SignalType.PARTIAL_CLOSE, SignalType.FULL_CLOSE}
)

log = get_logger("execution_engine")


@dataclass(frozen=True)
class ProcessingOutcome:
    status: str  # duplicate_post | not_actionable | extraction_rejected | validation_rejected |
                 # risk_rejected | logged_hypothetical | proposal_created | executed
    detail: str | None = None
    signal_id: str | None = None
    proposal_id: str | None = None


class ExecutionEngine:
    def __init__(
        self,
        repository: Repository,
        classifier: Classifier,
        context_engine: ContextEngine,
        signal_extractor: SignalExtractor,
        signal_validator: SignalValidator,
        risk_manager: RiskManager,
        approval_workflow: ApprovalWorkflow,
        order_manager: OrderManager,
        broker: BaseBroker,
        alert_sink: AlertSink,
        tracked_accounts: list[dict] | None = None,
    ):
        self.repository = repository
        self.classifier = classifier
        self.context_engine = context_engine
        self.signal_extractor = signal_extractor
        self.signal_validator = signal_validator
        self.risk_manager = risk_manager
        self.approval_workflow = approval_workflow
        self.order_manager = order_manager
        self.broker = broker
        self.alert_sink = alert_sink
        # Per-source-account position cap (tracked_accounts.yaml,
        # DESIGN.md/Phase 7: "max exposure per source account"). Falls back
        # to risk_manager's own config default for any account not listed
        # (e.g. a manual/webhook post from an untracked author).
        self._max_positions_by_account = {
            a["username"]: a["max_open_positions_from_account"]
            for a in (tracked_accounts or [])
            if "max_open_positions_from_account" in a
        }

    def process_post(self, post: Post, now: datetime | None = None) -> ProcessingOutcome:
        now = now or datetime.now(timezone.utc)

        inserted = self.repository.insert_raw_post(_post_to_row(post))
        if not inserted:
            return ProcessingOutcome("duplicate_post")

        context = self.context_engine.build_context(post)

        classification_result = self.classifier.classify(post, context)
        self.repository.insert_classification(
            post.post_id,
            classification_result.classification.category.value,
            {
                "has_cashtag": classification_result.rule_based_signals.has_cashtag,
                "matched_keywords": list(classification_result.rule_based_signals.matched_keywords),
            },
            classification_result.openai_request_id,
        )
        log.info(
            "post_classified", post_id=post.post_id, author=post.author,
            category=classification_result.classification.category.value,
        )

        if classification_result.classification.category not in ACTIONABLE_CATEGORIES:
            return ProcessingOutcome("not_actionable", classification_result.classification.category.value)

        extraction_result = self.signal_extractor.extract(post, context)
        if not extraction_result.accepted:
            log.info("signal_extraction_rejected", post_id=post.post_id, reason=extraction_result.rejection_reason)
            return ProcessingOutcome("extraction_rejected", extraction_result.rejection_reason)

        signal = extraction_result.signal
        signal_id = str(uuid.uuid4())
        self.repository.insert_signal(_signal_to_row(signal_id, post.post_id, signal, extraction_result.openai_request_id, now))

        validation = self.signal_validator.validate(signal, post.posted_at, now)
        if not validation.approved:
            self.repository.update_signal_validation(signal_id, "rejected", validation.reason)
            log.info("signal_validation_rejected", signal_id=signal_id, reason=validation.reason)
            return ProcessingOutcome("validation_rejected", validation.reason, signal_id=signal_id)

        if signal.signal_type in _POSITION_MANAGEMENT_TYPES:
            return self._process_position_management(signal, signal_id, validation, now)

        account_state = self._build_account_state()
        risk_decision = self.risk_manager.evaluate_trade(
            instrument=validation.oanda_instrument,
            source_account=post.author,
            stop_distance_price=validation.stop_distance_price or 0.0,
            target_distance_price=validation.target_distance_price or 0.0,
            current_spread_pips=validation.current_spread_pips or 0.0,
            price=validation.entry_reference_price or 0.0,
            account_state=account_state,
            now=now,
            margin_rate=validation.margin_rate,
            max_positions_for_source_account=self._max_positions_by_account.get(post.author),
        )
        if not risk_decision.approved:
            self.repository.update_signal_validation(signal_id, "rejected", risk_decision.reason)
            log.info("signal_risk_rejected", signal_id=signal_id, reason=risk_decision.reason)
            return ProcessingOutcome("risk_rejected", risk_decision.reason, signal_id=signal_id)

        self.repository.update_signal_validation(signal_id, "approved", None)

        if validation.stop_loss_is_fallback or validation.take_profit_is_fallback:
            log.info(
                "fallback_risk_model_applied",
                signal_id=signal_id,
                stop_loss_is_fallback=validation.stop_loss_is_fallback,
                take_profit_is_fallback=validation.take_profit_is_fallback,
                effective_stop_loss_price=validation.effective_stop_loss_price,
                effective_take_profit_price=validation.effective_take_profit_price,
            )

        workflow_decision = self.approval_workflow.decide(signal)
        if workflow_decision.action == WorkflowAction.LOG_HYPOTHETICAL:
            log.info(
                "hypothetical_trade_logged", signal_id=signal_id, instrument=validation.oanda_instrument,
                direction=signal.direction.value if signal.direction else None,
                units=risk_decision.position_size.units if risk_decision.position_size else None,
            )
            return ProcessingOutcome("logged_hypothetical", workflow_decision.reason, signal_id=signal_id)

        if workflow_decision.action == WorkflowAction.CREATE_PROPOSAL:
            proposal_id = self.approval_workflow.create_proposal(signal_id, now)
            self.alert_sink.send("new_proposal", f"New trade proposal for {validation.oanda_instrument}", {"signal_id": signal_id, "proposal_id": proposal_id})
            return ProcessingOutcome("proposal_created", workflow_decision.reason, signal_id=signal_id, proposal_id=proposal_id)

        return self._execute(signal, signal_id, validation, risk_decision, post.author, now)

    def execute_approved_proposal(self, proposal_id: str, now: datetime | None = None) -> ProcessingOutcome:
        """Called after a human clicks Approve. Re-validates at execution
        time (not just at proposal-creation time) since price/spread may
        have moved in the interim."""
        now = now or datetime.now(timezone.utc)
        proposal = self.repository.get_proposal(proposal_id)
        if proposal is None or proposal["status"] != "approved":
            return ProcessingOutcome("proposal_not_approved")

        signal_row = self.repository.get_signal(proposal["signal_id"])
        if signal_row is None:
            return ProcessingOutcome("signal_not_found")

        signal = _row_to_signal(signal_row)
        post_row = self.repository.get_raw_post(signal_row["post_id"])
        post_posted_at = post_row["posted_at"] if post_row else now

        validation = self.signal_validator.validate(signal, post_posted_at, now)
        if not validation.approved:
            self.repository.update_signal_validation(proposal["signal_id"], "rejected", f"re_validation_failed:{validation.reason}")
            log.info("proposal_execution_validation_rejected", proposal_id=proposal_id, signal_id=proposal["signal_id"], reason=validation.reason)
            return ProcessingOutcome("validation_rejected", validation.reason, signal_id=proposal["signal_id"])

        if signal.signal_type in _POSITION_MANAGEMENT_TYPES:
            return self._resolve_and_execute_management(signal, proposal["signal_id"], now)

        account_state = self._build_account_state()
        risk_decision = self.risk_manager.evaluate_trade(
            instrument=validation.oanda_instrument,
            source_account=signal.author,
            stop_distance_price=validation.stop_distance_price or 0.0,
            target_distance_price=validation.target_distance_price or 0.0,
            current_spread_pips=validation.current_spread_pips or 0.0,
            price=validation.entry_reference_price or 0.0,
            account_state=account_state,
            now=now,
            margin_rate=validation.margin_rate,
            max_positions_for_source_account=self._max_positions_by_account.get(signal.author),
        )
        if not risk_decision.approved:
            log.info("proposal_execution_risk_rejected", proposal_id=proposal_id, signal_id=proposal["signal_id"], reason=risk_decision.reason)
            return ProcessingOutcome("risk_rejected", risk_decision.reason, signal_id=proposal["signal_id"])

        return self._execute(signal, proposal["signal_id"], validation, risk_decision, signal.author, now)

    def _execute(self, signal: TradeSignal, signal_id: str, validation, risk_decision, source_account: str, now: datetime) -> ProcessingOutcome:
        limit_or_stop_price = None
        if signal.order_type and signal.order_type.value in ("limit", "stop"):
            limit_or_stop_price = signal.entry_price

        outcome = self.order_manager.submit(
            signal=signal,
            signal_id=signal_id,
            units=risk_decision.position_size.units,
            stop_loss_price=validation.effective_stop_loss_price,
            take_profit_price=validation.effective_take_profit_price,
            limit_or_stop_price=limit_or_stop_price,
            oanda_instrument=validation.oanda_instrument,
        )

        if not outcome.submitted:
            return ProcessingOutcome("order_skipped", outcome.skipped_reason, signal_id=signal_id)

        if outcome.result and outcome.result.success:
            self.risk_manager.record_order_accepted()
            self.risk_manager.record_trade_opened(now)
            self.alert_sink.send("order_filled", f"{validation.oanda_instrument} order filled", {"signal_id": signal_id})
            return ProcessingOutcome("executed", "order_filled", signal_id=signal_id)

        shutdown = self.risk_manager.record_order_rejection(now)
        reason = outcome.result.rejection_reason if outcome.result else "unknown"
        self.alert_sink.send("order_rejected", f"Order rejected: {reason}", {"signal_id": signal_id})
        if shutdown:
            self.alert_sink.send("circuit_breaker_shutdown", "Repeated order rejections tripped the circuit breaker", {})
        return ProcessingOutcome("order_rejected", reason, signal_id=signal_id)

    def _process_position_management(
        self, signal: TradeSignal, signal_id: str, validation, now: datetime
    ) -> ProcessingOutcome:
        """Handles update_stop / update_target / partial_close / full_close
        signals -- these act on an already-open trade, so they skip
        risk_manager.evaluate_trade() entirely (its checks are all about the
        risk of opening a new position) and go through the same
        observe/approval/practice_auto gate as a new_trade signal, just with
        a different execution step at the end."""
        if signal.signal_type == SignalType.PARTIAL_CLOSE:
            # The extraction schema (DESIGN.md Section 4) has no field for
            # how much to close -- a lot size or fraction. Guessing one would
            # mean inventing a number nothing in the post or schema actually
            # supports, which this system's extraction rules explicitly
            # forbid elsewhere (see prompts.py). Always route to a human
            # rather than silently doing nothing or picking an arbitrary size.
            self.repository.update_signal_validation(signal_id, "approved", None)
            proposal_id = self.approval_workflow.create_proposal(signal_id, now)
            self.alert_sink.send(
                "new_proposal",
                f"Partial close for {validation.oanda_instrument} -- size must be set manually",
                {"signal_id": signal_id, "proposal_id": proposal_id},
            )
            return ProcessingOutcome(
                "proposal_created", "partial_close has no auto-determined size", signal_id=signal_id, proposal_id=proposal_id
            )

        if signal.referenced_trade_id is None:
            self.repository.update_signal_validation(signal_id, "rejected", "no_referenced_trade_to_act_on")
            log.info("signal_validation_rejected", signal_id=signal_id, reason="no_referenced_trade_to_act_on")
            return ProcessingOutcome("validation_rejected", "no_referenced_trade_to_act_on", signal_id=signal_id)

        open_trade = self.repository.get_open_trade_for_signal(signal.referenced_trade_id)
        if open_trade is None:
            self.repository.update_signal_validation(signal_id, "rejected", "referenced_trade_not_open")
            log.info("signal_validation_rejected", signal_id=signal_id, reason="referenced_trade_not_open")
            return ProcessingOutcome("validation_rejected", "referenced_trade_not_open", signal_id=signal_id)

        self.repository.update_signal_validation(signal_id, "approved", None)

        workflow_decision = self.approval_workflow.decide(signal)
        if workflow_decision.action == WorkflowAction.LOG_HYPOTHETICAL:
            log.info(
                "hypothetical_position_management_logged",
                signal_id=signal_id, signal_type=signal.signal_type.value, oanda_trade_id=open_trade["oanda_trade_id"],
            )
            return ProcessingOutcome("logged_hypothetical", workflow_decision.reason, signal_id=signal_id)

        if workflow_decision.action == WorkflowAction.CREATE_PROPOSAL:
            proposal_id = self.approval_workflow.create_proposal(signal_id, now)
            self.alert_sink.send(
                "new_proposal", f"{signal.signal_type.value} proposal for {validation.oanda_instrument}",
                {"signal_id": signal_id, "proposal_id": proposal_id},
            )
            return ProcessingOutcome("proposal_created", workflow_decision.reason, signal_id=signal_id, proposal_id=proposal_id)

        return self._execute_management(signal, signal_id, open_trade, now)

    def _resolve_and_execute_management(self, signal: TradeSignal, signal_id: str, now: datetime) -> ProcessingOutcome:
        """Same as the EXECUTE_NOW tail of _process_position_management, but
        entered from execute_approved_proposal (after a human already
        approved the proposal) -- resolves the referenced trade fresh, since
        time has passed since the proposal was created."""
        if signal.signal_type == SignalType.PARTIAL_CLOSE:
            return ProcessingOutcome("order_skipped", "partial_close has no auto-determined size", signal_id=signal_id)
        if signal.referenced_trade_id is None:
            return ProcessingOutcome("order_skipped", "no_referenced_trade_to_act_on", signal_id=signal_id)
        open_trade = self.repository.get_open_trade_for_signal(signal.referenced_trade_id)
        if open_trade is None:
            return ProcessingOutcome("order_skipped", "referenced_trade_not_open", signal_id=signal_id)
        return self._execute_management(signal, signal_id, open_trade, now)

    def _execute_management(
        self, signal: TradeSignal, signal_id: str, open_trade: dict[str, Any], now: datetime
    ) -> ProcessingOutcome:
        oanda_trade_id = open_trade["oanda_trade_id"]

        if signal.signal_type == SignalType.FULL_CLOSE:
            outcome = self.order_manager.close_position(oanda_trade_id, now)
            if outcome.success:
                self.risk_manager.record_trade_closed(open_trade["instrument"], outcome.realized_pl or 0.0, now)
                self.alert_sink.send(
                    "trade_closed", f"{open_trade['instrument']} closed per signal", {"signal_id": signal_id}
                )
                return ProcessingOutcome("executed", "trade_closed", signal_id=signal_id)
            self.alert_sink.send("close_rejected", f"Close rejected: {outcome.rejection_reason}", {"signal_id": signal_id})
            return ProcessingOutcome("order_rejected", outcome.rejection_reason, signal_id=signal_id)

        # update_stop / update_target
        stop_loss_price = signal.stop_loss if signal.signal_type == SignalType.UPDATE_STOP else None
        take_profit_price = (
            signal.take_profit[0] if signal.signal_type == SignalType.UPDATE_TARGET and signal.take_profit else None
        )
        if stop_loss_price is None and take_profit_price is None:
            return ProcessingOutcome("order_skipped", "no_new_stop_or_target_in_signal", signal_id=signal_id)

        outcome = self.order_manager.modify_position(oanda_trade_id, stop_loss_price, take_profit_price)
        if outcome.success:
            self.alert_sink.send(
                "trade_modified", f"{open_trade['instrument']} stop/target updated per signal", {"signal_id": signal_id}
            )
            return ProcessingOutcome("executed", "trade_modified", signal_id=signal_id)
        self.alert_sink.send("modify_rejected", f"Modify rejected: {outcome.rejection_reason}", {"signal_id": signal_id})
        return ProcessingOutcome("order_rejected", outcome.rejection_reason, signal_id=signal_id)

    def _build_account_state(self) -> AccountState:
        snapshot = self.broker.get_account_snapshot()
        open_trades = self.repository.get_open_trades_with_source()
        # Exposure sums in risk_manager compare against risk_per_trade_percent's
        # own unit -- approximating each open position's contribution as the
        # *current* risk_per_trade_percent (we don't persist the risk% a given
        # trade was actually sized under). This was previously hardcoded to
        # 0.25, silently decoupled from whatever risk_per_trade_percent was
        # actually configured -- see chat log 2026-07-15.
        current_risk_pct = self.risk_manager.config["risk_per_trade_percent"]
        open_positions = [
            OpenPositionInfo(instrument=t["instrument"], source_account=t["source_account"], risk_amount_pct=current_risk_pct)
            for t in open_trades
        ]
        # Daily/weekly starting equity tracking is intentionally simple here
        # (main.py resets it once per UTC day/week) -- see main.py's loop.
        return AccountState(
            equity=snapshot.equity,
            balance=snapshot.balance,
            open_positions=open_positions,
            daily_starting_equity=snapshot.equity,
            weekly_starting_equity=snapshot.equity,
        )


def _post_to_row(post: Post) -> dict[str, Any]:
    return {
        "post_id": post.post_id,
        "author": post.author,
        "text": post.text,
        "posted_at": post.posted_at,
        "source": post.source,
        "reply_to_id": post.reply_to_id,
        "quoted_post_id": post.quoted_post_id,
        "is_repost": post.is_repost,
        "media_json": json.dumps([m.__dict__ for m in post.media]),
        "ingested_at": datetime.now(timezone.utc),
        "raw_payload_json": json.dumps(post.raw_payload, default=str) if post.raw_payload else None,
        "superseded_by_post_id": None,
    }


def _signal_to_row(signal_id: str, post_id: str, signal: TradeSignal, openai_request_id: str | None, now: datetime) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "post_id": post_id,
        "author": signal.author,
        "signal_type": signal.signal_type.value,
        "instrument": signal.instrument,
        "direction": signal.direction.value if signal.direction else None,
        "order_type": signal.order_type.value if signal.order_type else None,
        "entry_price": signal.entry_price,
        "entry_zone_low": signal.entry_zone_low,
        "entry_zone_high": signal.entry_zone_high,
        "stop_loss": signal.stop_loss,
        "take_profit_json": json.dumps(signal.take_profit),
        "timeframe": signal.timeframe,
        "valid_until": signal.valid_until,
        "referenced_trade_id": signal.referenced_trade_id,
        "confidence": signal.confidence,
        "evidence_json": json.dumps(signal.evidence),
        "assumptions_json": json.dumps(signal.assumptions),
        "missing_fields_json": json.dumps(signal.missing_fields),
        "requires_human_review": signal.requires_human_review,
        "reasoning_summary": signal.reasoning_summary,
        "openai_request_id": openai_request_id,
        "validation_status": "pending",
        "rejection_reason": None,
        "created_at": now,
    }


def _row_to_signal(row: dict[str, Any]) -> TradeSignal:
    from app.nlp.schemas import Direction as _Direction
    from app.nlp.schemas import OrderType as _OrderType
    from app.nlp.schemas import SignalType as _SignalType

    return TradeSignal(
        post_id=row["post_id"],
        author=row["author"],
        signal_type=_SignalType(row["signal_type"]),
        instrument=row["instrument"],
        direction=_Direction(row["direction"]) if row["direction"] else None,
        order_type=_OrderType(row["order_type"]) if row["order_type"] else None,
        entry_price=row["entry_price"],
        entry_zone_low=row["entry_zone_low"],
        entry_zone_high=row["entry_zone_high"],
        stop_loss=row["stop_loss"],
        take_profit=json.loads(row["take_profit_json"]),
        timeframe=row["timeframe"],
        valid_until=row["valid_until"],
        referenced_trade_id=row["referenced_trade_id"],
        confidence=row["confidence"],
        evidence=json.loads(row["evidence_json"]),
        assumptions=json.loads(row["assumptions_json"]),
        missing_fields=json.loads(row["missing_fields_json"]),
        requires_human_review=row["requires_human_review"],
        reasoning_summary=row["reasoning_summary"],
    )
