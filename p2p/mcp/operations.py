"""The nine gateway operations, each an `ErpOperation` over one ERP accessor.

The bodies do nothing but validate the boundary, call the accessor and shape the result.
The company-code filter and `assert_can` stay inside `p2p/core`. A write records a staged
row and returns it; applying that row is a second principal's act and is not this call.
"""

from __future__ import annotations

import json
from typing import Final, cast

from agent_container.core.tool import Effect
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from p2p.core.access import Action, Principal
from p2p.core.adapter import (
    MAX_RESULT_CHARS,
    AnyErpOperation,
    ErpOperation,
    OperationOutcome,
    StagedErpChange,
)
from p2p.core.erp import (
    DEFAULT_PAGE,
    MAX_PAGE,
    Erp,
    GoodsReceipt,
    Invoice,
    InvoiceLine,
    PaymentStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    StagedChangeRow,
    Supplier,
)
from p2p.core.errors import InternalFailure, ValidationFailed
from p2p.mcp.contract import PAYLOAD_TOO_LARGE

_STRICT: Final = ConfigDict(extra="forbid", strict=True)
_OUT: Final = ConfigDict(extra="forbid")


def _dump(model: BaseModel) -> str:
    """One compact JSON object, the text a tool result is size-checked on."""
    raw = model.model_dump(mode="json")
    if not isinstance(raw, dict):
        raise InternalFailure("a tool result was not an object")
    return json.dumps(raw, sort_keys=True, separators=(",", ":"))


def parse_object(content: str) -> dict[str, JsonValue]:
    """The object a tool put in its result. Refuses anything that is not one object.

    The message names the shape, never the text: a truncated page is still a document,
    and handing it back as the error would be handing back the row.
    """
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InternalFailure("a tool result was not JSON") from exc
    if not isinstance(parsed, dict):
        raise InternalFailure("a tool result was not an object")
    return cast(dict[str, JsonValue], parsed)


class _PageInput(BaseModel):
    """`limit` and `offset` for a capped read. Absent `limit` means the ERP's default."""

    model_config = _STRICT

    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0)


def _effective_limit(limit: int | None) -> int:
    """The page size the result reports. The model already rejected a limit past the cap."""
    if limit is None:
        return DEFAULT_PAGE
    return limit


class SupplierOut(BaseModel):
    model_config = _OUT

    id: str
    company_code: str
    name: str
    terms: str
    risk_flag: bool


class SupplierPage(BaseModel):
    model_config = _OUT

    items: list[SupplierOut]
    limit: int
    offset: int
    count: int


class FindSupplierInput(_PageInput):
    name: str = Field(min_length=1, max_length=200)


def _find_supplier(erp: Erp, principal: Principal, args: FindSupplierInput) -> OperationOutcome:
    found = erp.find_supplier(principal, args.name, limit=args.limit, offset=args.offset)
    page = SupplierPage(
        items=[_supplier(row) for row in found],
        limit=_effective_limit(args.limit),
        offset=args.offset,
        count=len(found),
    )
    return OperationOutcome(_dump(page))


def _supplier(row: Supplier) -> SupplierOut:
    return SupplierOut(
        id=row.id,
        company_code=row.company_code,
        name=row.name,
        terms=row.terms,
        risk_flag=row.risk_flag,
    )


class PurchaseOrderLineOut(BaseModel):
    model_config = _OUT

    line_no: int
    description: str
    qty: int
    unit_price: int


class PurchaseOrderOut(BaseModel):
    model_config = _OUT

    id: str
    company_code: str
    supplier_id: str
    currency: str
    total: int
    lines: list[PurchaseOrderLineOut]


class GetPurchaseOrderInput(BaseModel):
    model_config = _STRICT

    po_id: str = Field(min_length=1, max_length=64)


def _get_purchase_order(
    erp: Erp, principal: Principal, args: GetPurchaseOrderInput
) -> OperationOutcome:
    return OperationOutcome(_dump(_purchase_order(erp.get_purchase_order(principal, args.po_id))))


def _purchase_order(row: PurchaseOrder) -> PurchaseOrderOut:
    return PurchaseOrderOut(
        id=row.id,
        company_code=row.company_code,
        supplier_id=row.supplier_id,
        currency=row.currency,
        total=row.total,
        lines=[_purchase_order_line(line) for line in row.lines],
    )


def _purchase_order_line(line: PurchaseOrderLine) -> PurchaseOrderLineOut:
    return PurchaseOrderLineOut(
        line_no=line.line_no,
        description=line.description,
        qty=line.qty,
        unit_price=line.unit_price,
    )


class GoodsReceiptOut(BaseModel):
    model_config = _OUT

    id: str
    company_code: str
    po_id: str
    line_no: int
    qty_received: int
    received_at: str


class GoodsReceiptPage(BaseModel):
    model_config = _OUT

    items: list[GoodsReceiptOut]
    limit: int
    offset: int
    count: int


class GetGoodsReceiptInput(_PageInput):
    po_id: str = Field(min_length=1, max_length=64)
    line_no: int | None = None


def _get_goods_receipt(
    erp: Erp, principal: Principal, args: GetGoodsReceiptInput
) -> OperationOutcome:
    """Page a purchase order's receipts.

    The accessor returns every receipt for that order. A purchase order's lines are a
    handful, and the company-code predicate is already inside the query, so cutting the
    page here does not widen what the principal may see. It does keep the tool's result
    capped the way the other collection tools are.
    """
    found = erp.get_goods_receipt(principal, args.po_id, args.line_no)
    size = _effective_limit(args.limit)
    page_rows = found[args.offset : args.offset + size]
    page = GoodsReceiptPage(
        items=[_goods_receipt(row) for row in page_rows],
        limit=size,
        offset=args.offset,
        count=len(page_rows),
    )
    return OperationOutcome(_dump(page))


def _goods_receipt(row: GoodsReceipt) -> GoodsReceiptOut:
    return GoodsReceiptOut(
        id=row.id,
        company_code=row.company_code,
        po_id=row.po_id,
        line_no=row.line_no,
        qty_received=row.qty_received,
        received_at=row.received_at,
    )


class InvoiceLineOut(BaseModel):
    model_config = _OUT

    line_no: int
    description: str
    qty: int
    unit_price: int
    tax: int


class InvoiceOut(BaseModel):
    model_config = _OUT

    id: str
    company_code: str
    supplier_id: str
    po_ref: str | None
    number: str
    date: str
    currency: str
    total: int
    state: str
    lines: list[InvoiceLineOut]


class InvoicePage(BaseModel):
    model_config = _OUT

    items: list[InvoiceOut]
    limit: int
    offset: int
    count: int


class GetInvoiceInput(BaseModel):
    model_config = _STRICT

    invoice_id: str = Field(min_length=1, max_length=64)


class ListOpenInvoicesInput(_PageInput):
    """No filter beyond the page. The company code comes from the principal."""


def _get_invoice(erp: Erp, principal: Principal, args: GetInvoiceInput) -> OperationOutcome:
    return OperationOutcome(_dump(_invoice(erp.get_invoice(principal, args.invoice_id))))


def _list_open_invoices(
    erp: Erp, principal: Principal, args: ListOpenInvoicesInput
) -> OperationOutcome:
    found = erp.list_open_invoices(principal, limit=args.limit, offset=args.offset)
    page = InvoicePage(
        items=[_invoice(row) for row in found],
        limit=_effective_limit(args.limit),
        offset=args.offset,
        count=len(found),
    )
    return OperationOutcome(_dump(page))


def _invoice(row: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=row.id,
        company_code=row.company_code,
        supplier_id=row.supplier_id,
        po_ref=row.po_ref,
        number=row.number,
        date=row.date,
        currency=row.currency,
        total=row.total,
        state=row.state.value,
        lines=[_invoice_line(line) for line in row.lines],
    )


def _invoice_line(line: InvoiceLine) -> InvoiceLineOut:
    return InvoiceLineOut(
        line_no=line.line_no,
        description=line.description,
        qty=line.qty,
        unit_price=line.unit_price,
        tax=line.tax,
    )


class PaymentStatusOut(BaseModel):
    model_config = _OUT

    invoice_id: str
    state: str
    currency: str
    total: int
    applied_posting: str | None
    applied_holds: list[str]
    applied_credit_total: int


class GetPaymentStatusInput(BaseModel):
    model_config = _STRICT

    invoice_id: str = Field(min_length=1, max_length=64)


def _get_payment_status(
    erp: Erp, principal: Principal, args: GetPaymentStatusInput
) -> OperationOutcome:
    return OperationOutcome(_dump(_payment(erp.get_payment_status(principal, args.invoice_id))))


def _payment(row: PaymentStatus) -> PaymentStatusOut:
    return PaymentStatusOut(
        invoice_id=row.invoice_id,
        state=row.state.value,
        currency=row.currency,
        total=row.total,
        applied_posting=row.applied_posting,
        applied_holds=list(row.applied_holds),
        applied_credit_total=row.applied_credit_total,
    )


class StagedWriteOut(BaseModel):
    """The success payload of every write tool. Both fields, and no others."""

    model_config = _OUT

    change_key: str
    description: str


class PostInvoiceInput(BaseModel):
    model_config = _STRICT

    invoice_id: str = Field(min_length=1, max_length=64)
    payload: dict[str, JsonValue]


class IssueCreditMemoInput(BaseModel):
    model_config = _STRICT

    supplier_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")
    reason: str = Field(min_length=1, max_length=500)
    invoice_id: str | None = Field(default=None, min_length=1, max_length=64)


class HoldPaymentInput(BaseModel):
    model_config = _STRICT

    invoice_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    case_id: str | None = Field(default=None, min_length=1, max_length=64)


def _staged(erp: Erp, principal: Principal, change: StagedChangeRow) -> OperationOutcome:
    """The wire JSON and the row, from one `StagedErpChange` so the two cannot diverge.

    `description` is the adapter's own text. Building a second sentence here is how a
    clerk would be shown one change and an approver another.
    """
    view = StagedErpChange(erp, principal, change)
    payload = StagedWriteOut(change_key=view.change_key, description=view.description)
    return OperationOutcome(_dump(payload), staged=change)


def _post_invoice(erp: Erp, principal: Principal, args: PostInvoiceInput) -> OperationOutcome:
    try:
        encoded = json.dumps(args.payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("the posting payload is not JSON-serializable") from exc
    if len(encoded) > MAX_RESULT_CHARS:
        raise ValidationFailed(PAYLOAD_TOO_LARGE)
    change = erp.stage_posting(principal, args.invoice_id, args.payload)
    return _staged(erp, principal, change)


def _issue_credit_memo(
    erp: Erp, principal: Principal, args: IssueCreditMemoInput
) -> OperationOutcome:
    change = erp.stage_credit_memo(
        principal,
        supplier_id=args.supplier_id,
        amount=args.amount,
        currency=args.currency,
        reason=args.reason,
        invoice_id=args.invoice_id,
    )
    return _staged(erp, principal, change)


def _hold_payment(erp: Erp, principal: Principal, args: HoldPaymentInput) -> OperationOutcome:
    change = erp.stage_payment_hold(
        principal,
        invoice_id=args.invoice_id,
        reason=args.reason,
        case_id=args.case_id,
    )
    return _staged(erp, principal, change)


FIND_SUPPLIER: Final = ErpOperation(
    name="find_supplier",
    description=(
        "Find suppliers in the caller's company code whose name contains the given text. "
        "Results are capped and paged. count is the size of this page, not a total; a count "
        "equal to limit means another page may exist."
    ),
    effect=Effect.READ,
    input_model=FindSupplierInput,
    action=Action.READ_SUPPLIER,
    run=_find_supplier,
)
GET_PURCHASE_ORDER: Final = ErpOperation(
    name="get_purchase_order",
    description=(
        "Read one purchase order in the caller's company code, including its lines. "
        "Amounts are integer minor currency units."
    ),
    effect=Effect.READ,
    input_model=GetPurchaseOrderInput,
    action=Action.READ_PURCHASE_ORDER,
    run=_get_purchase_order,
)
GET_GOODS_RECEIPT: Final = ErpOperation(
    name="get_goods_receipt",
    description=(
        "Read the goods receipts for one purchase order in the caller's company code. "
        "Pass line_no for a single line. Results are capped and paged."
    ),
    effect=Effect.READ,
    input_model=GetGoodsReceiptInput,
    action=Action.READ_GOODS_RECEIPT,
    run=_get_goods_receipt,
)
GET_INVOICE: Final = ErpOperation(
    name="get_invoice",
    description=(
        "Read one invoice in the caller's company code, including its lines. "
        "Amounts are integer minor currency units."
    ),
    effect=Effect.READ,
    input_model=GetInvoiceInput,
    action=Action.READ_INVOICE,
    run=_get_invoice,
)
LIST_OPEN_INVOICES: Final = ErpOperation(
    name="list_open_invoices",
    description=(
        "List invoices in the caller's company code that are not posted or held. "
        "Results are capped and paged. count is the size of this page, not a total."
    ),
    effect=Effect.READ,
    input_model=ListOpenInvoicesInput,
    action=Action.READ_OPEN_INVOICES,
    run=_list_open_invoices,
)
GET_PAYMENT_STATUS: Final = ErpOperation(
    name="get_payment_status",
    description=(
        "Read an invoice's state and what has been applied to it: the applied posting, "
        "applied holds, and the applied credit total in the invoice's currency. A staged "
        "change that nobody has applied yet is not included."
    ),
    effect=Effect.READ,
    input_model=GetPaymentStatusInput,
    action=Action.READ_PAYMENT_STATUS,
    run=_get_payment_status,
)
POST_INVOICE: Final = ErpOperation(
    name="post_invoice",
    description=(
        "Stage a posting for an invoice that is matched. Records a staged change and moves "
        "the invoice to staged. Does not post it; a different principal applies the change "
        "later. The result carries change_key and the staged change's description."
    ),
    effect=Effect.WRITE,
    input_model=PostInvoiceInput,
    action=Action.STAGE_POSTING,
    run=_post_invoice,
)
ISSUE_CREDIT_MEMO: Final = ErpOperation(
    name="issue_credit_memo",
    description=(
        "Stage a credit memo against a supplier. Records a staged change and does not change "
        "any invoice's state. amount is a positive integer in minor currency units. A different "
        "principal applies the change later. The result carries change_key and the description."
    ),
    effect=Effect.WRITE,
    input_model=IssueCreditMemoInput,
    action=Action.STAGE_CREDIT_MEMO,
    run=_issue_credit_memo,
)
HOLD_PAYMENT: Final = ErpOperation(
    name="hold_payment",
    description=(
        "Stage a payment hold on an invoice. Records a staged change and does not change the "
        "invoice's state; applying the change is what holds a posted invoice. A different "
        "principal applies it later. The result carries change_key and the description."
    ),
    effect=Effect.WRITE,
    input_model=HoldPaymentInput,
    action=Action.STAGE_PAYMENT_HOLD,
    run=_hold_payment,
)

#: The gateway's operations, in the published order.
OPERATIONS: Final[tuple[AnyErpOperation, ...]] = (
    FIND_SUPPLIER,
    GET_PURCHASE_ORDER,
    GET_GOODS_RECEIPT,
    GET_INVOICE,
    LIST_OPEN_INVOICES,
    GET_PAYMENT_STATUS,
    POST_INVOICE,
    ISSUE_CREDIT_MEMO,
    HOLD_PAYMENT,
)

OUTPUT_MODELS: Final[dict[str, type[BaseModel]]] = {
    "find_supplier": SupplierPage,
    "get_purchase_order": PurchaseOrderOut,
    "get_goods_receipt": GoodsReceiptPage,
    "get_invoice": InvoiceOut,
    "list_open_invoices": InvoicePage,
    "get_payment_status": PaymentStatusOut,
    "post_invoice": StagedWriteOut,
    "issue_credit_memo": StagedWriteOut,
    "hold_payment": StagedWriteOut,
}
