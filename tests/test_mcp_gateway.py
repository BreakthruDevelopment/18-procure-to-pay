"""Gateway behavior against the seeded ERP: paging, company scope, and staged writes.

Writes record a row and return its key. They do not post, credit, or hold anything, and a
refusal carries no row data.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from agent_container.core.gate import PermissionGate, Policy
from agent_container.core.loop import AgentLoop
from agent_container.core.messages import ModelRequest, ModelResponse
from agent_container.core.provider import ModelProvider
from agent_container.core.tool import (
    ErrorCategory,
    Identity,
    ToolContext,
    has_write_gate,
    innermost,
    walk_stack,
    writes,
)
from pydantic import JsonValue

from p2p.core.access import Principal
from p2p.core.adapter import ErpTool, StagedErpChange
from p2p.core.erp import Erp, InvoiceState
from p2p.core.errors import AccessDenied, ValidationFailed
from p2p.mcp.contract import (
    NO_SUCH_TOOL,
    NOT_PERMITTED,
    PAYLOAD_TOO_LARGE,
)
from p2p.mcp.gate import StagedWriteGate
from p2p.mcp.operations import POST_INVOICE, PostInvoiceInput
from p2p.mcp.session import GatewayResult, GatewaySession


class _Silent(ModelProvider):
    async def converse(self, request: ModelRequest) -> ModelResponse:
        del request
        raise AssertionError("the model must not be called")


def _match(erp: Erp, clerk: Principal, invoice_id: str = "INV-1001") -> None:
    erp.advance_invoice(clerk, invoice_id, InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, invoice_id, InvoiceState.MATCHED)


def _items(result: GatewayResult) -> list[dict[str, JsonValue]]:
    found = result.body["items"]
    assert isinstance(found, list)
    rows: list[dict[str, JsonValue]] = []
    for item in found:
        assert isinstance(item, dict)
        rows.append(item)
    return rows


def _count(row: object) -> int:
    assert isinstance(row, sqlite3.Row | tuple)
    value = row[0]
    assert isinstance(value, int)
    return value


def _posting_leaf(session: GatewaySession) -> ErpTool[PostInvoiceInput]:
    found = innermost(session.tool("post_invoice"))
    assert isinstance(found, ErpTool)
    return cast(ErpTool[PostInvoiceInput], found)


def _context(identity: Identity) -> ToolContext:
    return ToolContext(run_id="run-1", turn=1, tool_use_id="tu-1", identity=identity)


def test_list_open_invoices_is_capped_paged_and_excludes_posted(erp: Erp, clerk: Principal) -> None:
    session = GatewaySession(erp, clerk)
    first = session.call("list_open_invoices", {"limit": 2})
    assert first.is_error is False
    assert first.body["limit"] == 2
    assert first.body["offset"] == 0
    assert first.body["count"] == 2
    assert _items(first)[0]["id"] == "INV-1001"

    tail = session.call("list_open_invoices", {"limit": 5, "offset": 10})
    assert tail.body["count"] == 2
    assert [row["id"] for row in _items(tail)] == ["INV-1011", "INV-1012"]

    everything = session.call("list_open_invoices", {"limit": 100})
    ids = [row["id"] for row in _items(everything)]
    assert everything.body["count"] == 12
    assert "INV-1013" not in ids

    refused = session.call("list_open_invoices", {"limit": 101})
    assert refused.error_category == "validation"
    assert refused.is_retryable is False


def test_find_supplier_pages_in_name_order(erp: Erp, clerk: Principal) -> None:
    session = GatewaySession(erp, clerk)
    first = session.call("find_supplier", {"name": "a", "limit": 1, "offset": 0})
    second = session.call("find_supplier", {"name": "a", "limit": 1, "offset": 1})
    assert _items(first)[0]["name"] == "Anselm Castings"
    assert _items(second)[0]["name"] == "Brightfield Packaging"


def test_a_purchase_order_total_is_an_integer_minor_unit(erp: Erp, clerk: Principal) -> None:
    result = GatewaySession(erp, clerk).call("get_purchase_order", {"po_id": "PO-1001"})
    assert result.is_error is False
    assert result.body["total"] == 125000
    assert isinstance(result.body["total"], int)
    assert result.body["supplier_id"] == "SUP-1001"
    assert result.body["currency"] == "USD"


def test_goods_receipts_page_and_another_company_is_an_empty_page(
    erp: Erp, clerk: Principal
) -> None:
    session = GatewaySession(erp, clerk)
    both = session.call("get_goods_receipt", {"po_id": "PO-1006"})
    assert both.body["count"] == 2
    one = session.call("get_goods_receipt", {"po_id": "PO-1006", "limit": 1})
    assert one.body["count"] == 1
    assert len(_items(one)) == 1

    elsewhere = session.call("get_goods_receipt", {"po_id": "PO-2001"})
    assert elsewhere.is_error is False
    assert elsewhere.body["count"] == 0
    assert elsewhere.body["items"] == []
    assert "EUR" not in elsewhere.text


def test_a_cross_company_invoice_is_the_same_refusal_as_a_missing_one(
    erp: Erp, clerk: Principal
) -> None:
    session = GatewaySession(erp, clerk)
    other = session.call("get_invoice", {"invoice_id": "INV-2001"})
    missing = session.call("get_invoice", {"invoice_id": "INV-9999"})
    assert other.error_category == "permission"
    assert other.is_retryable is False
    assert other.body["message"] == missing.body["message"]
    for leak in ("INV-2001", "INV-9999", "2000", "EUR", "50000", "Carbide", "2001-A"):
        assert leak not in other.text, leak


def test_posting_before_match_names_the_state_and_writes_nothing(
    erp: Erp, clerk: Principal, raw: sqlite3.Connection
) -> None:
    result = GatewaySession(erp, clerk).call(
        "post_invoice",
        {"invoice_id": "INV-1001", "payload": {"account": "40100"}},
    )
    assert result.error_category == "validation"
    assert result.is_retryable is False
    assert result.staged is None
    assert "received" in str(result.body["message"])
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.RECEIVED
    assert _count(raw.execute("SELECT COUNT(*) FROM posting").fetchone()) == 0


def test_a_staged_posting_returns_its_key_and_posts_nothing(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    _match(erp, clerk)
    session = GatewaySession(erp, clerk)
    result = session.call(
        "post_invoice",
        {"invoice_id": "INV-1001", "payload": {"account": "40100"}},
    )
    assert result.is_error is False
    assert result.body["change_key"] == "pst_000001"
    assert "pst_000001" in str(result.body["description"])
    assert json_has_only(result, ("change_key", "description"))
    assert isinstance(result.staged, StagedErpChange)
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.STAGED
    status = session.call("get_payment_status", {"invoice_id": "INV-1001"})
    assert status.body["state"] == "staged"
    assert status.body["applied_posting"] is None
    with pytest.raises(AccessDenied, match="may not decide_change"):
        asyncio.run(result.staged.apply())
    assert erp.get_invoice(approver, "INV-1001").state is InvoiceState.STAGED


def test_two_identical_credits_stage_two_rows_and_move_no_invoice(
    erp: Erp, disputes: Principal, clerk: Principal
) -> None:
    session = GatewaySession(erp, disputes)
    arguments: dict[str, JsonValue] = {
        "supplier_id": "SUP-1001",
        "amount": 100,
        "currency": "USD",
        "reason": "short shipment",
    }
    first = session.call("issue_credit_memo", arguments)
    second = session.call("issue_credit_memo", arguments)
    assert first.body["change_key"] == "crd_000001"
    assert second.body["change_key"] == "crd_000002"
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.RECEIVED


def test_a_credit_in_the_wrong_currency_is_validation_and_echoes_neither(
    erp: Erp, disputes: Principal, raw: sqlite3.Connection
) -> None:
    result = GatewaySession(erp, disputes).call(
        "issue_credit_memo",
        {
            "supplier_id": "SUP-1001",
            "amount": 100,
            "currency": "EUR",
            "reason": "wrong currency",
            "invoice_id": "INV-1001",
        },
    )
    assert result.error_category == "validation"
    assert result.is_retryable is False
    assert "EUR" not in result.text
    assert "INV-1001" not in result.text
    assert _count(raw.execute("SELECT COUNT(*) FROM credit_memo").fetchone()) == 0


def test_a_hold_on_the_agents_own_case_stages_and_leaves_the_invoice_posted(
    erp: Erp, disputes: Principal, clerk: Principal
) -> None:
    result = GatewaySession(erp, disputes).call(
        "hold_payment",
        {"invoice_id": "INV-1013", "reason": "under dispute", "case_id": "DC-0001"},
    )
    assert result.is_error is False
    assert result.body["change_key"] == "hld_000001"
    assert erp.get_invoice(clerk, "INV-1013").state is InvoiceState.POSTED


def test_a_hold_on_another_agents_case_is_permission_and_echoes_nothing(
    erp: Erp, disputes: Principal, clerk: Principal, raw: sqlite3.Connection
) -> None:
    result = GatewaySession(erp, disputes).call(
        "hold_payment",
        {"invoice_id": "INV-1013", "reason": "not mine", "case_id": "DC-0002"},
    )
    assert result.error_category == "permission"
    assert result.is_retryable is False
    assert "DC-0002" not in result.text
    assert erp.get_invoice(clerk, "INV-1013").state is InvoiceState.POSTED
    assert _count(raw.execute("SELECT COUNT(*) FROM payment_hold").fetchone()) == 0


def test_a_role_that_does_not_hold_a_tool_is_refused_without_an_echo(
    erp: Erp,
    clerk: Principal,
    approver: Principal,
    disputes: Principal,
    service: Principal,
) -> None:
    payload: dict[str, JsonValue] = {"invoice_id": "INV-1001", "payload": {}}
    for principal in (service, approver, disputes):
        result = GatewaySession(erp, principal).call("post_invoice", payload)
        assert result.body["message"] == NOT_PERMITTED
        assert result.error_category == "permission"
        assert result.is_retryable is False
        assert "INV-1001" not in result.text
    credit = GatewaySession(erp, clerk).call(
        "issue_credit_memo",
        {
            "supplier_id": "SUP-1001",
            "amount": 100,
            "currency": "USD",
            "reason": "not a clerk's tool",
        },
    )
    assert credit.body["message"] == NOT_PERMITTED
    assert "SUP-1001" not in credit.text


def test_an_unknown_tool_is_validation_and_its_name_is_not_repeated(
    erp: Erp, clerk: Principal
) -> None:
    result = GatewaySession(erp, clerk).call("made_up")
    assert result.error_category == "validation"
    assert result.body["message"] == NO_SUCH_TOOL
    assert "made_up" not in result.text
    with pytest.raises(ValidationFailed, match="no such tool") as missing:
        GatewaySession(erp, clerk).tool("made_up")
    assert "made_up" not in str(missing.value)


def test_session_tool_refuses_a_role_that_lacks_it(erp: Erp, service: Principal) -> None:
    with pytest.raises(AccessDenied) as denied:
        GatewaySession(erp, service).tool("post_invoice")
    assert str(denied.value) == NOT_PERMITTED


def test_a_string_amount_and_an_extra_field_are_validation_and_echo_no_value(
    erp: Erp, disputes: Principal, clerk: Principal
) -> None:
    amount: dict[str, JsonValue] = {
        "supplier_id": "SUP-1001",
        "amount": "4000",
        "currency": "USD",
        "reason": "coerced",
    }
    refused = GatewaySession(erp, disputes).call("issue_credit_memo", amount)
    assert refused.error_category == "validation"
    assert refused.is_retryable is False
    assert "4000" not in refused.text

    marker = "note-marker-9f3a"
    extra: dict[str, JsonValue] = {"invoice_id": "INV-1001", "note": marker}
    echoed = GatewaySession(erp, clerk).call("get_invoice", extra)
    assert echoed.error_category == "validation"
    assert marker not in echoed.text


def test_an_oversized_posting_payload_is_refused_before_a_row_is_written(
    erp: Erp, clerk: Principal, raw: sqlite3.Connection
) -> None:
    blob = "x" * 9000
    payload: dict[str, JsonValue] = {"body": blob}
    result = GatewaySession(erp, clerk).call(
        "post_invoice",
        {"invoice_id": "INV-1001", "payload": payload},
    )
    assert result.error_category == "validation"
    assert result.is_retryable is False
    assert result.body["message"] == PAYLOAD_TOO_LARGE
    assert blob not in result.text
    assert _count(raw.execute("SELECT COUNT(*) FROM posting").fetchone()) == 0


def test_a_locked_database_is_transient_and_the_write_inserts_nothing(
    db_path: Path, clerk: Principal
) -> None:
    with Erp.open(db_path) as built:
        _match(built, clerk)
    with Erp.open(db_path, busy_timeout_ms=100) as blocked:
        holder = sqlite3.connect(db_path, isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            session = GatewaySession(blocked, clerk)
            read = session.call("get_invoice", {"invoice_id": "INV-1001"})
            write = session.call(
                "post_invoice",
                {"invoice_id": "INV-1001", "payload": {"account": "40100"}},
            )
        finally:
            holder.close()
    assert read.error_category == "transient"
    assert read.is_retryable is True
    assert "INV-1001" not in read.text
    assert write.error_category == "transient"
    assert write.is_retryable is True
    assert "40100" not in write.text
    with Erp.open(db_path) as check:
        assert check.get_invoice(clerk, "INV-1001").state is InvoiceState.MATCHED
    probe = sqlite3.connect(db_path)
    try:
        row = probe.execute("SELECT COUNT(*) FROM posting").fetchone()
        assert row == (0,)
    finally:
        probe.close()


def test_requires_denies_before_the_tool_runs_and_identity_does_not_retarget_it(
    erp: Erp, clerk: Principal, service: Principal, raw: sqlite3.Connection
) -> None:
    _match(erp, clerk)
    leaf = _posting_leaf(GatewaySession(erp, clerk))
    denied = asyncio.run(
        StagedWriteGate(leaf, requires="stage_posting").invoke(
            {"invoice_id": "INV-1001", "payload": {}},
            _context(Identity.anonymous()),
        )
    )
    assert denied.error_category is ErrorCategory.PERMISSION
    assert denied.content == NOT_PERMITTED
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.MATCHED
    assert _count(raw.execute("SELECT COUNT(*) FROM posting").fetchone()) == 0

    staged = asyncio.run(
        StagedWriteGate(leaf).invoke(
            {"invoice_id": "INV-1001", "payload": {}},
            _context(Identity.anonymous()),
        )
    )
    assert staged.is_error is False
    assert staged.staged is not None
    assert "pst_000001" in staged.content

    foreign = asyncio.run(
        StagedWriteGate(POST_INVOICE.bind(erp, service)).invoke(
            {"invoice_id": "INV-1002", "payload": {}},
            _context(Identity("clerk@demo", frozenset({"stage_posting"}))),
        )
    )
    assert foreign.error_category is ErrorCategory.PERMISSION
    assert "INV-1002" not in foreign.content
    assert _count(raw.execute("SELECT COUNT(*) FROM posting").fetchone()) == 1


def test_a_gated_write_satisfies_the_loop_and_a_bare_write_does_not(
    erp: Erp, clerk: Principal
) -> None:
    session = GatewaySession(erp, clerk)
    write = session.tool("post_invoice")
    read = session.tool("get_invoice")
    AgentLoop(_Silent(), (write,), system_prompt="stage a posting")
    with pytest.raises(ValueError, match="permission gate"):
        AgentLoop(_Silent(), (POST_INVOICE.bind(erp, clerk),), system_prompt="stage a posting")
    assert writes(write) is True
    assert has_write_gate(write) is True
    assert any(isinstance(member, StagedWriteGate) for member in walk_stack(write))
    assert writes(read) is False
    read_gate = next(member for member in walk_stack(read) if isinstance(member, PermissionGate))
    assert not isinstance(read_gate, StagedWriteGate)
    assert read_gate.policy is Policy.READ


def json_has_only(result: GatewayResult, fields: tuple[str, ...]) -> bool:
    return tuple(sorted(result.body)) == fields
