"""The `dispute-triage` output schema: a closed set of actions and the completeness rules.

Each of these was a surviving mutant before: the closed set, `extra="forbid"`, the
`tool_choice`-matches-the-action rule and the per-action completeness rules were all
asserted only against constants built from the same enum, or not at all. Each test here
fails when the rule it names is removed from `p2p/disputes/schema.py`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from p2p.disputes.schema import (
    ACTION_TOOLS,
    CREDIT_LIMIT_MINOR,
    TRANSCRIPT_CAP,
    DisputeStep,
    NextAction,
)


def _step(**fields: object) -> DisputeStep:
    payload: dict[str, object] = {
        "next_action": "ask_question",
        "tool_choice": None,
        "draft_reply": "A reply.",
        "question": "What quantity was received?",
    }
    payload.update(fields)
    return DisputeStep.model_validate(payload)


def test_the_set_of_next_actions_is_closed() -> None:
    assert _step().next_action is NextAction.ASK_QUESTION
    for named in ("approve", "apply_credit", "pay_invoice", "ESCALATE", "", "resolve_case"):
        with pytest.raises(ValidationError, match="not in the closed set"):
            _step(next_action=named)


def test_the_action_set_and_the_tool_map_agree() -> None:
    """Adding a member to `NextAction` without an `ACTION_TOOLS` entry survived every other
    test: validation would then raise `KeyError` inside the model validator rather than
    refusing the action, and `take_step` would look up a tool that does not exist."""
    assert set(ACTION_TOOLS) == set(NextAction)
    assert len(NextAction) == 8


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        _step(apply_now=True)


def test_tool_choice_must_name_the_tool_for_the_action() -> None:
    for action, tool in ACTION_TOOLS.items():
        fields: dict[str, object] = {"next_action": action.value, "tool_choice": tool}
        if action is NextAction.PROPOSE_CREDIT:
            fields |= {"amount": 100, "currency": "USD", "reason": "short delivery"}
        if action is NextAction.HOLD_PAYMENT:
            fields |= {"reason": "under investigation"}
        assert _step(**fields).next_action is action
        wrong = "get_invoice" if tool != "get_invoice" else "find_supplier"
        with pytest.raises(ValidationError, match="tool_choice must name the tool"):
            _step(**(fields | {"tool_choice": wrong}))
        with pytest.raises(ValidationError, match="tool_choice must name the tool"):
            _step(**(fields | {"tool_choice": None if tool is not None else "get_invoice"}))


def test_each_action_must_carry_what_it_needs() -> None:
    credit: dict[str, object] = {
        "next_action": "propose_credit",
        "tool_choice": "issue_credit_memo",
        "amount": CREDIT_LIMIT_MINOR + 1,
        "currency": "USD",
        "reason": "short delivery",
    }
    assert _step(**credit).amount == CREDIT_LIMIT_MINOR + 1
    for missing in ("amount", "currency", "reason"):
        with pytest.raises(ValidationError, match="propose_credit needs"):
            _step(**(credit | {missing: None}))
    hold: dict[str, object] = {
        "next_action": "hold_payment",
        "tool_choice": "hold_payment",
        "reason": "under investigation",
    }
    assert _step(**hold).reason == "under investigation"
    with pytest.raises(ValidationError, match="hold_payment needs a reason"):
        _step(**(hold | {"reason": None}))
    with pytest.raises(ValidationError, match="ask_question needs a question"):
        _step(question=None)


def test_the_task_caps_are_the_written_ones() -> None:
    assert TRANSCRIPT_CAP == 12_000
    assert CREDIT_LIMIT_MINOR == 250_000
