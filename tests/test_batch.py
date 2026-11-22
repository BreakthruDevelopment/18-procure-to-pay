"""`p2p batch`: every document, isolated failures, one budget, one summary."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from agent_container.core.budget import Budget
from agent_container.core.messages import ModelResponse, ToolUseBlock
from agent_container.core.replay import ReplayProvider, dump_replies
from agent_container.core.tool import ErrorCategory
from pydantic import JsonValue

from p2p.batch.run import collect_documents, read_document, run_batch, seeded_folder
from p2p.batch.summary import BatchItem, BatchSummary, ItemOutcome, SkipReason, format_summary
from p2p.cli import main
from p2p.core.access import Principal, Role
from p2p.core.erp import Erp, Invoice, InvoiceState
from p2p.core.errors import AccessDenied, ValidationFailed
from p2p.extract.schema import SCHEMA_TOOL
from tests.match_support import decide_reply, po_replies

_VARIANCE = frozenset({"1002-A", "1003-A", "1004-A"})
_UNREADABLE = "07-1007-A.txt"
_STAGED = frozenset(
    {
        "01-1001-A.txt",
        "05-1005-A.txt",
        "06-1006-A.txt",
        "08-1008-A.txt",
        "10-1010-A.txt",
        "12-1012-A.txt",
    }
)


def _number(filename: str) -> str:
    _prefix, number = Path(filename).stem.split("-", 1)
    return number


def _draft(invoice: Invoice, supplier: str) -> dict[str, JsonValue]:
    lines: list[JsonValue] = []
    for line in invoice.lines:
        lines.append(
            {
                "line_no": line.line_no,
                "description": line.description,
                "qty": line.qty,
                "unit_price": line.unit_price,
                "tax": line.tax,
            }
        )
    confidence: dict[str, JsonValue] = dict.fromkeys(
        ("supplier_name", "po_ref", "number", "date", "currency", "total", "lines"),
        0.99,
    )
    return {
        "supplier_name": supplier,
        "po_ref": invoice.po_ref,
        "number": invoice.number,
        "date": invoice.date,
        "currency": invoice.currency,
        "total": invoice.total,
        "lines": lines,
        "confidence": confidence,
        "unreadable": [],
    }


def _extract(payload: dict[str, JsonValue]) -> ModelResponse:
    return ModelResponse.of(
        stop_reason="tool_use",
        tool_uses=[ToolUseBlock("extract", SCHEMA_TOOL, payload)],
    )


def _replies(
    erp: Erp,
    principal: Principal,
    folder: Path,
    *,
    match: bool,
) -> list[ModelResponse]:
    """Replies in filename order, skipping a document `read_document` refuses."""
    replies: list[ModelResponse] = []
    for path in collect_documents(folder):
        try:
            read_document(path)
        except ValidationFailed:
            continue
        found = erp.find_invoices_by_number(principal, _number(path.name))
        invoice = found[0]
        supplier = erp.get_supplier(principal, invoice.supplier_id).name
        replies.append(_extract(_draft(invoice, supplier)))
        if not match:
            continue
        if invoice.po_ref is None:
            replies.append(decide_reply(verdict="no_po"))
        else:
            verdict = "variance" if invoice.number in _VARIANCE else "match"
            replies.extend(po_replies(invoice.po_ref, invoice.supplier_id, verdict=verdict))
    return replies


def _states(erp: Erp, principal: Principal) -> dict[str, InvoiceState]:
    return {
        invoice_id: erp.get_invoice(principal, invoice_id).state
        for invoice_id in (f"INV-{number}" for number in range(1001, 1013))
    }


def _dump(item: BatchItem) -> dict[str, object]:
    raw = item.model_dump(mode="json", exclude_none=True)
    assert isinstance(raw, dict)
    return {str(key): value for key, value in raw.items()}


def _flags(database: Path) -> list[str]:
    return [
        "--database",
        str(database),
        "--subject",
        "clerk@demo",
        "--role",
        "clerk",
        "--company-code",
        "1000",
    ]


@pytest.fixture
def built_db(db_path: Path) -> Path:
    with Erp.open(db_path):
        pass
    return db_path


def test_the_seeded_folder_counts_every_document_and_keeps_change_keys(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    folder = seeded_folder()
    names = [path.name for path in collect_documents(folder)]
    assert names[6] == _UNREADABLE
    assert len(names) == 12
    provider = ReplayProvider(_replies(erp, clerk, folder, match=True))

    summary = run_batch(erp, clerk, folder, provider)

    assert summary.items == 12
    assert summary.ok == 11
    assert summary.failed == 1
    assert summary.skipped == 0
    assert summary.stopped_early is None
    assert summary.ok + summary.failed + summary.skipped == summary.items
    assert [item.filename for item in summary.documents] == names
    failed = summary.documents[6]
    assert failed.filename == _UNREADABLE
    assert failed.outcome is ItemOutcome.FAILED
    assert failed.error_category is ErrorCategory.VALIDATION
    assert "error_category" in _dump(failed)
    assert "skip_reason" not in _dump(failed)
    assert "change_key" not in _dump(failed)
    staged = {item.filename for item in summary.documents if item.change_key is not None}
    assert staged == _STAGED
    by_name = {item.filename: item for item in summary.documents}
    assert by_name["02-1002-A.txt"].outcome is ItemOutcome.OK
    assert by_name["02-1002-A.txt"].change_key is None
    assert "change_key" not in _dump(by_name["02-1002-A.txt"])
    assert "error_category" not in _dump(by_name["02-1002-A.txt"])
    assert by_name["09-1009-A.txt"].change_key is None
    assert by_name["11-1011-A.txt"].outcome is ItemOutcome.OK
    assert by_name["11-1011-A.txt"].change_key is None
    assert erp.get_invoice(clerk, "INV-1007").state is InvoiceState.RECEIVED
    keys = [item.change_key for item in summary.documents if item.change_key is not None]
    assert keys
    assert all(key.startswith("pst_") for key in keys)
    assert {row.change_key for row in erp.list_staged(clerk)} == set(keys)
    for key in keys:
        assert erp.get_staged(approver, key).change_key == key
    recorded = erp.get_batch_run(clerk, "BR-000001")
    assert (recorded.items, recorded.ok, recorded.failed, recorded.skipped) == (12, 11, 1, 0)
    assert "stopped_early" not in recorded.summary
    stored = BatchSummary.model_validate(recorded.summary)
    assert [item.change_key for item in stored.documents] == [
        item.change_key for item in summary.documents
    ]
    text = json.dumps(recorded.summary)
    for key in keys:
        assert key in text
    tools = {spec.name for request in provider.requests for spec in request.tools}
    assert tools <= {
        "invoice_draft",
        "get_purchase_order",
        "get_goods_receipt",
        "match_outcome",
    }
    systems = "\n".join(request.system for request in provider.requests)
    assert "invoice-batch" not in systems
    assert "invoice-extract" in systems
    assert "invoice-match" in systems
    assert format_summary(summary).startswith("{")
    assert "stopped_early" not in json.loads(format_summary(summary))


def test_a_second_run_of_the_seeded_folder_writes_no_invoice_state(
    erp: Erp, clerk: Principal
) -> None:
    folder = seeded_folder()
    first = run_batch(erp, clerk, folder, ReplayProvider(_replies(erp, clerk, folder, match=True)))
    states = _states(erp, clerk)
    postings = {row.change_key for row in erp.list_staged(clerk)}
    advanced = {
        invoice_id for invoice_id, state in states.items() if state is not InvoiceState.RECEIVED
    }
    assert "INV-1007" not in advanced
    assert len(advanced) == 11

    second = run_batch(
        erp, clerk, folder, ReplayProvider(_replies(erp, clerk, folder, match=False))
    )

    assert first.stopped_early is None
    assert second.stopped_early is None
    assert first.ok + first.failed + first.skipped == first.items == 12
    assert second.ok + second.failed + second.skipped == second.items == 12
    filename_of = {
        f"{index:02d}-{number}-A.txt": f"INV-{number}"
        for index, number in enumerate(range(1001, 1013), start=1)
    }
    for item in second.documents:
        invoice_id = filename_of[item.filename]
        if invoice_id in advanced:
            assert item.outcome is ItemOutcome.SKIPPED
            assert item.skip_reason is SkipReason.ALREADY_ADVANCED
            assert item.error_category is None
            assert item.change_key is None
        else:
            assert item.filename == _UNREADABLE
            assert item.outcome is ItemOutcome.FAILED
            assert item.error_category is ErrorCategory.VALIDATION
    assert first.documents[6].outcome is ItemOutcome.FAILED
    assert second.documents[6].outcome is ItemOutcome.FAILED
    assert _states(erp, clerk) == states
    assert {row.change_key for row in erp.list_staged(clerk)} == postings
    assert len(erp.list_extractions(clerk, "INV-1001")) == 2
    assert erp.list_extractions(clerk, "INV-1007") == ()
    recorded = erp.get_batch_run(clerk, "BR-000002")
    assert recorded.skipped == 11
    assert recorded.failed == 1
    assert recorded.ok == 0
    assert "stopped_early" not in recorded.summary


def test_the_budget_stops_the_run_and_the_rest_are_not_reached(
    erp: Erp, clerk: Principal, tmp_path: Path
) -> None:
    folder = tmp_path / "two"
    folder.mkdir()
    (folder / "01-1001-A.txt").write_text("invoice 1001-A", encoding="utf-8")
    (folder / "02-1005-A.txt").write_text("invoice 1005-A", encoding="utf-8")
    budget = Budget(max_turns=4, max_tokens=100_000, max_seconds=60)
    summary = run_batch(
        erp,
        clerk,
        folder,
        ReplayProvider(_replies(erp, clerk, folder, match=True)),
        budget=budget,
    )

    assert summary.documents[0].outcome is ItemOutcome.OK
    assert summary.documents[0].change_key is not None
    assert summary.documents[1].outcome is ItemOutcome.SKIPPED
    assert summary.documents[1].skip_reason is SkipReason.NOT_REACHED
    assert summary.documents[1].error_category is None
    assert "error_category" not in _dump(summary.documents[1])
    assert summary.stopped_early == "turns budget exceeded: 4 of 4 turns used"
    assert (summary.items, summary.ok, summary.failed, summary.skipped) == (2, 1, 0, 1)
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.STAGED
    assert erp.get_invoice(clerk, "INV-1005").state is InvoiceState.RECEIVED
    recorded = erp.get_batch_run(clerk, "BR-000001")
    assert recorded.summary["stopped_early"] == summary.stopped_early
    assert recorded.skipped == 1


def test_a_budget_cut_mid_extraction_is_transient(
    erp: Erp, clerk: Principal, tmp_path: Path
) -> None:
    folder = tmp_path / "cut"
    folder.mkdir()
    (folder / "01-bad.txt").write_text("not empty", encoding="utf-8")
    (folder / "02-later.txt").write_text("also not empty", encoding="utf-8")
    invoice = erp.get_invoice(clerk, "INV-1001")
    bad = _draft(invoice, erp.get_supplier(clerk, invoice.supplier_id).name)
    bad["total"] = 1
    budget = Budget(max_turns=1, max_tokens=100_000, max_seconds=60)
    summary = run_batch(
        erp,
        clerk,
        folder,
        ReplayProvider([_extract(bad), _extract(bad)]),
        budget=budget,
    )

    assert summary.documents[0].outcome is ItemOutcome.FAILED
    assert summary.documents[0].error_category is ErrorCategory.TRANSIENT
    assert summary.documents[0].skip_reason is None
    assert summary.documents[1].outcome is ItemOutcome.SKIPPED
    assert summary.documents[1].skip_reason is SkipReason.NOT_REACHED
    assert summary.stopped_early == "turns budget exceeded: 1 of 1 turns used"
    assert (summary.ok, summary.failed, summary.skipped) == (0, 1, 1)
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.RECEIVED
    assert erp.list_extractions(clerk, "INV-1001") == ()


def test_a_budget_cut_during_match_is_not_a_success(
    erp: Erp, clerk: Principal, tmp_path: Path
) -> None:
    folder = tmp_path / "match-cut"
    folder.mkdir()
    (folder / "01-1001-A.txt").write_text("invoice 1001-A", encoding="utf-8")
    (folder / "02-1005-A.txt").write_text("invoice 1005-A", encoding="utf-8")
    invoice = erp.get_invoice(clerk, "INV-1001")
    draft = _draft(invoice, erp.get_supplier(clerk, invoice.supplier_id).name)
    budget = Budget(max_turns=1, max_tokens=100_000, max_seconds=60)
    summary = run_batch(
        erp,
        clerk,
        folder,
        ReplayProvider([_extract(draft)]),
        budget=budget,
    )

    assert summary.documents[0].outcome is ItemOutcome.FAILED
    assert summary.documents[0].error_category is ErrorCategory.TRANSIENT
    assert summary.documents[0].change_key is None
    assert summary.documents[1].skip_reason is SkipReason.NOT_REACHED
    assert summary.stopped_early is not None
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.EXTRACTED
    assert erp.get_invoice(clerk, "INV-1005").state is InvoiceState.RECEIVED
    assert erp.list_staged(clerk) == ()


def test_a_failure_is_isolated_and_the_run_continues(
    erp: Erp, clerk: Principal, tmp_path: Path
) -> None:
    folder = tmp_path / "mixed"
    folder.mkdir()
    (folder / "a-1001.txt").write_text("invoice 1001-A", encoding="utf-8")
    (folder / "b-blank.txt").write_text("  \n", encoding="utf-8")
    (folder / "c-unknown.txt").write_text("no seeded row", encoding="utf-8")
    (folder / "d-human.txt").write_text("will not validate", encoding="utf-8")
    (folder / "e-1005.txt").write_text("invoice 1005-A", encoding="utf-8")
    first = erp.get_invoice(clerk, "INV-1001")
    fifth = erp.get_invoice(clerk, "INV-1005")
    human = erp.get_invoice(clerk, "INV-1002")
    bad = _draft(human, erp.get_supplier(clerk, human.supplier_id).name)
    bad["total"] = 1
    unknown = _draft(first, erp.get_supplier(clerk, first.supplier_id).name)
    unknown["number"] = "NO-SUCH"
    replies: list[ModelResponse] = []
    replies.append(_extract(_draft(first, erp.get_supplier(clerk, first.supplier_id).name)))
    replies.extend(po_replies("PO-1001", first.supplier_id, verdict="match"))
    replies.append(_extract(unknown))
    replies.append(_extract(bad))
    replies.append(_extract(bad))
    replies.append(_extract(_draft(fifth, erp.get_supplier(clerk, fifth.supplier_id).name)))
    replies.extend(po_replies("PO-1005", fifth.supplier_id, verdict="match"))
    before = {invoice.id for invoice in erp.list_open_invoices(clerk, limit=100)}

    summary = run_batch(erp, clerk, folder, ReplayProvider(replies))

    assert [item.filename for item in summary.documents] == [
        "a-1001.txt",
        "b-blank.txt",
        "c-unknown.txt",
        "d-human.txt",
        "e-1005.txt",
    ]
    assert [item.outcome for item in summary.documents] == [
        ItemOutcome.OK,
        ItemOutcome.FAILED,
        ItemOutcome.FAILED,
        ItemOutcome.FAILED,
        ItemOutcome.OK,
    ]
    assert summary.stopped_early is None
    assert (summary.items, summary.ok, summary.failed, summary.skipped) == (5, 2, 3, 0)
    for item in summary.documents:
        if item.outcome is ItemOutcome.FAILED:
            assert item.error_category is ErrorCategory.VALIDATION
            assert item.skip_reason is None
            assert item.change_key is None
    assert summary.documents[0].change_key is not None
    assert summary.documents[4].change_key is not None
    assert erp.get_invoice(clerk, "INV-1002").state is InvoiceState.RECEIVED
    assert len(erp.list_extractions(clerk, "INV-1002")) == 1
    assert erp.find_invoices_by_number(clerk, "NO-SUCH") == ()
    assert {invoice.id for invoice in erp.list_open_invoices(clerk, limit=100)} == before


def test_more_than_25_documents_is_refused_and_records_nothing(
    erp: Erp, clerk: Principal, raw: sqlite3.Connection, tmp_path: Path
) -> None:
    folder = tmp_path / "too-many"
    folder.mkdir()
    for index in range(26):
        (folder / f"{index:02d}.txt").write_text("invoice", encoding="utf-8")
    provider = ReplayProvider(())

    with pytest.raises(ValidationFailed, match="26") as raised:
        run_batch(erp, clerk, folder, provider)

    assert "refuses more than 25" in str(raised.value)
    assert provider.requests == []
    assert raw.execute("SELECT COUNT(*) AS n FROM batch_run").fetchone()["n"] == 0
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.RECEIVED


def test_twenty_five_documents_run_and_a_dotfile_is_not_one(
    erp: Erp, clerk: Principal, tmp_path: Path
) -> None:
    folder = tmp_path / "cap"
    folder.mkdir()
    for index in range(25):
        (folder / f"{index:02d}.txt").write_text("invoice", encoding="utf-8")
    (folder / ".DS_Store").write_bytes(b"\x00\x01")
    budget = Budget(max_turns=0, max_tokens=10, max_seconds=30)

    summary = run_batch(erp, clerk, folder, ReplayProvider(()), budget=budget)

    assert summary.items == 25
    assert (summary.ok, summary.failed, summary.skipped) == (0, 0, 25)
    assert all(item.skip_reason is SkipReason.NOT_REACHED for item in summary.documents)
    assert all(not item.filename.startswith(".") for item in summary.documents)
    assert summary.stopped_early == "turns budget exceeded: 0 of 0 turns used"
    recorded = erp.get_batch_run(clerk, "BR-000001")
    assert (recorded.items, recorded.skipped) == (25, 25)


def test_an_empty_folder_records_a_run_that_adds_up(
    erp: Erp, clerk: Principal, tmp_path: Path
) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()

    summary = run_batch(erp, clerk, folder, ReplayProvider(()))

    assert (summary.items, summary.ok, summary.failed, summary.skipped) == (0, 0, 0, 0)
    assert summary.stopped_early is None
    assert summary.documents == []
    recorded = erp.get_batch_run(clerk, "BR-000001")
    assert (recorded.items, recorded.ok, recorded.failed, recorded.skipped) == (0, 0, 0, 0)


def test_a_path_that_is_not_a_directory_is_refused_without_a_row(
    erp: Erp, clerk: Principal, raw: sqlite3.Connection, tmp_path: Path
) -> None:
    secret = tmp_path / "secret-folder-name"
    secret.write_text("not a folder", encoding="utf-8")

    with pytest.raises(ValidationFailed, match="not a directory") as raised:
        run_batch(erp, clerk, secret, ReplayProvider(()))

    assert "secret-folder-name" not in str(raised.value)
    assert raw.execute("SELECT COUNT(*) AS n FROM batch_run").fetchone()["n"] == 0


def test_a_service_principal_cannot_record_a_batch(
    erp: Erp, clerk: Principal, service: Principal, raw: sqlite3.Connection, tmp_path: Path
) -> None:
    folder = tmp_path / "one"
    folder.mkdir()
    (folder / "01-1001-A.txt").write_text("invoice 1001-A", encoding="utf-8")
    invoice = erp.get_invoice(clerk, "INV-1001")
    provider = ReplayProvider(
        [_extract(_draft(invoice, erp.get_supplier(clerk, invoice.supplier_id).name))]
    )

    with pytest.raises(AccessDenied):
        run_batch(erp, service, folder, provider)

    assert provider.requests == []
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.RECEIVED
    assert raw.execute("SELECT COUNT(*) AS n FROM batch_run").fetchone()["n"] == 0


def test_cli_batch_prints_one_summary(
    built_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clerk = Principal("clerk@demo", Role.CLERK, "1000")
    with Erp.open(built_db) as erp:
        replies = _replies(erp, clerk, seeded_folder(), match=True)
    replay = tmp_path / "batch.json"
    replay.write_text(json.dumps(dump_replies(replies)), encoding="utf-8")

    code = main(["batch", str(seeded_folder()), *_flags(built_db), "--replay", str(replay)])

    assert code == 0
    summary = BatchSummary.model_validate(json.loads(capsys.readouterr().out))
    assert (summary.items, summary.ok, summary.failed, summary.skipped) == (12, 11, 1, 0)
    assert summary.stopped_early is None
    assert summary.documents[6].filename == _UNREADABLE
    assert any(item.change_key is not None for item in summary.documents)


def test_cli_refuses_a_folder_over_the_limit(
    built_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "too-many"
    folder.mkdir()
    for index in range(26):
        (folder / f"{index:02d}.txt").write_text("invoice", encoding="utf-8")
    replay = tmp_path / "empty.json"
    replay.write_text('{"replies": []}\n', encoding="utf-8")

    code = main(["batch", str(folder), *_flags(built_db), "--replay", str(replay)])

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("validation:")
    assert "26" in captured.err
    connection = sqlite3.connect(built_db)
    try:
        row = connection.execute("SELECT COUNT(*) FROM batch_run").fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        connection.close()
