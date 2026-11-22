"""The `dispute_case` lifecycle as the State pattern.

`open → investigating → credit_proposed → escalated → resolved`. Each phase names the
actions it will accept and the state those actions move to. The ERP accessor refuses a
backwards move; this module is what decides the forward step, so a resolved case cannot
be reopened from here either.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Final

from p2p.core.erp import CaseState
from p2p.core.errors import ValidationFailed
from p2p.disputes.schema import NextAction

CHAIN: Final[tuple[CaseState, ...]] = (
    CaseState.OPEN,
    CaseState.INVESTIGATING,
    CaseState.CREDIT_PROPOSED,
    CaseState.ESCALATED,
    CaseState.RESOLVED,
)

_READS: Final[frozenset[NextAction]] = frozenset(
    {NextAction.READ_SUPPLIER, NextAction.READ_INVOICE, NextAction.READ_PAYMENT}
)


class Phase(ABC):
    """One state of the case. Subclasses are the only things that know their own exits."""

    state: ClassVar[CaseState]

    @abstractmethod
    def allowed(self) -> frozenset[NextAction]:
        """The closed set of next actions this phase will execute."""

    def transition(self, action: NextAction) -> CaseState:
        if action not in self.allowed():
            raise ValidationFailed("this action is not available in the current case state")
        return self._transition(action)

    @abstractmethod
    def _transition(self, action: NextAction) -> CaseState:
        """The state this action moves to, ignoring escalation. Escalation is applied on top."""


class Open(Phase):
    state = CaseState.OPEN

    def allowed(self) -> frozenset[NextAction]:
        return frozenset(NextAction)

    def _transition(self, action: NextAction) -> CaseState:
        if action is NextAction.RESOLVE:
            return CaseState.RESOLVED
        if action is NextAction.PROPOSE_CREDIT:
            return CaseState.CREDIT_PROPOSED
        if action is NextAction.ESCALATE:
            return CaseState.ESCALATED
        return CaseState.INVESTIGATING


class Investigating(Phase):
    state = CaseState.INVESTIGATING

    def allowed(self) -> frozenset[NextAction]:
        return frozenset(NextAction)

    def _transition(self, action: NextAction) -> CaseState:
        if action is NextAction.RESOLVE:
            return CaseState.RESOLVED
        if action is NextAction.PROPOSE_CREDIT:
            return CaseState.CREDIT_PROPOSED
        if action is NextAction.ESCALATE:
            return CaseState.ESCALATED
        return CaseState.INVESTIGATING


class CreditProposed(Phase):
    state = CaseState.CREDIT_PROPOSED

    def allowed(self) -> frozenset[NextAction]:
        return _READS | {
            NextAction.PROPOSE_CREDIT,
            NextAction.HOLD_PAYMENT,
            NextAction.ESCALATE,
            NextAction.RESOLVE,
        }

    def _transition(self, action: NextAction) -> CaseState:
        if action is NextAction.RESOLVE:
            return CaseState.RESOLVED
        if action is NextAction.ESCALATE:
            return CaseState.ESCALATED
        return CaseState.CREDIT_PROPOSED


class Escalated(Phase):
    state = CaseState.ESCALATED

    def allowed(self) -> frozenset[NextAction]:
        return _READS | {
            NextAction.PROPOSE_CREDIT,
            NextAction.HOLD_PAYMENT,
            NextAction.RESOLVE,
        }

    def _transition(self, action: NextAction) -> CaseState:
        if action is NextAction.RESOLVE:
            return CaseState.RESOLVED
        return CaseState.ESCALATED


class Resolved(Phase):
    state = CaseState.RESOLVED

    def allowed(self) -> frozenset[NextAction]:
        return frozenset()

    def transition(self, action: NextAction) -> CaseState:
        del action
        raise ValidationFailed("a resolved case has no next step")

    def _transition(self, action: NextAction) -> CaseState:
        del action
        raise ValidationFailed("a resolved case has no next step")


_PHASES: Final[dict[CaseState, Phase]] = {
    CaseState.OPEN: Open(),
    CaseState.INVESTIGATING: Investigating(),
    CaseState.CREDIT_PROPOSED: CreditProposed(),
    CaseState.ESCALATED: Escalated(),
    CaseState.RESOLVED: Resolved(),
}


def phase_for(state: CaseState) -> Phase:
    return _PHASES[state]


def next_state(current: CaseState, action: NextAction, *, escalate: bool) -> CaseState:
    """The state after `action`, with escalation taking the case to `escalated` unless it resolved.

    Proposing a credit and matching a criterion in the same step visits `credit_proposed` then
    `escalated`: the chain is a sequence, and skipping the credit state would lose that it was
    proposed. `advance_case` is called for each hop.
    """
    target = phase_for(current).transition(action)
    if escalate and target is not CaseState.RESOLVED:
        return CaseState.ESCALATED
    return target


def hops(current: CaseState, action: NextAction, *, escalate: bool) -> tuple[CaseState, ...]:
    """The forward states to visit, in order, including an intermediate `credit_proposed` hop."""
    target = next_state(current, action, escalate=escalate)
    if target is current:
        return ()
    if (
        escalate
        and action is NextAction.PROPOSE_CREDIT
        and current not in {CaseState.CREDIT_PROPOSED, CaseState.ESCALATED, CaseState.RESOLVED}
    ):
        return (CaseState.CREDIT_PROPOSED, CaseState.ESCALATED)
    return (target,)
