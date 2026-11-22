"""`p2p match`, `p2p staged`, `p2p review` and `p2p approve`.

`p2p staged` prints `list_staged` in the order the accessor returns it. The visibility rule
lives in that accessor; this command does not filter or sort. `p2p review` and `p2p approve`
are generic over a posting, a credit memo and a payment hold. Approve calls `apply_change`
and no other write. A posting's review prints only the band, the check fields and the
reasons this package knows, never an arbitrary payload string.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_container.core.budget import Budget
from agent_container.core.errors import ReplayFormatError
from agent_container.core.provider import ModelProvider
from agent_container.core.replay import ReplayProvider
from pydantic import ValidationError

from p2p.core.access import Principal, Role
from p2p.core.erp import ApplyOutcome, ChangeKind, Decision, Erp, StagedChangeRow
from p2p.core.errors import P2PError, ValidationFailed
from p2p.match.schema import CHECKABLE, KNOWN_WHYS, PostingFacts, VarianceItem
from p2p.match.workflow import MatchResult, match_invoice

Handler = Callable[[argparse.Namespace, Erp, Principal], int]


def run_match(
    erp: Erp,
    principal: Principal,
    invoice_id: str,
    provider: ModelProvider,
    *,
    budget: Budget | None = None,
    fence_nonce: str | None = None,
) -> MatchResult:
    """In-process entry for tests that inject a provider instead of a file."""
    return asyncio.run(
        match_invoice(
            erp,
            principal,
            invoice_id,
            provider,
            budget=budget,
            fence_nonce=fence_nonce,
        )
    )


def format_match(result: MatchResult) -> str:
    """One match. The change key, when a posting was staged, is the last line."""
    lines = [
        f"invoice {result.invoice_id}",
        f"state {result.state.value}",
        f"verdict {result.verdict.value}",
        f"confidence {result.confidence:.2f}",
        f"band {result.band.value}",
        f"needs_human {_flag(result.needs_human)}",
        f"why {result.why}",
        f"steps {', '.join(result.steps)}",
    ]
    lines.extend(_variance_line(item) for item in result.variances)
    if result.fields_to_check:
        lines.append(f"check {','.join(result.fields_to_check)}")
    if result.change_key is not None:
        lines.append(result.change_key)
    return "\n".join(lines)


def format_staged(rows: tuple[StagedChangeRow, ...]) -> str:
    """The accessor's rows, in its order. Nothing is added and nothing is dropped."""
    if not rows:
        return "no staged changes"
    return "\n".join(
        f"{row.change_key} {row.kind.value} {row.summary} staged_by {row.staged_by}" for row in rows
    )


def format_review(change: StagedChangeRow) -> str:
    """One staged change. A posting may show a band; a credit or a hold never does."""
    decision = "none" if change.decision is None else change.decision.kind.value
    lines = [
        f"change {change.change_key}",
        f"kind {change.kind.value}",
        f"summary {change.summary}",
        f"staged_by {change.staged_by}",
        f"decision {decision}",
    ]
    if change.kind is ChangeKind.POSTING:
        lines.extend(_posting_lines(change.payload))
    else:
        lines.extend(_money_lines(change))
    return "\n".join(lines)


def format_apply(outcome: ApplyOutcome) -> str:
    """An apply or reject, including the stated no-op when the change was already decided."""
    state = outcome.invoice_state.value if outcome.invoice_state is not None else "none"
    return "\n".join(
        [
            f"change {outcome.change_key}",
            f"kind {outcome.kind.value}",
            f"decision {outcome.decision.kind.value}",
            f"already_decided {_flag(outcome.already_decided)}",
            f"detail {outcome.detail}",
            f"invoice_state {state}",
        ]
    )


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `p2p match`, `p2p staged`, `p2p review` and `p2p approve`."""
    principal = argparse.ArgumentParser(add_help=False)
    principal.add_argument("--database", required=True, help="Path to the ERP sqlite file.")
    principal.add_argument("--subject", required=True, help="Who the command acts as.")
    principal.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in Role],
        help="The role the command acts as.",
    )
    principal.add_argument(
        "--company-code",
        required=True,
        help="The company code this command is scoped to.",
    )

    match = subparsers.add_parser(
        "match",
        parents=[principal],
        help="Run the four-step match and stage a posting when the rules allow.",
    )
    match.add_argument("invoice", help="Invoice id to match.")
    match.add_argument("--replay", required=True, help="Recorded replies JSON for invoice-match.")
    match.set_defaults(handler=_handle_match)

    staged = subparsers.add_parser(
        "staged",
        parents=[principal],
        help="List the staged changes this principal may see.",
    )
    staged.set_defaults(handler=_handle_staged)

    review = subparsers.add_parser(
        "review",
        parents=[principal],
        help="Show one staged change and, for a posting, its confidence band.",
    )
    review.add_argument("change", help="Staged change key.")
    review.set_defaults(handler=_handle_review)

    approve = subparsers.add_parser(
        "approve",
        parents=[principal],
        help="Apply or reject any staged change. A second decision is a no-op.",
    )
    approve.add_argument("change", help="Staged change key.")
    choice = approve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--yes", action="store_true", help="Apply the change.")
    choice.add_argument("--reject", metavar="REASON", help="Reject the change and record why.")
    approve.set_defaults(handler=_handle_approve)


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _variance_line(item: VarianceItem) -> str:
    return (
        f"variance {item.kind.value} line {item.line_no} "
        f"expected {item.expected} actual {item.actual}"
    )


def _known_why(text: str | None) -> str | None:
    """Keep only the fixed reasons. A payload's other words are not shown."""
    if text is None:
        return None
    parts = [part for part in text.split("; ") if part in KNOWN_WHYS]
    if not parts:
        return None
    return "; ".join(parts)


def _facts(payload: Mapping[str, object] | None) -> PostingFacts | None:
    if payload is None:
        return None
    try:
        return PostingFacts.model_validate(dict(payload))
    except ValidationError:
        return None


def _posting_lines(payload: Mapping[str, object] | None) -> list[str]:
    facts = _facts(payload)
    if facts is None:
        return []
    lines: list[str] = []
    if facts.band is not None:
        lines.append(f"band {facts.band.value}")
    if facts.confidence is not None:
        lines.append(f"confidence {facts.confidence:.2f}")
    if facts.needs_human is not None:
        lines.append(f"needs_human {_flag(facts.needs_human)}")
    why = _known_why(facts.why)
    if why is not None:
        lines.append(f"why {why}")
    lines.extend(_variance_line(item) for item in facts.variances)
    fields = [name for name in facts.fields_to_check if name in CHECKABLE]
    lines.append(f"check {','.join(fields) if fields else 'none'}")
    return lines


def _money_lines(change: StagedChangeRow) -> list[str]:
    lines: list[str] = []
    if change.amount is not None:
        lines.append(f"amount {change.amount}")
    if change.currency is not None:
        lines.append(f"currency {change.currency}")
    if change.reason is not None:
        lines.append(f"reason {change.reason}")
    if change.supplier_id is not None:
        lines.append(f"supplier {change.supplier_id}")
    if change.invoice_id is not None:
        lines.append(f"invoice {change.invoice_id}")
    if change.case_id is not None:
        lines.append(f"case {change.case_id}")
    return lines


def _principal(args: argparse.Namespace) -> Principal:
    return Principal(args.subject, Role(args.role), args.company_code)


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


def _handle_match(args: argparse.Namespace) -> int:
    return _with_erp(args, _run_match)


def _handle_staged(args: argparse.Namespace) -> int:
    return _with_erp(args, _run_staged)


def _handle_review(args: argparse.Namespace) -> int:
    return _with_erp(args, _run_review)


def _handle_approve(args: argparse.Namespace) -> int:
    return _with_erp(args, _run_approve)


def _run_match(args: argparse.Namespace, erp: Erp, principal: Principal) -> int:
    provider = _replay(args.replay)
    print(format_match(run_match(erp, principal, args.invoice, provider)))
    return 0


def _run_staged(args: argparse.Namespace, erp: Erp, principal: Principal) -> int:
    print(format_staged(erp.list_staged(principal)))
    return 0


def _run_review(args: argparse.Namespace, erp: Erp, principal: Principal) -> int:
    print(format_review(erp.get_staged(principal, args.change)))
    return 0


def _run_approve(args: argparse.Namespace, erp: Erp, principal: Principal) -> int:
    decision = Decision.applied() if args.reject is None else Decision.rejected(args.reject)
    print(format_apply(erp.apply_change(principal, args.change, decision)))
    return 0


def _replay(path: str) -> ModelProvider:
    replay_path = Path(path)
    if not replay_path.is_file():
        raise ValidationFailed("the recording could not be read")
    try:
        return ReplayProvider.from_file(replay_path)
    except ReplayFormatError as exc:
        raise ValidationFailed("the recording could not be read") from exc
