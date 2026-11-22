"""The dispute_case state machine: allowed actions and forward hops."""

from __future__ import annotations

import pytest

from p2p.core.erp import CaseState
from p2p.core.errors import ValidationFailed
from p2p.disputes.schema import NextAction
from p2p.disputes.state import CHAIN, hops, next_state, phase_for


def test_the_chain_is_the_spec_order() -> None:
    assert CHAIN == (
        CaseState.OPEN,
        CaseState.INVESTIGATING,
        CaseState.CREDIT_PROPOSED,
        CaseState.ESCALATED,
        CaseState.RESOLVED,
    )


def test_open_moves_to_investigating_on_a_read() -> None:
    assert next_state(CaseState.OPEN, NextAction.READ_INVOICE, escalate=False) is (
        CaseState.INVESTIGATING
    )


def test_proposing_a_credit_and_escalating_visits_both_states() -> None:
    assert hops(CaseState.OPEN, NextAction.PROPOSE_CREDIT, escalate=True) == (
        CaseState.CREDIT_PROPOSED,
        CaseState.ESCALATED,
    )
    assert hops(CaseState.INVESTIGATING, NextAction.PROPOSE_CREDIT, escalate=True) == (
        CaseState.CREDIT_PROPOSED,
        CaseState.ESCALATED,
    )


def test_a_resolved_case_accepts_no_action() -> None:
    assert phase_for(CaseState.RESOLVED).allowed() == frozenset()
    with pytest.raises(ValidationFailed, match="resolved case has no next step"):
        phase_for(CaseState.RESOLVED).transition(NextAction.RESOLVE)


def test_escalation_does_not_reopen_a_resolved_case() -> None:
    assert next_state(CaseState.CREDIT_PROPOSED, NextAction.RESOLVE, escalate=True) is (
        CaseState.RESOLVED
    )


def test_an_escalated_case_cannot_go_back_to_credit_proposed() -> None:
    assert next_state(CaseState.ESCALATED, NextAction.PROPOSE_CREDIT, escalate=False) is (
        CaseState.ESCALATED
    )
    with pytest.raises(ValidationFailed, match="not available"):
        phase_for(CaseState.ESCALATED).transition(NextAction.ASK_QUESTION)
