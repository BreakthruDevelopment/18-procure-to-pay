"""Confidence bands and forcing rules, computed in code (statement 5.5).

The model proposes a `MatchOutcome`. This module does not read that proposal. It
compares the invoice, the purchase order and the goods receipt, takes the minimum
per-field confidence from the extraction, and applies the forcing rules: no PO, a
variance over 5% of the PO total, a flagged supplier, a duplicate invoice number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from p2p.core.erp import GoodsReceipt, Invoice, PurchaseOrder, Supplier
from p2p.core.errors import InternalFailure
from p2p.extract.schema import FieldConfidence, InvoiceDraft
from p2p.match.schema import (
    CLEAN,
    CURRENCY,
    DRAFT,
    DUPLICATE,
    FLAGGED,
    HIGH_AT,
    LOW_AT,
    LOW_CONFIDENCE,
    NO_PO,
    REVIEW_BAND,
    VARIANCE_OVER,
    VARIANCE_PERCENT,
    Band,
    VarianceItem,
    VarianceKind,
    Verdict,
)


@dataclass(frozen=True, slots=True)
class CodeDecision:
    """What the documents support, before a model answer is allowed to block a stage."""

    verdict: Verdict
    confidence: float
    band: Band
    needs_human: bool
    why: str
    variances: tuple[VarianceItem, ...]
    fields_to_check: tuple[str, ...]
    blocking: tuple[str, ...]

    @property
    def stage(self) -> bool:
        """A low band is not staged. A forcing rule is not staged. Anything else may be."""
        return self.band is not Band.LOW and not self.blocking


def band_for(confidence: float, variances: Sequence[VarianceItem]) -> Band:
    """≥0.9 with no variance is high. Below 0.6 is low. Every other case is review."""
    if confidence < LOW_AT:
        return Band.LOW
    if confidence < HIGH_AT or variances:
        return Band.REVIEW
    return Band.HIGH


def draft_matches(draft: InvoiceDraft, invoice: Invoice, supplier: Supplier) -> bool:
    """The extraction and the seeded invoice describe the same document.

    Descriptions are not compared: they are document text, and a wording difference
    is not a money difference. Quantity, price, tax, identity and the PO reference are.
    """
    if draft.supplier_name.casefold() != supplier.name.casefold():
        return False
    if draft.number != invoice.number or draft.date != invoice.date:
        return False
    if draft.currency != invoice.currency or draft.total != invoice.total:
        return False
    if draft.po_ref != invoice.po_ref:
        return False
    if len(draft.lines) != len(invoice.lines):
        return False
    by_no = {line.line_no: line for line in invoice.lines}
    for line in draft.lines:
        other = by_no.get(line.line_no)
        if other is None:
            return False
        if line.qty != other.qty or line.unit_price != other.unit_price or line.tax != other.tax:
            return False
    return True


def decide(
    draft: InvoiceDraft,
    invoice: Invoice,
    order: PurchaseOrder | None,
    receipts: Sequence[GoodsReceipt],
    supplier: Supplier,
    *,
    duplicate: bool,
) -> CodeDecision:
    """The authoritative outcome. `duplicate` is a fact the caller counted in the ERP."""
    if (invoice.po_ref is None) != (order is None):
        raise InternalFailure("the purchase order does not agree with the invoice")
    if order is None:
        variances: tuple[VarianceItem, ...] = ()
        over = False
        currency_mismatch = False
    else:
        variances, amount = _line_facts(invoice, order, receipts)
        currency_mismatch = order.currency.casefold() != invoice.currency.casefold()
        over = not currency_mismatch and _over_five_percent(amount, order.total)

    confidence = draft.confidence.overall()
    band = band_for(confidence, variances)
    blocking = _blocking(
        no_po=order is None,
        over=over,
        flagged=supplier.risk_flag,
        duplicate=duplicate,
        currency_mismatch=currency_mismatch,
        draft_ok=draft_matches(draft, invoice, supplier),
    )
    if order is None:
        verdict = Verdict.NO_PO
    elif variances or currency_mismatch:
        verdict = Verdict.VARIANCE
    else:
        verdict = Verdict.MATCH
    return CodeDecision(
        verdict=verdict,
        confidence=confidence,
        band=band,
        needs_human=band is not Band.HIGH or bool(blocking),
        why=_why(blocking, band),
        variances=variances,
        fields_to_check=_fields_to_check(draft.confidence, variances),
        blocking=blocking,
    )


def _over_five_percent(amount: int, po_total: int) -> bool:
    """Strictly over 5% of the PO total. Integer minor units, so 5% exactly is not over."""
    return amount * 100 > po_total * VARIANCE_PERCENT


def _blocking(
    *,
    no_po: bool,
    over: bool,
    flagged: bool,
    duplicate: bool,
    currency_mismatch: bool,
    draft_ok: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if no_po:
        reasons.append(NO_PO)
    else:
        if currency_mismatch:
            reasons.append(CURRENCY)
        if over:
            reasons.append(VARIANCE_OVER)
    if flagged:
        reasons.append(FLAGGED)
    if duplicate:
        reasons.append(DUPLICATE)
    if not draft_ok:
        reasons.append(DRAFT)
    return tuple(reasons)


def _why(blocking: tuple[str, ...], band: Band) -> str:
    reasons = list(blocking)
    if band is Band.LOW:
        reasons.append(LOW_CONFIDENCE)
    elif band is Band.REVIEW and not blocking:
        reasons.append(REVIEW_BAND)
    if not reasons:
        return CLEAN
    return "; ".join(reasons)


def _fields_to_check(
    confidence: FieldConfidence, variances: Sequence[VarianceItem]
) -> tuple[str, ...]:
    values = {
        "supplier_name": confidence.supplier_name,
        "po_ref": confidence.po_ref,
        "number": confidence.number,
        "date": confidence.date,
        "currency": confidence.currency,
        "total": confidence.total,
        "lines": confidence.lines,
    }
    names = [name for name, value in values.items() if value < HIGH_AT]
    if any(item.kind is VarianceKind.QUANTITY for item in variances):
        names.append("quantity")
    if any(item.kind is VarianceKind.PRICE for item in variances):
        names.append("price")
    return tuple(names)


def _line_facts(
    invoice: Invoice, order: PurchaseOrder, receipts: Sequence[GoodsReceipt]
) -> tuple[tuple[VarianceItem, ...], int]:
    """Variances, and the absolute money those lines differ by, in minor units.

    Quantity is the invoice quantity against the goods receipt (expected is what was
    received). Price is the invoice unit price against the purchase order. The money
    is the invoiced extension against the received quantity at the order's price, which
    is what "over 5% of the PO total" is measured on. A line the order does not have
    is a quantity variance against nothing ordered.
    """
    po_lines = {line.line_no: line for line in order.lines}
    received: dict[int, int] = {}
    for receipt in receipts:
        received[receipt.line_no] = received.get(receipt.line_no, 0) + receipt.qty_received
    items: list[VarianceItem] = []
    amount = 0
    for line in invoice.lines:
        po_line = po_lines.get(line.line_no)
        got = received.get(line.line_no, 0)
        price = po_line.unit_price if po_line is not None else 0
        supported = got * price
        invoiced = line.qty * line.unit_price + line.tax
        amount += abs(invoiced - supported)
        if po_line is None or line.qty != got:
            items.append(
                VarianceItem(
                    kind=VarianceKind.QUANTITY,
                    line_no=line.line_no,
                    expected=0 if po_line is None else got,
                    actual=line.qty,
                )
            )
        if po_line is not None and line.unit_price != po_line.unit_price:
            items.append(
                VarianceItem(
                    kind=VarianceKind.PRICE,
                    line_no=line.line_no,
                    expected=po_line.unit_price,
                    actual=line.unit_price,
                )
            )
        elif (
            po_line is not None
            and line.qty == got
            and line.unit_price == po_line.unit_price
            and line.tax != 0
        ):
            items.append(
                VarianceItem(
                    kind=VarianceKind.PRICE,
                    line_no=line.line_no,
                    expected=supported,
                    actual=invoiced,
                )
            )
    items.sort(key=lambda item: (item.line_no, 0 if item.kind is VarianceKind.QUANTITY else 1))
    return tuple(items), amount
