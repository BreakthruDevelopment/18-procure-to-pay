"""Shared setup for the match tests. Not collected by pytest."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agent_container.core.messages import ModelResponse, ToolUseBlock
from pydantic import JsonValue

from p2p.core.access import Principal
from p2p.core.erp import Erp, Invoice, InvoiceState
from p2p.extract.schema import FieldConfidence
from p2p.match.schema import SCHEMA_TOOL

FIXTURES = Path(__file__).resolve().parents[1] / "p2p" / "match" / "fixtures"


class _Unset:
    """Marks a decide-reply id the caller did not override."""


_UNSET = _Unset()


def prepare(
    erp: Erp,
    principal: Principal,
    invoice_id: str,
    *,
    confidence: float = 0.99,
    description: str | None = None,
    field_confidence: Mapping[str, float] | None = None,
) -> Invoice:
    """Record a draft that matches the invoice, then move it to `extracted`."""
    invoice = erp.get_invoice(principal, invoice_id)
    supplier = erp.get_supplier(principal, invoice.supplier_id)
    fields = _fields(
        invoice,
        supplier.name,
        confidence=confidence,
        description=description,
        field_confidence=field_confidence,
    )
    stored = fields["confidence"]
    overall = 1.0
    if isinstance(stored, dict):
        numbers = [value for value in stored.values() if isinstance(value, float)]
        if numbers:
            overall = min(numbers)
    erp.record_extraction(
        principal,
        invoice.id,
        attempts=1,
        repaired=False,
        confidence=overall,
        fields=fields,
        model_task="invoice-extract",
    )
    return erp.advance_invoice(principal, invoice.id, InvoiceState.EXTRACTED)


def outcome_input(**fields: object) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "verdict": "match",
        "variances": [],
        "confidence": 0.99,
        "needs_human": False,
        "why": "recorded",
    }
    for key, value in fields.items():
        payload[key] = cast(JsonValue, value)
    return payload


def fetch_reply(tool: str, po_id: str) -> ModelResponse:
    return ModelResponse.of(
        stop_reason="tool_use",
        tool_uses=[ToolUseBlock(tool, tool, {"po_id": po_id})],
    )


def decide_reply(**fields: object) -> ModelResponse:
    return ModelResponse.of(
        stop_reason="tool_use",
        tool_uses=[ToolUseBlock("decide", SCHEMA_TOOL, outcome_input(**fields))],
    )


def po_replies(
    po_id: str,
    supplier_id: str,
    *,
    outcome_po: str | _Unset | None = _UNSET,
    outcome_supplier: str | _Unset | None = _UNSET,
    **fields: object,
) -> tuple[ModelResponse, ModelResponse, ModelResponse]:
    """Fetch replies name `po_id`. The decide reply names it too, unless overridden.

    `outcome_po` and `outcome_supplier` are what the decide tool call says. Pass None
    to omit a real id. The fetch calls always use `po_id`, which is the invoice's.
    """
    answer: dict[str, object] = dict(fields)
    answer["po_id"] = po_id if outcome_po is _UNSET else outcome_po
    answer["supplier_id"] = supplier_id if outcome_supplier is _UNSET else outcome_supplier
    return (
        fetch_reply("get_purchase_order", po_id),
        fetch_reply("get_goods_receipt", po_id),
        decide_reply(**answer),
    )


def _fields(
    invoice: Invoice,
    supplier_name: str,
    *,
    confidence: float,
    description: str | None,
    field_confidence: Mapping[str, float] | None,
) -> dict[str, object]:
    scores: dict[str, float] = dict.fromkeys(FieldConfidence.model_fields, confidence)
    if field_confidence is not None:
        scores.update(field_confidence)
    lines: list[dict[str, object]] = []
    for index, line in enumerate(invoice.lines):
        text = description if index == 0 and description is not None else line.description
        lines.append(
            {
                "line_no": line.line_no,
                "description": text,
                "qty": line.qty,
                "unit_price": line.unit_price,
                "tax": line.tax,
            }
        )
    return {
        "supplier_name": supplier_name,
        "po_ref": invoice.po_ref,
        "number": invoice.number,
        "date": invoice.date,
        "currency": invoice.currency,
        "total": invoice.total,
        "lines": lines,
        "confidence": scores,
        "unreadable": [],
    }
