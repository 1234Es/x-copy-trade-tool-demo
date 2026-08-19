"""Phase 8: the single gate that decides observe vs. approval vs.
practice_auto behavior. Nothing downstream (risk manager, order manager)
branches on `APP_MODE` -- this is the only place that reads it, so
switching modes can never accidentally skip a validation or risk check.

`requires_human_review` on the signal always forces a proposal, even in
practice_auto mode -- the mode changes who has to click "go" for a
qualifying signal, it never changes what qualifies.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from app.nlp.schemas import TradeSignal
from app.storage.repository import Repository


class WorkflowAction(str, Enum):
    LOG_HYPOTHETICAL = "log_hypothetical"
    CREATE_PROPOSAL = "create_proposal"
    EXECUTE_NOW = "execute_now"


@dataclass(frozen=True)
class WorkflowDecision:
    action: WorkflowAction
    reason: str


class ApprovalWorkflow:
    def __init__(
        self,
        repository: Repository,
        app_mode: str,
        proposal_expiry_minutes: int,
        min_signal_confidence: float,
        auto_approve: bool = False,
    ):
        if app_mode not in ("observe", "approval", "practice_auto"):
            raise ValueError(f"invalid app_mode: {app_mode}")
        self.repository = repository
        self.app_mode = app_mode
        self.proposal_expiry_minutes = proposal_expiry_minutes
        self.min_signal_confidence = min_signal_confidence
        # Live-toggleable from the dashboard (see /api/settings/auto-approve),
        # not just an .env value read once at startup -- deliberately mutable.
        self.auto_approve = auto_approve

    def set_auto_approve(self, enabled: bool) -> None:
        self.auto_approve = enabled

    def decide(self, signal: TradeSignal) -> WorkflowDecision:
        # observe mode's "never submit an order" guarantee is a different,
        # more fundamental axis than auto_approve (which only decides who
        # has to click for a trade that would otherwise become a
        # proposal) -- it is never bypassed by auto_approve.
        if self.app_mode == "observe":
            return WorkflowDecision(WorkflowAction.LOG_HYPOTHETICAL, "observe mode never submits orders")

        if self.auto_approve:
            return WorkflowDecision(
                WorkflowAction.EXECUTE_NOW,
                "auto_approve enabled -- executing without a human click (all validation/risk checks already passed)",
            )

        if signal.requires_human_review:
            return WorkflowDecision(WorkflowAction.CREATE_PROPOSAL, "signal requires human review")

        if self.app_mode == "approval":
            return WorkflowDecision(WorkflowAction.CREATE_PROPOSAL, "approval mode requires a human click for every trade")

        # practice_auto
        if signal.confidence < self.min_signal_confidence:
            return WorkflowDecision(
                WorkflowAction.CREATE_PROPOSAL,
                f"confidence {signal.confidence:.2f} below auto-execution threshold {self.min_signal_confidence:.2f}",
            )
        return WorkflowDecision(WorkflowAction.EXECUTE_NOW, "practice_auto mode, confidence threshold met, all checks passed")

    def create_proposal(self, signal_id: str, now: datetime) -> str:
        proposal_id = str(uuid.uuid4())
        self.repository.insert_proposal(
            {
                "proposal_id": proposal_id,
                "signal_id": signal_id,
                "status": "pending",
                "expires_at": now + timedelta(minutes=self.proposal_expiry_minutes),
                "decided_at": None,
                "decided_by": None,
                "created_at": now,
            }
        )
        return proposal_id

    def approve(self, proposal_id: str, decided_by: str, now: datetime) -> bool:
        proposal = self.repository.get_proposal(proposal_id)
        if proposal is None or proposal["status"] != "pending":
            return False
        if now > proposal["expires_at"]:
            self.repository.update_proposal_status(proposal_id, "expired", None, now)
            return False
        self.repository.update_proposal_status(proposal_id, "approved", decided_by, now)
        return True

    def reject(self, proposal_id: str, decided_by: str, now: datetime) -> bool:
        proposal = self.repository.get_proposal(proposal_id)
        if proposal is None or proposal["status"] != "pending":
            return False
        self.repository.update_proposal_status(proposal_id, "rejected", decided_by, now)
        return True

    def expire_stale_proposals(self, now: datetime) -> int:
        return self.repository.expire_stale_proposals(now)
