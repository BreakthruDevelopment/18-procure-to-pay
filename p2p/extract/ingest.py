"""`p2p ingest`: extract one invoice, validate, repair, record `extraction`.

A draft carries a number, never an id. `find_invoices_by_number` is how the document
reaches its seeded row; a document with no matching row is refused rather than
inserted. A successful extraction of a `received` invoice moves it to `extracted`.
Re-ingesting an already-advanced invoice records a new extraction row and writes no
state. This command stages nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from agent_container.core.budget import Budget
from agent_container.core.errors import ReplayFormatError
from agent_container.core.provider import ModelProvider
from agent_container.core.replay import ReplayProvider

from p2p.core.access import Action, Principal, Role, assert_can
from p2p.core.erp import Erp, Extraction, InvoiceState
from p2p.core.errors import P2PError, ValidationFailed
from p2p.extract.extract import ExtractResult, extract_draft
from p2p.extract.schema import TASK_NAME, InvoiceDraft


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What `p2p ingest` prints, and what a later batch item will count."""

    invoice_id: str | None
    number: str | None
    state: InvoiceState | None
    attempts: int
    repaired: bool
    confidence: float | None
    draft: InvoiceDraft | None
    already_advanced: bool
    extraction: Extraction | None


def prepare_document(text: str) -> str:
    """Refuse an empty or NUL-bearing file; the extract loop caps length."""
    if "\x00" in text:
        raise ValidationFailed("the document is unreadable")
    if not text.strip():
        raise ValidationFailed("the document is unreadable")
    return text


async def ingest(
    erp: Erp,
    principal: Principal,
    document: str,
    provider: ModelProvider,
    *,
    budget: Budget | None = None,
    fence_nonce: str | None = None,
) -> IngestOutcome:
    """Extract, bind the draft to a seeded invoice, record the trail, maybe advance."""
    assert_can(principal, Action.RECORD_EXTRACTION)
    text = prepare_document(document)
    result = await extract_draft(text, provider, budget=budget, fence_nonce=fence_nonce)
    if result.draft is None:
        _record_failed(erp, principal, result)
        raise ValidationFailed("needs_human")

    matches = erp.find_invoices_by_number(principal, result.draft.number)
    if not matches:
        raise ValidationFailed("no matching invoice row")
    invoice = matches[0]
    already = invoice.state is not InvoiceState.RECEIVED
    confidence = result.draft.confidence.overall()
    recorded = erp.record_extraction(
        principal,
        invoice.id,
        attempts=result.attempts,
        repaired=result.repaired,
        confidence=confidence,
        fields=_fields(result.draft),
        model_task=TASK_NAME,
    )
    if not already:
        invoice = erp.advance_invoice(principal, invoice.id, InvoiceState.EXTRACTED)
    return IngestOutcome(
        invoice_id=invoice.id,
        number=result.draft.number,
        state=invoice.state,
        attempts=result.attempts,
        repaired=result.repaired,
        confidence=confidence,
        draft=result.draft,
        already_advanced=already,
        extraction=recorded,
    )


def format_outcome(outcome: IngestOutcome) -> str:
    invoice = outcome.invoice_id if outcome.invoice_id is not None else "none"
    number = outcome.number if outcome.number is not None else "none"
    state = outcome.state.value if outcome.state is not None else "none"
    lines = [
        f"invoice {invoice}",
        f"number {number}",
        f"state {state}",
        f"attempts {outcome.attempts}",
        f"repaired {_flag(outcome.repaired)}",
    ]
    if outcome.already_advanced:
        lines.append("already advanced")
    if outcome.confidence is not None:
        lines.append(f"confidence {outcome.confidence:.2f}")
    return "\n".join(lines)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `p2p ingest` on the root parser."""
    ingest_cmd = subparsers.add_parser(
        "ingest",
        help="Extract one invoice, validate, repair, and record the extraction.",
    )
    ingest_cmd.add_argument("file", help="Path to the invoice document.")
    ingest_cmd.add_argument("--database", required=True, help="Path to the ERP sqlite file.")
    ingest_cmd.add_argument("--subject", required=True, help="Who the command acts as.")
    ingest_cmd.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in Role],
        help="The role the command acts as.",
    )
    ingest_cmd.add_argument(
        "--company-code",
        required=True,
        help="The company code this command is scoped to.",
    )
    ingest_cmd.add_argument(
        "--replay",
        required=True,
        help="Recorded replies JSON for the invoice-extract task.",
    )
    ingest_cmd.set_defaults(handler=_handle_ingest)


def run_ingest(
    erp: Erp,
    principal: Principal,
    document: str,
    provider: ModelProvider,
    *,
    budget: Budget | None = None,
    fence_nonce: str | None = None,
) -> IngestOutcome:
    """In-process entry for tests that inject a provider instead of a file."""
    return asyncio.run(
        ingest(
            erp,
            principal,
            document,
            provider,
            budget=budget,
            fence_nonce=fence_nonce,
        )
    )


def _handle_ingest(args: argparse.Namespace) -> int:
    try:
        principal = Principal(args.subject, Role(args.role), args.company_code)
    except ValueError:
        print("a principal needs a subject and a company code", file=sys.stderr)
        return 2
    try:
        document = _read_file(args.file)
        provider = _replay(args.replay)
        with Erp.open(args.database) as erp:
            outcome = run_ingest(erp, principal, document, provider)
        print(format_outcome(outcome))
        return 0
    except P2PError as failure:
        print(f"{failure.category.value}: {failure}", file=sys.stderr)
        return 1
    except OSError:
        print("the database could not be opened", file=sys.stderr)
        return 1


def _read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationFailed("the document is unreadable") from exc


def _replay(path: str) -> ModelProvider:
    replay_path = Path(path)
    if not replay_path.is_file():
        raise ValidationFailed("the recording could not be read")
    try:
        return ReplayProvider.from_file(replay_path)
    except ReplayFormatError as exc:
        raise ValidationFailed("the recording could not be read") from exc


def _record_failed(erp: Erp, principal: Principal, result: ExtractResult) -> None:
    number = _number_from(result)
    if number is None:
        return
    matches = erp.find_invoices_by_number(principal, number)
    if not matches:
        return
    erp.record_extraction(
        principal,
        matches[0].id,
        attempts=result.attempts,
        repaired=False,
        confidence=None,
        fields=_failed_fields(result),
        model_task=TASK_NAME,
    )


def _number_from(result: ExtractResult) -> str | None:
    if result.draft is not None:
        return result.draft.number
    value = result.last_input.get("number")
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text[:64]
    return None


def _fields(draft: InvoiceDraft) -> dict[str, object]:
    dumped = draft.model_dump(mode="json")
    if not isinstance(dumped, dict):
        raise ValidationFailed("the draft could not be recorded")
    return dumped


def _failed_fields(result: ExtractResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "failures": list(result.failures),
        "last_input": dict(result.last_input),
    }
    return payload


def _flag(value: bool) -> str:
    return "true" if value else "false"


def fixture_path(name: str) -> Path:
    """A committed recorded-reply file, for tests and for 18.8's demo."""
    return Path(str(resources.files("p2p.extract").joinpath("fixtures", name)))


def load_fixture(name: str) -> Mapping[str, object]:
    raw: object = json.loads(fixture_path(name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValidationFailed("the recording could not be read")
    return raw
