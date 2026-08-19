from datetime import timedelta

import pytest

from app.execution.approval_workflow import ApprovalWorkflow, WorkflowAction
from app.nlp.schemas import Direction, SignalType, TradeSignal
from app.storage.database import create_db_engine
from app.storage.repository import Repository
from tests.fixtures.sample_posts import NOW


@pytest.fixture
def repository() -> Repository:
    return Repository(create_db_engine("sqlite:///:memory:"))


def _signal(**overrides) -> TradeSignal:
    base = dict(
        post_id="post-1", author="waltervannelli", signal_type=SignalType.NEW_TRADE, instrument="EUR_USD",
        direction=Direction.LONG, confidence=0.95, requires_human_review=False,
    )
    base.update(overrides)
    return TradeSignal(**base)


def test_observe_mode_always_logs_hypothetical(repository):
    workflow = ApprovalWorkflow(repository, "observe", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    decision = workflow.decide(_signal(confidence=0.99))
    assert decision.action == WorkflowAction.LOG_HYPOTHETICAL


def test_approval_mode_always_creates_proposal(repository):
    workflow = ApprovalWorkflow(repository, "approval", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    decision = workflow.decide(_signal(confidence=0.99))
    assert decision.action == WorkflowAction.CREATE_PROPOSAL


def test_requires_human_review_forces_proposal_even_in_practice_auto(repository):
    workflow = ApprovalWorkflow(repository, "practice_auto", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    decision = workflow.decide(_signal(confidence=0.99, requires_human_review=True))
    assert decision.action == WorkflowAction.CREATE_PROPOSAL


def test_practice_auto_executes_when_confidence_sufficient(repository):
    workflow = ApprovalWorkflow(repository, "practice_auto", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    decision = workflow.decide(_signal(confidence=0.95, requires_human_review=False))
    assert decision.action == WorkflowAction.EXECUTE_NOW


def test_practice_auto_falls_back_to_proposal_when_confidence_low(repository):
    workflow = ApprovalWorkflow(repository, "practice_auto", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    decision = workflow.decide(_signal(confidence=0.5, requires_human_review=False))
    assert decision.action == WorkflowAction.CREATE_PROPOSAL


def test_approve_proposal_flow(repository):
    workflow = ApprovalWorkflow(repository, "approval", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    proposal_id = workflow.create_proposal("sig-1", NOW)
    assert workflow.approve(proposal_id, "tester", NOW + timedelta(minutes=1))
    proposal = repository.get_proposal(proposal_id)
    assert proposal["status"] == "approved"


def test_expired_proposal_cannot_be_approved(repository):
    workflow = ApprovalWorkflow(repository, "approval", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    proposal_id = workflow.create_proposal("sig-1", NOW)
    approved = workflow.approve(proposal_id, "tester", NOW + timedelta(minutes=10))
    assert not approved
    proposal = repository.get_proposal(proposal_id)
    assert proposal["status"] == "expired"


def test_reject_proposal_flow(repository):
    workflow = ApprovalWorkflow(repository, "approval", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    proposal_id = workflow.create_proposal("sig-1", NOW)
    assert workflow.reject(proposal_id, "tester", NOW)
    proposal = repository.get_proposal(proposal_id)
    assert proposal["status"] == "rejected"


def test_auto_approve_executes_even_when_requires_human_review(repository):
    workflow = ApprovalWorkflow(repository, "practice_auto", proposal_expiry_minutes=5, min_signal_confidence=0.9, auto_approve=True)
    decision = workflow.decide(_signal(confidence=0.5, requires_human_review=True))
    assert decision.action == WorkflowAction.EXECUTE_NOW


def test_auto_approve_executes_in_approval_mode_too(repository):
    workflow = ApprovalWorkflow(repository, "approval", proposal_expiry_minutes=5, min_signal_confidence=0.9, auto_approve=True)
    decision = workflow.decide(_signal(confidence=0.99))
    assert decision.action == WorkflowAction.EXECUTE_NOW


def test_auto_approve_never_bypasses_observe_mode(repository):
    workflow = ApprovalWorkflow(repository, "observe", proposal_expiry_minutes=5, min_signal_confidence=0.9, auto_approve=True)
    decision = workflow.decide(_signal(confidence=0.99))
    assert decision.action == WorkflowAction.LOG_HYPOTHETICAL


def test_set_auto_approve_toggles_live(repository):
    workflow = ApprovalWorkflow(repository, "practice_auto", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    assert workflow.decide(_signal(confidence=0.5)).action == WorkflowAction.CREATE_PROPOSAL
    workflow.set_auto_approve(True)
    assert workflow.decide(_signal(confidence=0.5)).action == WorkflowAction.EXECUTE_NOW
    workflow.set_auto_approve(False)
    assert workflow.decide(_signal(confidence=0.5)).action == WorkflowAction.CREATE_PROPOSAL


def test_cannot_approve_already_decided_proposal(repository):
    workflow = ApprovalWorkflow(repository, "approval", proposal_expiry_minutes=5, min_signal_confidence=0.9)
    proposal_id = workflow.create_proposal("sig-1", NOW)
    workflow.reject(proposal_id, "tester", NOW)
    assert not workflow.approve(proposal_id, "tester", NOW)
