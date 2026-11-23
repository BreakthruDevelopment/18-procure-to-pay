"""Builders for the committed demo recordings.

The JSON files on disk are `RecordingProvider` / `dump_replies` output. This module
is what produced them, so a later dump cannot drift from the replies the story
consumes.
"""

from __future__ import annotations

from typing import cast

from agent_container.core.messages import ModelResponse, ToolUseBlock, Usage
from pydantic import JsonValue

from p2p.disputes.schema import SCHEMA_TOOL as DISPUTE_TOOL
from p2p.extract.schema import SCHEMA_TOOL as EXTRACT_TOOL
from p2p.match.schema import SCHEMA_TOOL as MATCH_TOOL

_CONFIDENCE: dict[str, JsonValue] = dict.fromkeys(
    ("supplier_name", "po_ref", "number", "date", "currency", "total", "lines"),
    0.99,
)

_VARIANCE_NUMBERS = frozenset({"1003-A", "1004-A"})


def extract_reply(
    *,
    supplier_name: str,
    po_ref: str | None,
    number: str,
    date: str,
    currency: str,
    total: int,
    lines: tuple[tuple[int, str, int, int], ...],
) -> ModelResponse:
    payload: dict[str, JsonValue] = {
        "supplier_name": supplier_name,
        "po_ref": po_ref,
        "number": number,
        "date": date,
        "currency": currency,
        "total": total,
        "lines": [
            {
                "line_no": line_no,
                "description": description,
                "qty": qty,
                "unit_price": unit_price,
                "tax": 0,
            }
            for line_no, description, qty, unit_price in lines
        ],
        "confidence": dict(_CONFIDENCE),
        "unreadable": [],
    }
    return ModelResponse.of(
        stop_reason="tool_use",
        tool_uses=[ToolUseBlock("extract", EXTRACT_TOOL, payload)],
        usage=Usage(800, 120),
    )


def fetch_reply(tool: str, po_id: str) -> ModelResponse:
    return ModelResponse.of(
        stop_reason="tool_use",
        tool_uses=[ToolUseBlock(tool, tool, {"po_id": po_id})],
        usage=Usage(400, 20),
    )


def decide_reply(
    *,
    verdict: str,
    po_id: str | None,
    supplier_id: str,
    needs_human: bool = False,
) -> ModelResponse:
    payload: dict[str, JsonValue] = {
        "verdict": verdict,
        "variances": [],
        "confidence": 0.99,
        "needs_human": needs_human,
        "why": verdict,
        "supplier_id": supplier_id,
    }
    if po_id is not None:
        payload["po_id"] = po_id
    return ModelResponse.of(
        stop_reason="tool_use",
        tool_uses=[ToolUseBlock("decide", MATCH_TOOL, payload)],
        usage=Usage(900, 40),
    )


def match_replies(po_id: str, supplier_id: str, *, verdict: str) -> tuple[ModelResponse, ...]:
    if verdict == "no_po":
        return (
            decide_reply(
                verdict="no_po",
                po_id=None,
                supplier_id=supplier_id,
                needs_human=True,
            ),
        )
    return (
        fetch_reply("get_purchase_order", po_id),
        fetch_reply("get_goods_receipt", po_id),
        decide_reply(verdict=verdict, po_id=po_id, supplier_id=supplier_id),
    )


def dispute_reply(**fields: object) -> ModelResponse:
    payload: dict[str, JsonValue] = {
        "next_action": "ask_question",
        "tool_choice": None,
        "draft_reply": "Need a fact before acting.",
        "question": "What quantity was received against this invoice?",
    }
    for key, value in fields.items():
        payload[str(key)] = cast(JsonValue, value)
    return ModelResponse.of(
        stop_reason="tool_use",
        tool_uses=[ToolUseBlock("t1", DISPUTE_TOOL, payload)],
        usage=Usage(600, 40),
    )


def quantity_extract() -> ModelResponse:
    return extract_reply(
        supplier_name="Northwind Fasteners",
        po_ref="PO-1002",
        number="1002-A",
        date="2026-11-08",
        currency="USD",
        total=100000,
        lines=((1, "Stainless washers, 25mm", 50, 2000),),
    )


def health_reply() -> ModelResponse:
    return ModelResponse.of("ok", stop_reason="end_turn", usage=Usage(1, 1))


def dispute_replies() -> tuple[ModelResponse, ...]:
    """Four steps on DC-0001 ending in a 4,000 credit that escalates on amount."""
    return (
        dispute_reply(
            next_action="read_invoice",
            tool_choice="get_invoice",
            draft_reply="Looking up the billed invoice.",
            invoice_id="INV-1013",
            question=None,
        ),
        dispute_reply(
            next_action="ask_question",
            tool_choice=None,
            draft_reply="Need a fact before acting.",
            question="What quantity was received against this invoice?",
        ),
        dispute_reply(
            next_action="read_supplier",
            tool_choice="find_supplier",
            draft_reply="Checking the supplier record.",
            supplier_name="Rhine Valve Works",
            question=None,
        ),
        dispute_reply(
            next_action="propose_credit",
            tool_choice="issue_credit_memo",
            draft_reply="Proposing the credit the supplier asked for.",
            amount=400_000,
            currency="USD",
            reason="supplier claim",
            question=None,
        ),
    )


def batch_replies() -> tuple[ModelResponse, ...]:
    """Extract (and match) replies for the seeded folder, in filename order.

    INV-1001 and INV-1002 are already advanced when the demo reaches batch, so those
    two documents still consume one extract reply and then skip match.
    """
    replies: list[ModelResponse] = []
    for row in _BATCH_ROWS:
        replies.append(
            extract_reply(
                supplier_name=row.supplier_name,
                po_ref=row.po_ref,
                number=row.number,
                date=row.date,
                currency="USD",
                total=row.total,
                lines=row.lines,
            )
        )
        if row.already_advanced:
            continue
        if row.po_ref is None:
            replies.extend(match_replies("", row.supplier_id, verdict="no_po"))
            continue
        verdict = "variance" if row.number in _VARIANCE_NUMBERS else "match"
        replies.extend(match_replies(row.po_ref, row.supplier_id, verdict=verdict))
    return tuple(replies)


class _Row:
    __slots__ = (
        "already_advanced",
        "date",
        "lines",
        "number",
        "po_ref",
        "supplier_id",
        "supplier_name",
        "total",
    )

    def __init__(
        self,
        number: str,
        supplier_id: str,
        supplier_name: str,
        po_ref: str | None,
        date: str,
        total: int,
        lines: tuple[tuple[int, str, int, int], ...],
        *,
        already_advanced: bool = False,
    ) -> None:
        self.number = number
        self.supplier_id = supplier_id
        self.supplier_name = supplier_name
        self.po_ref = po_ref
        self.date = date
        self.total = total
        self.lines = lines
        self.already_advanced = already_advanced


_BATCH_ROWS: tuple[_Row, ...] = (
    _Row(
        "1001-A",
        "SUP-1001",
        "Northwind Fasteners",
        "PO-1001",
        "2026-11-08",
        125000,
        ((1, "M8 hex bolts, zinc", 100, 1250),),
        already_advanced=True,
    ),
    _Row(
        "1002-A",
        "SUP-1001",
        "Northwind Fasteners",
        "PO-1002",
        "2026-11-08",
        100000,
        ((1, "Stainless washers, 25mm", 50, 2000),),
        already_advanced=True,
    ),
    _Row(
        "1003-A",
        "SUP-1002",
        "Calder Industrial",
        "PO-1003",
        "2026-11-09",
        100000,
        ((1, "Conveyor rollers", 25, 4000),),
    ),
    _Row(
        "1004-A",
        "SUP-1002",
        "Calder Industrial",
        "PO-1004",
        "2026-11-09",
        550000,
        ((1, "Gearbox assembly, 4:1", 10, 55000),),
    ),
    _Row(
        "1005-A",
        "SUP-1003",
        "Rhine Valve Works",
        "PO-1005",
        "2026-11-10",
        60000,
        ((1, "Brass gate valve, 1in", 200, 300),),
    ),
    _Row(
        "1006-A",
        "SUP-1003",
        "Rhine Valve Works",
        "PO-1006",
        "2026-11-10",
        80000,
        (
            (1, "Actuator, pneumatic", 5, 10000),
            (2, "Valve seat kit", 20, 1500),
        ),
    ),
    _Row(
        "1008-A",
        "SUP-1004",
        "Peakline Electrical",
        "PO-1008",
        "2026-11-11",
        27000,
        ((1, "Cable gland, M20", 60, 450),),
    ),
    _Row(
        "1009-A",
        "SUP-1005",
        "Anselm Castings",
        "PO-1009",
        "2026-11-12",
        200000,
        ((1, "Cast housing, grey iron", 8, 25000),),
    ),
    _Row(
        "1010-A",
        "SUP-1006",
        "Brightfield Packaging",
        "PO-1010",
        "2026-11-12",
        33000,
        ((1, "Corrugated carton, 400x300", 150, 220),),
    ),
    _Row(
        "1011-A",
        "SUP-1006",
        "Brightfield Packaging",
        None,
        "2026-11-13",
        44000,
        ((1, "Stretch wrap, 500mm", 40, 1100),),
    ),
    _Row(
        "1012-A",
        "SUP-1001",
        "Northwind Fasteners",
        "PO-1012",
        "2026-11-13",
        99000,
        ((1, "Threaded rod, M12", 30, 3300),),
    ),
)
