"""The four roles, and the one function that decides whether a principal may do a thing.

A principal is not a web session: there is no Better Auth and no cookie in this repo. Each command
invocation and each MCP session acts as exactly one `Principal`, which carries the subject it acts
for, its role and the single company code it is scoped to.

`assert_can(principal, action, row)` is the only authorization in the app. It is called by every
accessor in `p2p/core/erp.py`, which is the only module that opens the database, so there is nowhere
for a caller to reach a row without passing through it. Four rules, applied in this order:

1. **The role must permit the action.** The table below is the spec's role table, written out. A
   `service` principal holds read actions only, which is checked by a test over this table rather
   than by a special case here.
2. **The row must be in the principal's company code.** The `company_code` filter also lives inside
   `erp.py`'s queries, so this is the second of two independent checks, not the only one: a query
   that forgot its WHERE clause is still refused here.
3. **A `disputes` principal sees only cases assigned to it.** Company code alone would let one
   disputes agent read another's case.
4. **Separation of duties: no principal may decide a change it staged.** Checked on `staged_by`, for
   all three staged-change tables, since all three go through the one `DECIDE_CHANGE` action.

Refusals name the role, the action and, where it helps, the column that failed -- all names from
this module or the schema. They never carry a value from the row or from the caller's input; see the
header of `p2p/core/errors.py` for why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from p2p.core.errors import AccessDenied

# A row as the accessors hand it over: column name to value. Deliberately not a typed model --
# `assert_can` asks only for the three owner columns, and every accessor's own return type is typed.
Row = Mapping[str, object]


class Role(StrEnum):
    """The four principals of the spec's role table."""

    CLERK = "clerk"
    APPROVER = "approver"
    DISPUTES = "disputes"
    SERVICE = "service"


class Action(StrEnum):
    """Everything a principal can ask the ERP to do. One action per accessor, or per group of
    accessors that the role table treats alike."""

    # Reads. The six that the MCP gateway exposes to every role come first.
    READ_SUPPLIER = "read_supplier"
    READ_PURCHASE_ORDER = "read_purchase_order"
    READ_GOODS_RECEIPT = "read_goods_receipt"
    READ_INVOICE = "read_invoice"
    READ_OPEN_INVOICES = "read_open_invoices"
    READ_PAYMENT_STATUS = "read_payment_status"
    # Reads that are not gateway tools, so the read-only `service` principal does not get them.
    READ_EXTRACTION = "read_extraction"
    READ_STAGED = "read_staged"
    READ_CASE = "read_case"
    READ_BATCH_RUN = "read_batch_run"

    # Writes.
    RECORD_EXTRACTION = "record_extraction"
    ADVANCE_INVOICE = "advance_invoice"
    STAGE_POSTING = "stage_posting"
    STAGE_CREDIT_MEMO = "stage_credit_memo"
    STAGE_PAYMENT_HOLD = "stage_payment_hold"
    OPEN_CASE = "open_case"
    ADVANCE_CASE = "advance_case"
    APPEND_CASE_EVENT = "append_case_event"
    RECORD_BATCH_RUN = "record_batch_run"
    DECIDE_CHANGE = "decide_change"


#: The reads. `READ_ACTIONS` is derived from the member names, not written out a second time, so a
#: new `READ_*` action cannot be left out of it and silently counted as a write.
READ_ACTIONS: frozenset[Action] = frozenset(a for a in Action if a.name.startswith("READ_"))
#: Everything else. A test asserts the two partition `Action`.
WRITE_ACTIONS: frozenset[Action] = frozenset(Action) - READ_ACTIONS

#: The six reads the MCP gateway publishes to every role, including `service`.
GATEWAY_READ_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.READ_SUPPLIER,
        Action.READ_PURCHASE_ORDER,
        Action.READ_GOODS_RECEIPT,
        Action.READ_INVOICE,
        Action.READ_OPEN_INVOICES,
        Action.READ_PAYMENT_STATUS,
    }
)

# The spec's role table, written as the permitted action set per role.
#
#   clerk     ingest invoices, run the match, stage a posting, run a batch; NEVER apply a change
#   approver  read everything in their company code, apply or reject a staged change; never apply
#             one they staged (rule 4, not a matter of which actions they hold)
#   disputes  read supplier, invoice and payment state, run a dispute case, stage a credit memo,
#             escalate
#   service   the read tools of the MCP gateway only, scoped to one company code
_ALLOWED: Mapping[Role, frozenset[Action]] = {
    Role.CLERK: GATEWAY_READ_ACTIONS
    | {
        Action.READ_EXTRACTION,
        Action.READ_STAGED,
        Action.READ_BATCH_RUN,
        Action.RECORD_EXTRACTION,
        Action.ADVANCE_INVOICE,
        Action.STAGE_POSTING,
        Action.RECORD_BATCH_RUN,
    },
    Role.APPROVER: GATEWAY_READ_ACTIONS
    | {
        Action.READ_EXTRACTION,
        Action.READ_STAGED,
        Action.READ_CASE,
        Action.READ_BATCH_RUN,
        Action.DECIDE_CHANGE,
    },
    Role.DISPUTES: GATEWAY_READ_ACTIONS
    | {
        Action.READ_STAGED,
        Action.READ_CASE,
        Action.STAGE_CREDIT_MEMO,
        Action.STAGE_PAYMENT_HOLD,
        Action.OPEN_CASE,
        Action.ADVANCE_CASE,
        Action.APPEND_CASE_EVENT,
    },
    Role.SERVICE: GATEWAY_READ_ACTIONS,
}


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting, in what role, in which company code.

    `subject` is an opaque name for the actor -- an operator id, or app 19's service name. It is
    what `staged_by`, `decided_by`, `assigned_to` and `started_by` record, and what separation of
    duties compares, so it must be stable for one actor and different between actors.
    """

    subject: str
    role: Role
    company_code: str

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("a principal needs a subject")
        if not self.company_code:
            raise ValueError("a principal needs a company code")

    @property
    def may_write(self) -> bool:
        """Whether this role holds any write action at all. `service` does not."""
        return bool(_ALLOWED[self.role] & WRITE_ACTIONS)


def allowed_actions(role: Role) -> frozenset[Action]:
    """The actions this role holds, for tests and for the gateway's per-role tool list."""
    return _ALLOWED[role]


def assert_can(principal: Principal, action: Action, row: Row | None = None) -> None:
    """Refuse with `AccessDenied` unless `principal` may do `action`, on `row` if one is given.

    `row` is omitted only where there is no row yet to judge -- opening a case, listing within the
    principal's own company code. Every accessor that reads or changes an existing row passes it.
    """
    if action not in _ALLOWED[principal.role]:
        raise AccessDenied(f"a principal with role {principal.role} may not {action}")

    if row is None:
        return

    owner = row.get("company_code")
    if owner is not None and owner != principal.company_code:
        # No value in the message, in either direction: that this row exists at all is already more
        # than the caller is entitled to know.
        raise AccessDenied(f"this row is not in the principal's company code; {action} refused")

    if principal.role is Role.DISPUTES:
        assigned = row.get("assigned_to")
        if assigned is not None and assigned != principal.subject:
            raise AccessDenied(
                f"a disputes principal may act only on cases assigned to it; {action} refused"
            )

    if action is Action.DECIDE_CHANGE:
        staged_by = row.get("staged_by")
        if staged_by is not None and staged_by == principal.subject:
            raise AccessDenied(
                "separation of duties: the principal that staged a change may not decide it"
            )
