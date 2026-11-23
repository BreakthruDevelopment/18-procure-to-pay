"""`p2p batch <dir>`: ingest and match every document in the folder.

Each document is 18.4's `ingest` and, when that advances it, 18.5's `match_invoice`.
Nothing here writes a posting except by that call. One `Budget` covers the run. When
it is exhausted the document in hand is a failure and every document not yet started
is `not_reached`. A folder of more than 25 documents is refused before any of that,
and no `batch_run` row is recorded, because nothing ran.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from importlib import resources
from pathlib import Path

from agent_container.core.budget import Budget
from agent_container.core.errors import ReplayFormatError
from agent_container.core.provider import ModelProvider
from agent_container.core.replay import ReplayProvider
from agent_container.core.tool import ErrorCategory

from p2p.batch.summary import (
    BatchItem,
    BatchSummary,
    ItemOutcome,
    SkipReason,
    format_summary,
)
from p2p.core.access import Principal, Role
from p2p.core.erp import Erp
from p2p.core.errors import P2PError, ValidationFailed
from p2p.extract.ingest import ingest, prepare_document
from p2p.match.workflow import match_invoice

#: A demo bound. One summary stays readable on a terminal, and one run's cost stays predictable.
MAX_DOCUMENTS = 25
# Extract attempts at most twice. A match that has a purchase order asks the model three times.
_CALLS_PER_DOCUMENT = 5
_MAX_TURNS = MAX_DOCUMENTS * _CALLS_PER_DOCUMENT
_MAX_TOKENS = MAX_DOCUMENTS * 80_000
_MAX_SECONDS = float(MAX_DOCUMENTS * 60)


def seeded_folder() -> Path:
    """The twelve documents for INV-1001..INV-1012. Sorted, the seventh is unreadable."""
    return Path(str(resources.files("p2p.batch").joinpath("seeded")))


def collect_documents(folder: Path) -> tuple[Path, ...]:
    """The documents in `folder`, in filename order. Not its subfolders.

    Filename order is the order a position in the folder means: the seeded folder's
    seventh file is the unreadable one. A leading dot is filesystem metadata, not a
    document, so it cannot push a 25-document folder over the demo bound.
    """
    if not folder.is_dir():
        raise ValidationFailed("the batch folder is not a directory")
    found = [path for path in folder.iterdir() if path.is_file() and not path.name.startswith(".")]
    found.sort(key=lambda path: path.name)
    count = len(found)
    if count > MAX_DOCUMENTS:
        raise ValidationFailed(
            f"the folder holds {count} documents, and a batch refuses more than {MAX_DOCUMENTS}"
        )
    return tuple(found)


def read_document(path: Path) -> str:
    """The file's text, or the same unreadable refusal `p2p ingest` uses."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationFailed("the document is unreadable") from exc
    return prepare_document(text)


def run_batch(
    erp: Erp,
    principal: Principal,
    folder: Path,
    provider: ModelProvider,
    *,
    budget: Budget | None = None,
    fence_nonce: str | None = None,
) -> BatchSummary:
    """Ingest and match every document, record one `batch_run`, and return its summary."""
    return asyncio.run(
        _run_batch(
            erp,
            principal,
            folder,
            provider,
            budget=budget,
            fence_nonce=fence_nonce,
        )
    )


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `p2p batch` on the root parser."""
    batch = subparsers.add_parser(
        "batch",
        help="Ingest and match every document in a folder and print one summary.",
    )
    batch.add_argument("directory", metavar="dir", help="Folder of invoice documents.")
    batch.add_argument("--database", required=True, help="Path to the ERP sqlite file.")
    batch.add_argument("--subject", required=True, help="Who the command acts as.")
    batch.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in Role],
        help="The role the command acts as.",
    )
    batch.add_argument(
        "--company-code",
        required=True,
        help="The company code this command is scoped to.",
    )
    batch.add_argument(
        "--replay",
        required=True,
        help="Recorded replies JSON for the extract and match calls, in document order.",
    )
    batch.set_defaults(handler=_handle_batch)


async def _run_batch(
    erp: Erp,
    principal: Principal,
    folder: Path,
    provider: ModelProvider,
    *,
    budget: Budget | None,
    fence_nonce: str | None,
) -> BatchSummary:
    documents = collect_documents(folder)
    run_budget = budget if budget is not None else _new_budget()
    if not run_budget.started:
        run_budget.start()

    items: list[BatchItem] = []
    stopped: str | None = None
    for index, path in enumerate(documents):
        reason = _budget_reason(run_budget)
        if reason is not None:
            stopped = reason
            items.extend(_not_reached(later) for later in documents[index:])
            break
        item, cut = await _one(
            erp,
            principal,
            path,
            provider,
            run_budget,
            fence_nonce,
        )
        items.append(item)
        if cut is not None:
            # The budget stopped the *run* only if it left documents unattempted. A cut that
            # coincides with the last document ends a run in which every document was
            # attempted, so nothing about it is early: carrying the reason there would claim a
            # `not_reached` document that cannot exist, which `BatchSummary` rightly refuses.
            remaining = documents[index + 1 :]
            if remaining:
                stopped = cut
                items.extend(_not_reached(later) for later in remaining)
            break

    summary = _summarize(items, stopped)
    _record(erp, principal, summary)
    return summary


async def _one(
    erp: Erp,
    principal: Principal,
    path: Path,
    provider: ModelProvider,
    budget: Budget,
    fence_nonce: str | None,
) -> tuple[BatchItem, str | None]:
    try:
        item = await _process(erp, principal, path, provider, budget, fence_nonce)
    except P2PError as failure:
        # A lock is this document's failure and the run continues. The budget is the
        # only reason the documents after this one are left unattempted.
        return _failed(path, failure.category), _budget_reason(budget)
    return item, None


async def _process(
    erp: Erp,
    principal: Principal,
    path: Path,
    provider: ModelProvider,
    budget: Budget,
    fence_nonce: str | None,
) -> BatchItem:
    text = read_document(path)
    outcome = await ingest(
        erp,
        principal,
        text,
        provider,
        budget=budget,
        fence_nonce=fence_nonce,
    )
    # A previous run already moved this invoice. Ingest recorded a new extraction and
    # wrote no state. Matching would move it again, so the re-run stops here.
    if outcome.already_advanced:
        return _already(path)
    invoice_id = outcome.invoice_id
    if invoice_id is None:
        raise ValidationFailed("no matching invoice row")
    result = await match_invoice(
        erp,
        principal,
        invoice_id,
        provider,
        budget=budget,
        fence_nonce=fence_nonce,
    )
    return _ok(path, result.change_key)


def _new_budget() -> Budget:
    """Room for every document at the calls those two tasks actually make. Not a second cap."""
    return Budget(max_turns=_MAX_TURNS, max_tokens=_MAX_TOKENS, max_seconds=_MAX_SECONDS)


def _budget_reason(budget: Budget) -> str | None:
    exceeded = budget.check()
    if exceeded is None:
        return None
    return exceeded.message


def _summarize(items: list[BatchItem], stopped: str | None) -> BatchSummary:
    ok = sum(item.outcome is ItemOutcome.OK for item in items)
    failed = sum(item.outcome is ItemOutcome.FAILED for item in items)
    skipped = sum(item.outcome is ItemOutcome.SKIPPED for item in items)
    return BatchSummary(
        items=len(items),
        ok=ok,
        failed=failed,
        skipped=skipped,
        stopped_early=stopped,
        documents=items,
    )


def _record(erp: Erp, principal: Principal, summary: BatchSummary) -> None:
    raw = summary.model_dump(mode="json", exclude_none=True)
    if not isinstance(raw, dict):
        raise ValidationFailed("the batch summary could not be recorded")
    body: dict[str, object] = {}
    for key, value in raw.items():
        body[str(key)] = value
    erp.record_batch_run(
        principal,
        items=summary.items,
        ok=summary.ok,
        failed=summary.failed,
        skipped=summary.skipped,
        summary=body,
    )


def _ok(path: Path, change_key: str | None) -> BatchItem:
    return BatchItem(filename=path.name, outcome=ItemOutcome.OK, change_key=change_key)


def _failed(path: Path, category: ErrorCategory) -> BatchItem:
    return BatchItem(filename=path.name, outcome=ItemOutcome.FAILED, error_category=category)


def _already(path: Path) -> BatchItem:
    return BatchItem(
        filename=path.name,
        outcome=ItemOutcome.SKIPPED,
        skip_reason=SkipReason.ALREADY_ADVANCED,
    )


def _not_reached(path: Path) -> BatchItem:
    return BatchItem(
        filename=path.name,
        outcome=ItemOutcome.SKIPPED,
        skip_reason=SkipReason.NOT_REACHED,
    )


def _handle_batch(args: argparse.Namespace) -> int:
    try:
        principal = Principal(args.subject, Role(args.role), args.company_code)
    except ValueError:
        print("a principal needs a subject and a company code", file=sys.stderr)
        return 2
    try:
        provider = _replay(args.replay)
        with Erp.open(args.database) as erp:
            summary = run_batch(erp, principal, Path(args.directory), provider)
        print(format_summary(summary))
        return 0
    except P2PError as failure:
        print(f"{failure.category.value}: {failure}", file=sys.stderr)
        return 1
    except OSError:
        print("the database could not be opened", file=sys.stderr)
        return 1


def _replay(path: str) -> ModelProvider:
    replay_path = Path(path)
    if not replay_path.is_file():
        raise ValidationFailed("the recording could not be read")
    try:
        return ReplayProvider.from_file(replay_path)
    except ReplayFormatError as exc:
        raise ValidationFailed("the recording could not be read") from exc
