"""`assert_can` itself: the four rules, and the invariants the role table has to keep."""

from __future__ import annotations

import pytest

from p2p.core.access import (
    GATEWAY_READ_ACTIONS,
    READ_ACTIONS,
    WRITE_ACTIONS,
    Action,
    Principal,
    Role,
    allowed_actions,
    assert_can,
)
from p2p.core.errors import AccessDenied


def test_reads_and_writes_partition_every_action() -> None:
    every_action = set(Action)
    assert every_action == READ_ACTIONS | WRITE_ACTIONS
    assert not READ_ACTIONS & WRITE_ACTIONS


def test_the_service_principal_holds_read_actions_only() -> None:
    held = allowed_actions(Role.SERVICE)
    assert held == set(GATEWAY_READ_ACTIONS)
    assert not held & WRITE_ACTIONS
    assert not Principal("svc", Role.SERVICE, "1000").may_write


def test_every_role_may_use_the_gateway_reads() -> None:
    for role in Role:
        assert allowed_actions(role) >= GATEWAY_READ_ACTIONS, role


def test_a_clerk_never_decides_a_change_and_an_approver_never_stages_one() -> None:
    assert Action.DECIDE_CHANGE not in allowed_actions(Role.CLERK)
    assert Action.DECIDE_CHANGE not in allowed_actions(Role.DISPUTES)
    assert Action.DECIDE_CHANGE in allowed_actions(Role.APPROVER)
    for staging in (Action.STAGE_POSTING, Action.STAGE_CREDIT_MEMO, Action.STAGE_PAYMENT_HOLD):
        assert staging not in allowed_actions(Role.APPROVER), staging


def test_only_a_clerk_stages_a_posting_and_only_disputes_stage_money() -> None:
    assert Action.STAGE_POSTING in allowed_actions(Role.CLERK)
    assert Action.STAGE_POSTING not in allowed_actions(Role.DISPUTES)
    for money in (Action.STAGE_CREDIT_MEMO, Action.STAGE_PAYMENT_HOLD):
        assert money in allowed_actions(Role.DISPUTES), money
        assert money not in allowed_actions(Role.CLERK), money


def test_rule_one_the_role_must_hold_the_action() -> None:
    clerk = Principal("clerk@demo", Role.CLERK, "1000")
    with pytest.raises(AccessDenied, match="may not decide_change"):
        assert_can(clerk, Action.DECIDE_CHANGE)
    assert_can(clerk, Action.STAGE_POSTING)


def test_rule_two_a_row_in_another_company_code_is_refused() -> None:
    clerk = Principal("clerk@demo", Role.CLERK, "1000")
    with pytest.raises(AccessDenied, match="not in the principal's company code"):
        assert_can(clerk, Action.READ_INVOICE, {"company_code": "2000"})
    assert_can(clerk, Action.READ_INVOICE, {"company_code": "1000"})


def test_rule_two_leaks_neither_company_code_into_the_message() -> None:
    clerk = Principal("clerk@demo", Role.CLERK, "1000")
    with pytest.raises(AccessDenied) as refused:
        assert_can(clerk, Action.READ_INVOICE, {"company_code": "2000"})
    assert "2000" not in str(refused.value)
    assert "1000" not in str(refused.value)


def test_rule_two_fails_closed_on_a_row_that_carries_no_company_code() -> None:
    clerk = Principal("clerk@demo", Role.CLERK, "1000")
    with pytest.raises(AccessDenied, match="carries no company code"):
        assert_can(clerk, Action.READ_INVOICE, {"staged_by": "clerk@demo"})


def test_rule_three_a_disputes_principal_sees_only_its_own_cases() -> None:
    mine = Principal("disputes@demo", Role.DISPUTES, "1000")
    row = {"company_code": "1000", "assigned_to": "disputes2@demo"}
    with pytest.raises(AccessDenied, match="assigned to it"):
        assert_can(mine, Action.READ_CASE, row)
    assert_can(mine, Action.READ_CASE, {**row, "assigned_to": "disputes@demo"})


def test_rule_three_does_not_constrain_an_approver() -> None:
    approver = Principal("approver@demo", Role.APPROVER, "1000")
    assert_can(approver, Action.READ_CASE, {"company_code": "1000", "assigned_to": "someone@else"})


def test_rule_four_separation_of_duties_on_a_change_the_principal_staged() -> None:
    approver = Principal("approver@demo", Role.APPROVER, "1000")
    with pytest.raises(AccessDenied, match="separation of duties"):
        assert_can(
            approver,
            Action.DECIDE_CHANGE,
            {"company_code": "1000", "staged_by": "approver@demo"},
        )
    assert_can(approver, Action.DECIDE_CHANGE, {"company_code": "1000", "staged_by": "clerk@demo"})


def test_a_principal_needs_a_subject_and_a_company_code() -> None:
    with pytest.raises(ValueError, match="subject"):
        Principal("", Role.CLERK, "1000")
    with pytest.raises(ValueError, match="company code"):
        Principal("clerk@demo", Role.CLERK, "")
