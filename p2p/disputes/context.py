"""Context preservation for a dispute case (statement 5.1).

The model sees a rolling summary plus the last 10 `case_event` rows, capped at 12,000
characters. The summary is replaced after each step, not appended: that is what makes it
rolling. Events stay append-only. Transcript text is data, never instructions, and is
fenced before it reaches a model.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from agent_container.core.fence import check_nonce

from p2p.core.access import Principal
from p2p.core.erp import CaseEvent, DisputeCase, Erp, Invoice, PurchaseOrder, Supplier
from p2p.core.errors import AccessDenied
from p2p.disputes.schema import TRANSCRIPT_CAP

SUMMARY_CAP = 4000
EVENT_WINDOW = 10


@dataclass(frozen=True, slots=True)
class CaseFacts:
    """What the desk knows about a case at the start of a step, loaded through the ERP."""

    case: DisputeCase
    supplier: Supplier
    invoice: Invoice | None
    purchase_order: PurchaseOrder | None
    window: tuple[CaseEvent, ...]
    history: tuple[CaseEvent, ...]
    proposed_amount: int | None = None

    @property
    def passes_without_new_facts(self) -> int:
        return sum(
            1
            for event in self.history
            if event.kind == "step" and event.payload.get("new_facts") is False
        )

    @property
    def question_asked(self) -> bool:
        return any(event.kind == "question" for event in self.history)

    def with_amount(self, amount: int | None) -> CaseFacts:
        return CaseFacts(
            case=self.case,
            supplier=self.supplier,
            invoice=self.invoice,
            purchase_order=self.purchase_order,
            window=self.window,
            history=self.history,
            proposed_amount=amount,
        )


def load_facts(erp: Erp, principal: Principal, case_id: str) -> CaseFacts:
    case = erp.get_case(principal, case_id)
    supplier = erp.get_supplier(principal, case.supplier_id)
    invoice: Invoice | None = None
    purchase_order: PurchaseOrder | None = None
    if case.invoice_id is not None:
        invoice = erp.get_invoice(principal, case.invoice_id)
        if invoice.po_ref is not None:
            try:
                purchase_order = erp.get_purchase_order(principal, invoice.po_ref)
            except AccessDenied:
                purchase_order = None
    window = erp.list_case_events(principal, case_id, limit=EVENT_WINDOW)
    history = erp.list_case_events(principal, case_id, limit=100)
    return CaseFacts(case, supplier, invoice, purchase_order, window, history)


def render_transcript(facts: CaseFacts) -> str:
    """The rolling summary plus the last 10 events, cut to the task's 12,000 character cap."""
    parts: list[str] = []
    summary = facts.case.summary
    if summary:
        parts.append("Rolling summary:")
        parts.append(summary)
    parts.append("Last events:")
    if not facts.window:
        parts.append("(none)")
    for event in facts.window:
        parts.append(_event_line(event))
    text = "\n".join(parts)
    if len(text) <= TRANSCRIPT_CAP:
        return text
    return text[: TRANSCRIPT_CAP - 22] + "\n[transcript truncated]"


def fence_transcript(text: str, nonce: str | None = None) -> tuple[str, str]:
    """Wrap the transcript so a supplier message cannot close the block. Returns (fenced, nonce)."""
    token = nonce if nonce is not None else secrets.token_hex(8)
    check_nonce(token)
    body = text
    while token in body:
        body = body.replace(token, "")
    fenced = f"<case_transcript fence={token}>\n{body}\n</case_transcript fence={token}>"
    return fenced, token


def roll_summary(previous: str | None, kind: str, detail: str) -> str:
    """Replace the rolling summary with a short line for this step, keeping a prefix of the last."""
    head = previous.strip() if previous else ""
    line = f"{kind}: {detail}"
    text = f"{head} | {line}" if head else line
    return text[:SUMMARY_CAP]


def _event_line(event: CaseEvent) -> str:
    payload = _payload_text(event.payload)
    return f"{event.seq} {event.kind} {payload}"


def _payload_text(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


__all__ = [
    "EVENT_WINDOW",
    "SUMMARY_CAP",
    "CaseFacts",
    "fence_transcript",
    "load_facts",
    "render_transcript",
    "roll_summary",
]
