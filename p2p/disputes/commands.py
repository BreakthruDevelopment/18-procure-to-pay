"""`p2p dispute open/step/show`. Formats the desk's results for a terminal.

`p2p dispute step` prints the `change_key` as the last line when it staged a credit or a
hold, because that key is the only handle an approver has on the change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from agent_container.core.errors import ReplayFormatError
from agent_container.core.provider import ModelProvider
from agent_container.core.replay import ReplayProvider

from p2p.core.access import Principal, Role
from p2p.core.erp import DisputeCase, Erp
from p2p.core.errors import P2PError, ValidationFailed
from p2p.disputes.context import load_facts
from p2p.disputes.triage import StepOutcome, take_step

Handler = Callable[[argparse.Namespace, Erp, Principal], int]


def open_case(
    erp: Erp, principal: Principal, *, supplier_id: str, invoice_id: str | None
) -> DisputeCase:
    case = erp.open_case(principal, supplier_id=supplier_id, invoice_id=invoice_id)
    erp.append_case_event(principal, case.id, kind="opened", payload={"state": case.state.value})
    return erp.get_case(principal, case.id)


def format_opened(case: DisputeCase) -> str:
    invoice = case.invoice_id if case.invoice_id is not None else "none"
    return (
        f"opened {case.id}\n"
        f"state {case.state.value}\n"
        f"supplier {case.supplier_id}\n"
        f"invoice {invoice}"
    )


def format_show(erp: Erp, principal: Principal, case_id: str) -> str:
    facts = load_facts(erp, principal, case_id)
    case = facts.case
    invoice = case.invoice_id if case.invoice_id is not None else "none"
    reason = case.escalated_reason if case.escalated_reason is not None else "none"
    summary = case.summary if case.summary is not None else "none"
    lines = [
        f"id {case.id}",
        f"state {case.state.value}",
        f"supplier {case.supplier_id}",
        f"invoice {invoice}",
        f"assigned_to {case.assigned_to}",
        f"escalated_reason {reason}",
        f"summary {summary}",
        f"events {len(facts.window)}",
    ]
    for event in facts.window:
        extra = _event_extra(event.kind, event.payload)
        lines.append(f"{event.seq} {event.kind}{extra}")
    return "\n".join(lines)


def _event_extra(kind: str, payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if kind == "question":
        text = payload.get("text")
        return f" {text}" if isinstance(text, str) else ""
    if kind == "staged":
        key = payload.get("change_key")
        return f" {key}" if isinstance(key, str) else ""
    if kind == "escalated":
        criterion = payload.get("criterion")
        return f" {criterion}" if isinstance(criterion, str) else ""
    return ""


def format_step(outcome: StepOutcome) -> str:
    lines = [
        f"case {outcome.case.id}",
        f"state {outcome.case.state.value}",
    ]
    if outcome.action is not None:
        lines.append(f"action {outcome.action.value}")
    if outcome.escalate is not None:
        lines.append(f"escalate {outcome.escalate}")
    if outcome.question is not None:
        lines.append(outcome.question)
    elif outcome.refused is not None:
        lines.append(outcome.refused)
    else:
        lines.append(outcome.draft_reply)
    if outcome.change_key is not None:
        lines.append(outcome.change_key)
    return "\n".join(lines)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `p2p dispute open/step/show` on the root parser."""
    erp = argparse.ArgumentParser(add_help=False)
    erp.add_argument("--database", required=True, help="Path to the ERP sqlite file.")
    erp.add_argument("--subject", required=True, help="Who the command acts as.")
    erp.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in Role],
        help="The role the command acts as.",
    )
    erp.add_argument(
        "--company-code",
        required=True,
        help="The company code this command is scoped to.",
    )

    dispute = subparsers.add_parser("dispute", help="Run the supplier disputes desk.")
    commands = dispute.add_subparsers(dest="dispute_command", required=True)

    opened = commands.add_parser("open", parents=[erp], help="Open a dispute case.")
    opened.add_argument("--supplier", required=True, help="Supplier id the case is about.")
    opened.add_argument("--invoice", default=None, help="Invoice id the case is about, if any.")
    opened.set_defaults(handler=_handle_open)

    step = commands.add_parser("step", parents=[erp], help="Take one triage step on a case.")
    step.add_argument("case", help="Dispute case id.")
    step.add_argument(
        "--replay",
        required=True,
        help="Recorded replies JSON for the dispute-triage task.",
    )
    step.set_defaults(handler=_handle_step)

    show = commands.add_parser(
        "show", parents=[erp], help="Show a dispute case and its recent events."
    )
    show.add_argument("case", help="Dispute case id.")
    show.set_defaults(handler=_handle_show)


def _principal(args: argparse.Namespace) -> Principal:
    return Principal(args.subject, Role(args.role), args.company_code)


def _handle_open(args: argparse.Namespace) -> int:
    return _with_erp(args, _run_open)


def _handle_step(args: argparse.Namespace) -> int:
    return _with_erp(args, _run_step)


def _handle_show(args: argparse.Namespace) -> int:
    return _with_erp(args, _run_show)


def _with_erp(args: argparse.Namespace, run: Handler) -> int:
    try:
        principal = _principal(args)
    except ValueError:
        print("a principal needs a subject and a company code", file=sys.stderr)
        return 2
    try:
        with Erp.open(args.database) as erp:
            return run(args, erp, principal)
    except P2PError as failure:
        print(f"{failure.category.value}: {failure}", file=sys.stderr)
        return 1
    except OSError:
        print("the database could not be opened", file=sys.stderr)
        return 1


def _run_open(args: argparse.Namespace, erp: Erp, principal: Principal) -> int:
    case = open_case(erp, principal, supplier_id=args.supplier, invoice_id=args.invoice)
    print(format_opened(case))
    return 0


def _run_step(args: argparse.Namespace, erp: Erp, principal: Principal) -> int:
    provider = _replay(args.replay)
    outcome = asyncio.run(take_step(erp, principal, args.case, provider))
    print(format_step(outcome))
    return 0


def _run_show(args: argparse.Namespace, erp: Erp, principal: Principal) -> int:
    print(format_show(erp, principal, args.case))
    return 0


def _replay(path: str) -> ModelProvider:
    replay_path = Path(path)
    if not replay_path.is_file():
        raise ValidationFailed("the recording could not be read")
    try:
        return ReplayProvider.from_file(replay_path)
    except ReplayFormatError as exc:
        raise ValidationFailed("the recording could not be read") from exc


def run_step_with(
    erp: Erp, principal: Principal, case_id: str, provider: ModelProvider
) -> StepOutcome:
    """In-process entry for tests that inject a provider instead of a file."""
    return asyncio.run(take_step(erp, principal, case_id, provider))
