"""Context preservation for a dispute case (statement 5.1).

The model sees a rolling summary plus the last 10 `case_event` rows, capped at 12,000
characters. The summary is replaced after each step, not appended: that is what makes it
rolling. Events stay append-only. Transcript text is data, never instructions, and is
fenced before it reaches a model.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from agent_container.core.fence import check_nonce
from pydantic import JsonValue

from p2p.core.access import Principal
from p2p.core.erp import CaseEvent, DisputeCase, Erp, Invoice, PurchaseOrder, Supplier
from p2p.core.errors import AccessDenied
from p2p.disputes.schema import TRANSCRIPT_CAP

SUMMARY_CAP = 4000
EVENT_WINDOW = 10
#: How much of a read's own result is recorded on the case, so the next step's model sees what
#: was read. Ten of these still leave room for the summary inside `TRANSCRIPT_CAP`.
READ_RESULT_CAP = 600
_TRUNCATED = "\n[transcript truncated]"
_RESULT_TRUNCATED = "…[cut]"
_OLDER_OMITTED = "[older events omitted]"


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
        """How many steps in a row, most recent first, made no progress.

        Counting every such step ever made the criterion read "twice without new facts" as
        "twice in the case's whole history": `[no, yes, no]` escalated a case that had just
        made progress, with "the case has looped twice without new facts" recorded as the
        reason. A loop is consecutive by definition.
        """
        count = 0
        for event in reversed(self.history):
            if event.kind != "step":
                continue
            if event.payload.get("new_facts") is not False:
                break
            count += 1
        return count

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
    """The rolling summary plus the last 10 events, inside the task's 12,000 character cap.

    The budget goes to the NEWEST events first. Cutting a prefix of the joined text spent it
    on the oldest instead: nine long notes followed by a supplier's correction left the
    correction out of the model's request altogether, so the desk could go on proposing a
    credit the supplier had just withdrawn. Whole events are dropped from the old end, and the
    drop is stated rather than silent.
    """
    head: list[str] = []
    summary = facts.case.summary
    if summary:
        head.append("Rolling summary:")
        head.append(summary)
    head.append("Last events:")
    header = "\n".join(head)
    if not facts.window:
        return _within_cap(f"{header}\n(none)")
    # The omission marker's room is reserved up front, so adding it can never push the result
    # back over the cap and cost the newest event the budget was just spent on.
    room = TRANSCRIPT_CAP - len(header) - len(_OLDER_OMITTED) - 1
    kept: list[str] = []
    dropped = False
    for event in reversed(facts.window):
        line = _event_line(event)
        if len(line) + 1 > room:
            dropped = True
            if not kept:
                # Not even the newest event fits beside the summary. Keep what of it does.
                kept.append(_within_cap(line, room))
            break
        kept.append(line)
        room -= len(line) + 1
    if dropped:
        kept.append(_OLDER_OMITTED)
    kept.reverse()
    return _within_cap("\n".join([header, *kept]))


def _within_cap(text: str, cap: int = TRANSCRIPT_CAP) -> str:
    """Cut `text` to `cap`, saying so. Measured from the marker, not written out: a
    `cap - 22` slice plus a 23-character marker returned 12,001, one over the cap."""
    if len(text) <= cap:
        return text
    return text[: cap - len(_TRUNCATED)] + _TRUNCATED


def fence_transcript(text: str, nonce: str | None = None) -> tuple[str, str]:
    """Wrap the transcript so a supplier message cannot close the block. Returns (fenced, nonce)."""
    token = nonce if nonce is not None else secrets.token_hex(8)
    check_nonce(token)
    body = text
    while token in body:
        body = body.replace(token, "")
    fenced = f"<case_transcript fence={token}>\n{body}\n</case_transcript fence={token}>"
    return fenced, token


def opening_note(facts: CaseFacts) -> str | None:
    """The claim the case was opened with, so it survives its event ageing out of the window."""
    for event in facts.history:
        if event.kind == "opened":
            note = event.payload.get("note")
            if isinstance(note, str) and note:
                return f"opened: {note}"
    return None


def roll_summary(previous: str | None, kind: str, detail: str) -> str:
    """Replace the rolling summary with a line for this step, keeping the TAIL of the last.

    Cutting a prefix off the joined text dropped the newest line first: from a head already at
    the cap, three successive calls each returned the same 4,000 characters and none of them
    contained its own new line, so the summary froze and stopped being rolling at all. The new
    line always survives; what falls off is the oldest history.
    """
    head = previous.strip() if previous else ""
    line = f"{kind}: {detail}"
    if len(line) >= SUMMARY_CAP:
        return line[-SUMMARY_CAP:]
    if not head:
        return line
    room = SUMMARY_CAP - len(line) - 3  # the " | " separator
    if room <= 0:
        return line
    return f"{head[-room:]} | {line}"


def read_result_payload(tool: str, body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """A bounded record of what a read returned, so the next step's model can see it.

    Without this a read's result was discarded: the step event recorded only the action, so a
    model that read an invoice was never told the total or the PO it had just asked for, and the
    written loop criterion could never fire because every repeat read counted as new facts.

    The scalar fields come first and the collections after, because the cap cuts the tail and
    the written criteria are about the scalars. Sorting the whole body put `lines` first, so a
    five-line invoice lost its `total` and a six-line one also lost `po_ref`, `state` and
    `supplier_id` — the change failed its own purpose on exactly the invoices a disputes desk
    argues about. `digest` is over the WHOLE body, so deciding whether a read was progress
    never depends on what the cap removed.
    """
    scalars = {key: value for key, value in body.items() if not isinstance(value, list | dict)}
    collections = {key: value for key, value in body.items() if isinstance(value, list | dict)}
    text = _payload_text(scalars)
    if collections and len(text) < READ_RESULT_CAP:
        text = f"{text} {_payload_text(collections)}"
    if len(text) > READ_RESULT_CAP:
        text = text[: READ_RESULT_CAP - len(_RESULT_TRUNCATED)] + _RESULT_TRUNCATED
    return {"tool": tool, "result": text, "digest": _digest(body)}


def _digest(body: Mapping[str, JsonValue]) -> str:
    """A short fingerprint of a whole read result, for deciding whether a step made progress."""
    return hashlib.sha256(_payload_text(body).encode()).hexdigest()[:16]


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
    "READ_RESULT_CAP",
    "SUMMARY_CAP",
    "CaseFacts",
    "fence_transcript",
    "load_facts",
    "opening_note",
    "read_result_payload",
    "render_transcript",
    "roll_summary",
]
