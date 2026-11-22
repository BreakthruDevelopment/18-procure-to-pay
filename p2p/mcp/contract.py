"""The frozen tool list and error shape app 19 connects to.

App 19 imports this module. A change here is a change to a published contract, which is
why `tests/test_mcp_contract.py` asserts the running server against these constants
rather than against itself. The five `error_category` values are app 17's. This module
does not add a sixth.
"""

from __future__ import annotations

from typing import Final

from agent_container.core.tool import ErrorCategory

from p2p import __version__

SERVER_NAME: Final = "p2p-erp"
SERVER_VERSION: Final = __version__

#: MCP protocol versions this server accepts. A client that offers one gets it back.
#: Anything else is answered with `PREFERRED_PROTOCOL`, and the client may disconnect.
PROTOCOL_VERSIONS: Final[tuple[str, ...]] = ("2024-11-05", "2025-03-26")
PREFERRED_PROTOCOL: Final = "2025-03-26"

#: The nine tools, in the spec's gateway-table order. This order is the `tools/list` order.
TOOL_NAMES: Final[tuple[str, ...]] = (
    "find_supplier",
    "get_purchase_order",
    "get_goods_receipt",
    "get_invoice",
    "list_open_invoices",
    "get_payment_status",
    "post_invoice",
    "issue_credit_memo",
    "hold_payment",
)

READ_TOOLS: Final[tuple[str, ...]] = TOOL_NAMES[:6]
WRITE_TOOLS: Final[tuple[str, ...]] = TOOL_NAMES[6:]

TOOL_EFFECTS: Final[dict[str, str]] = {
    **dict.fromkeys(READ_TOOLS, "read"),
    **dict.fromkeys(WRITE_TOOLS, "write"),
}

#: Tools each role's session publishes, in `TOOL_NAMES` order. The role table in
#: `p2p/core/access.py` is the authority; these tuples are the contract a caller sees.
SERVICE_TOOLS: Final[tuple[str, ...]] = READ_TOOLS
APPROVER_TOOLS: Final[tuple[str, ...]] = READ_TOOLS
CLERK_TOOLS: Final[tuple[str, ...]] = (*READ_TOOLS, "post_invoice")
DISPUTES_TOOLS: Final[tuple[str, ...]] = (*READ_TOOLS, "issue_credit_memo", "hold_payment")

#: Argument names a tool accepts, sorted. `REQUIRED_ARGUMENTS` is the subset with no default.
ARGUMENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "find_supplier": ("limit", "name", "offset"),
    "get_purchase_order": ("po_id",),
    "get_goods_receipt": ("limit", "line_no", "offset", "po_id"),
    "get_invoice": ("invoice_id",),
    "list_open_invoices": ("limit", "offset"),
    "get_payment_status": ("invoice_id",),
    "post_invoice": ("invoice_id", "payload"),
    "issue_credit_memo": ("amount", "currency", "invoice_id", "reason", "supplier_id"),
    "hold_payment": ("case_id", "invoice_id", "reason"),
}

REQUIRED_ARGUMENTS: Final[dict[str, tuple[str, ...]]] = {
    "find_supplier": ("name",),
    "get_purchase_order": ("po_id",),
    "get_goods_receipt": ("po_id",),
    "get_invoice": ("invoice_id",),
    "list_open_invoices": (),
    "get_payment_status": ("invoice_id",),
    "post_invoice": ("invoice_id", "payload"),
    "issue_credit_memo": ("supplier_id", "amount", "currency", "reason"),
    "hold_payment": ("invoice_id", "reason"),
}

#: A tool failure's structured payload, and nothing else. `message` is prose from a fixed
#: string. `error_category` and `is_retryable` are how a caller tells a retry from a dead end.
ERROR_FIELDS: Final[tuple[str, ...]] = ("error_category", "is_retryable", "message")

#: The five categories, in app 17's declaration order. Compared to `ErrorCategory` by a test.
ERROR_CATEGORIES: Final[tuple[str, ...]] = tuple(category.value for category in ErrorCategory)

#: A staged write's success payload. The key is app 18's; `StagedChange` has no id.
WRITE_FIELDS: Final[tuple[str, ...]] = ("change_key", "description")

#: A paged read. `count` is the number of rows in this page, not a total: the ERP does not
#: count past the page, and a count equal to `limit` means another page may exist.
PAGE_FIELDS: Final[tuple[str, ...]] = ("count", "items", "limit", "offset")

SUPPLIER_FIELDS: Final[tuple[str, ...]] = ("company_code", "id", "name", "risk_flag", "terms")
PURCHASE_ORDER_FIELDS: Final[tuple[str, ...]] = (
    "company_code",
    "currency",
    "id",
    "lines",
    "supplier_id",
    "total",
)
PURCHASE_ORDER_LINE_FIELDS: Final[tuple[str, ...]] = ("description", "line_no", "qty", "unit_price")
GOODS_RECEIPT_FIELDS: Final[tuple[str, ...]] = (
    "company_code",
    "id",
    "line_no",
    "po_id",
    "qty_received",
    "received_at",
)
INVOICE_FIELDS: Final[tuple[str, ...]] = (
    "company_code",
    "currency",
    "date",
    "id",
    "lines",
    "number",
    "po_ref",
    "state",
    "supplier_id",
    "total",
)
INVOICE_LINE_FIELDS: Final[tuple[str, ...]] = ("description", "line_no", "qty", "tax", "unit_price")
PAYMENT_FIELDS: Final[tuple[str, ...]] = (
    "applied_credit_total",
    "applied_holds",
    "applied_posting",
    "currency",
    "invoice_id",
    "state",
    "total",
)

#: Per-step tool manifests (statement 2.3). Order inside a step is the order `tools/list`
#: uses while that step is active. A name here is always one of `TOOL_NAMES`.
STEP_NAMES: Final[tuple[str, ...]] = (
    "extract",
    "fetch_po",
    "fetch_receipt",
    "decide",
    "dispute",
)
STEP_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    # 18.4 forces its own schema tool. No ERP tool belongs to extraction.
    "extract": (),
    "fetch_po": ("get_purchase_order",),
    "fetch_receipt": ("get_goods_receipt",),
    "decide": (
        "get_invoice",
        "get_purchase_order",
        "get_goods_receipt",
        "get_payment_status",
        "post_invoice",
    ),
    "dispute": (
        "find_supplier",
        "get_invoice",
        "get_payment_status",
        "issue_credit_memo",
        "hold_payment",
    ),
}

#: `(mode, name)` for the step's own `ToolChoice`. `name` is set only when mode is `tool`.
#: `extract` stays `auto` until 18.4 passes `force` for the schema tool it owns.
STEP_TOOL_CHOICE: Final[dict[str, tuple[str, str | None]]] = {
    "extract": ("auto", None),
    "fetch_po": ("tool", "get_purchase_order"),
    "fetch_receipt": ("tool", "get_goods_receipt"),
    "decide": ("auto", None),
    "dispute": ("auto", None),
}

#: Fixed sentences. A message built for a caller is one of these, or a message an ERP
#: failure already carries (those name schema facts, never a caller's value).
NO_SUCH_TOOL: Final = "no such tool"
NOT_PERMITTED: Final = "this caller is not permitted to use this tool"
NOT_IN_MANIFEST: Final = "this tool is not in the active step's manifest"
PAGE_TOO_LARGE: Final = "this result is larger than a tool result may be; ask for a smaller page"
INTERNAL_MESSAGE: Final = "this tool failed; nothing was done"
WRITE_KEY_MISSING: Final = "a staged write's key could not be returned"
NO_SUCH_STEP: Final = "no such step"
FORCED_TOOL_INVALID: Final = "a forced tool name is not a valid tool name"
PAYLOAD_TOO_LARGE: Final = "the posting payload is larger than a tool argument may be"
