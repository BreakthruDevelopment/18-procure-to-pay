"""Per-step manifests, tool_choice, and the interceptors on every gateway call."""

from __future__ import annotations

import json
import logging

import pytest
from agent_container.core.interceptors import TURN_CAP_NOTICE

from p2p.core.access import Principal
from p2p.core.erp import Erp, InvoiceState
from p2p.core.errors import ValidationFailed
from p2p.mcp.contract import (
    FORCED_TOOL_INVALID,
    NO_SUCH_STEP,
    NOT_IN_MANIFEST,
    PAGE_TOO_LARGE,
)
from p2p.mcp.log import JsonLineFormatter
from p2p.mcp.manifest import manifest_for, tool_choice_for
from p2p.mcp.session import GatewaySession


def test_tool_choice_follows_the_step_and_a_forced_name_is_only_a_shape_check(
    erp: Erp, clerk: Principal
) -> None:
    session = GatewaySession(erp, clerk)
    fetch = session.tool_choice("fetch_po")
    assert fetch.mode == "tool"
    assert fetch.name == "get_purchase_order"
    receipt = session.tool_choice("fetch_receipt")
    assert receipt.mode == "tool"
    assert receipt.name == "get_goods_receipt"

    forced = session.tool_choice("extract", force="invoice_draft")
    assert forced.mode == "tool"
    assert forced.name == "invoice_draft"
    bare = session.tool_choice(force="invoice_draft")
    assert bare.mode == "tool"
    assert bare.name == "invoice_draft"

    with pytest.raises(ValidationFailed) as bad_name:
        session.tool_choice(force="has a space")
    assert str(bad_name.value) == FORCED_TOOL_INVALID
    assert "has a space" not in str(bad_name.value)

    with pytest.raises(ValidationFailed) as bad_step:
        tool_choice_for("nope", ())
    assert str(bad_step.value) == NO_SUCH_STEP
    assert "nope" not in str(bad_step.value)
    with pytest.raises(ValidationFailed):
        manifest_for("nope")


def test_a_forced_step_tool_the_role_does_not_hold_falls_back_to_auto() -> None:
    choice = tool_choice_for("fetch_po", ())
    assert choice.mode == "auto"
    assert choice.name is None


def test_visible_names_are_the_step_cut_to_the_role(
    erp: Erp, clerk: Principal, service: Principal
) -> None:
    clerk_session = GatewaySession(erp, clerk)
    service_session = GatewaySession(erp, service)
    assert clerk_session.visible_names("extract") == ()
    assert service_session.visible_names("dispute") == (
        "find_supplier",
        "get_invoice",
        "get_payment_status",
    )
    assert "post_invoice" in clerk_session.visible_names("decide")
    assert "post_invoice" not in service_session.visible_names("decide")

    clerk_session.set_step("fetch_receipt")
    assert clerk_session.visible_names() == ("get_goods_receipt",)
    clerk_session.set_step(None)
    assert "post_invoice" in clerk_session.visible_names()
    with pytest.raises(ValidationFailed):
        clerk_session.set_step("nope")
    assert clerk_session.current_step() is None


def test_a_tool_outside_the_step_does_not_run_and_the_log_has_no_arguments(
    erp: Erp, clerk: Principal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="p2p.mcp")
    session = GatewaySession(erp, clerk, step="fetch_po")
    marker = "do-not-log-this-payload"
    result = session.call(
        "post_invoice",
        {"invoice_id": "INV-1001", "payload": {"account": marker}},
    )
    assert result.error_category == "permission"
    assert result.is_retryable is False
    assert result.body["message"] == NOT_IN_MANIFEST
    assert "INV-1001" not in result.text
    assert marker not in result.text
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.RECEIVED
    records = [record for record in caplog.records if record.message == "gateway call"]
    assert len(records) == 1
    line = JsonLineFormatter().format(records[0])
    parsed = json.loads(line)
    assert parsed["tool"] == "post_invoice"
    assert parsed["is_error"] is True
    assert parsed["error_category"] == "permission"
    assert parsed["staged"] is False
    assert "INV-1001" not in line
    assert marker not in line


def test_an_audit_line_names_the_tool_and_not_the_row(
    erp: Erp, clerk: Principal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="p2p.mcp")
    GatewaySession(erp, clerk).call("get_invoice", {"invoice_id": "INV-1001"})
    records = [record for record in caplog.records if record.message == "gateway call"]
    assert len(records) == 1
    line = JsonLineFormatter().format(records[0])
    parsed = json.loads(line)
    assert parsed["tool"] == "get_invoice"
    assert parsed["is_error"] is False
    assert parsed["error_category"] is None
    assert parsed["staged"] is False
    for leak in ("INV-1001", "1001-A", "Northwind"):
        assert leak not in line, leak


def test_a_page_the_turn_cap_cuts_is_an_error_and_not_a_partial_document(
    erp: Erp, clerk: Principal
) -> None:
    session = GatewaySession(erp, clerk, max_turn_chars=20)
    listed = session.call("list_open_invoices")
    assert listed.error_category == "validation"
    assert listed.is_retryable is False
    assert listed.body["message"] == PAGE_TOO_LARGE
    assert "items" not in listed.body
    assert TURN_CAP_NOTICE not in listed.text

    shared = GatewaySession(erp, clerk, max_turn_chars=20)
    first = shared.call("get_invoice", {"invoice_id": "INV-1001"}, turn=1)
    second = shared.call("get_invoice", {"invoice_id": "INV-1002"}, turn=1)
    for result, invoice_id in ((first, "INV-1001"), (second, "INV-1002")):
        assert result.error_category == "validation"
        assert result.is_retryable is False
        assert result.body["message"] == PAGE_TOO_LARGE
        assert invoice_id not in result.text
        assert TURN_CAP_NOTICE not in result.text


def test_a_write_the_turn_cap_would_cut_still_returns_its_key(erp: Erp, clerk: Principal) -> None:
    erp.advance_invoice(clerk, "INV-1001", InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, "INV-1001", InvoiceState.MATCHED)
    result = GatewaySession(erp, clerk, max_turn_chars=20).call(
        "post_invoice",
        {"invoice_id": "INV-1001", "payload": {"account": "40100"}},
    )
    assert result.is_error is False
    assert result.body["change_key"] == "pst_000001"
    assert json.loads(result.text) == result.body
    assert TURN_CAP_NOTICE not in result.text
    assert "40100" not in result.text
    assert erp.get_invoice(clerk, "INV-1001").state is InvoiceState.STAGED
