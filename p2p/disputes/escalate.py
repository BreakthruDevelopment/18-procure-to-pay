"""Escalation as an app-17 interceptor chain over the five written criteria (statement 5.2).

Each criterion is one interceptor. The first that matches wins; the rest do not overwrite it.
A match records the criterion and does not block the tool: a credit above 2,500 is still
staged, then the case escalates. Ambiguity that matches none of them is not this module's
job — the desk asks exactly one clarifying question instead.

A sixth interceptor, not a criterion, refuses `hold_payment` unless the invoice is already
`posted`. Applying the hold is the only path to `invoice.state = held`, and that transition
starts at `posted`.
"""

from __future__ import annotations

from agent_container.core.interceptors import Interceptor, InterceptorChain
from agent_container.core.tool import ErrorCategory, ToolContext, ToolResult
from pydantic import BaseModel

from p2p.core.access import Principal
from p2p.core.adapter import as_error
from p2p.core.erp import Erp, InvoiceState
from p2p.core.errors import P2PError
from p2p.disputes.context import CaseFacts
from p2p.disputes.schema import CREDIT_LIMIT_MINOR, VARIANCE_PERCENT, Criterion

HOLD_NOT_POSTED = "a payment hold is staged against a posted invoice; this one is {state}"


class EscalationSink:
    """The first matched criterion. Later interceptors read it and do not replace it."""

    def __init__(self) -> None:
        self.criterion: Criterion | None = None

    def record(self, criterion: Criterion) -> None:
        if self.criterion is None:
            self.criterion = criterion


class CriterionInterceptor(Interceptor):
    """One written criterion. `check` is the predicate; the hooks call it and record a match."""

    criterion: Criterion

    def __init__(self, facts: CaseFacts, sink: EscalationSink) -> None:
        self._facts = facts
        self._sink = sink

    def check(self, facts: CaseFacts) -> bool:
        raise NotImplementedError

    async def before(self, tool: str, args: BaseModel, ctx: ToolContext) -> ToolResult | None:
        del tool, args, ctx
        if self._sink.criterion is None and self.check(self._facts):
            self._sink.record(self.criterion)
        return None

    async def after(
        self, tool: str, args: BaseModel, ctx: ToolContext, result: ToolResult
    ) -> ToolResult:
        del tool, args, ctx
        if self._sink.criterion is None and self.check(self._facts):
            self._sink.record(self.criterion)
        return result


class NoPoInterceptor(CriterionInterceptor):
    criterion = Criterion.NO_PO

    def check(self, facts: CaseFacts) -> bool:
        return facts.invoice is not None and facts.invoice.po_ref is None


class VarianceInterceptor(CriterionInterceptor):
    criterion = Criterion.VARIANCE

    def check(self, facts: CaseFacts) -> bool:
        invoice = facts.invoice
        order = facts.purchase_order
        if invoice is None or order is None or order.total <= 0:
            return False
        delta = abs(invoice.total - order.total)
        return delta * 100 > order.total * VARIANCE_PERCENT


class CreditAmountInterceptor(CriterionInterceptor):
    criterion = Criterion.CREDIT_AMOUNT

    def check(self, facts: CaseFacts) -> bool:
        amount = facts.proposed_amount
        return amount is not None and amount > CREDIT_LIMIT_MINOR

    async def before(self, tool: str, args: BaseModel, ctx: ToolContext) -> ToolResult | None:
        amount = getattr(args, "amount", None) if tool == "issue_credit_memo" else None
        if isinstance(amount, int) and amount > CREDIT_LIMIT_MINOR and self._sink.criterion is None:
            self._sink.record(self.criterion)
        return await super().before(tool, args, ctx)


class FlaggedSupplierInterceptor(CriterionInterceptor):
    criterion = Criterion.FLAGGED_SUPPLIER

    def check(self, facts: CaseFacts) -> bool:
        return facts.supplier.risk_flag


class LoopInterceptor(CriterionInterceptor):
    criterion = Criterion.LOOPED

    def check(self, facts: CaseFacts) -> bool:
        return facts.passes_without_new_facts >= 2


class HoldPostedGuard(Interceptor):
    """Refuse a hold on an invoice that is not `posted`. Not an escalation criterion."""

    def __init__(self, erp: Erp, principal: Principal) -> None:
        self._erp = erp
        self._principal = principal

    async def before(self, tool: str, args: BaseModel, ctx: ToolContext) -> ToolResult | None:
        del ctx
        if tool != "hold_payment":
            return None
        invoice_id = getattr(args, "invoice_id", None)
        if not isinstance(invoice_id, str):
            return None
        try:
            invoice = self._erp.get_invoice(self._principal, invoice_id)
        except P2PError as failure:
            return as_error(failure)
        if invoice.state is InvoiceState.POSTED:
            return None
        return ToolResult.error(
            ErrorCategory.VALIDATION,
            HOLD_NOT_POSTED.format(state=invoice.state),
        )


def criteria_in_order() -> tuple[type[CriterionInterceptor], ...]:
    return (
        NoPoInterceptor,
        VarianceInterceptor,
        CreditAmountInterceptor,
        FlaggedSupplierInterceptor,
        LoopInterceptor,
    )


def first_match(facts: CaseFacts) -> Criterion | None:
    """Walk the five criteria in written order. Used when the step is not a tool call."""
    for cls in criteria_in_order():
        # The sink is unused; `check` is the predicate.
        if cls(facts, EscalationSink()).check(facts):
            return cls.criterion
    return None


def escalation_chain(
    facts: CaseFacts, erp: Erp, principal: Principal, sink: EscalationSink
) -> InterceptorChain:
    """Hold guard first, then the five criteria. A blocked hold never reaches a criterion."""
    interceptors: list[Interceptor] = [HoldPostedGuard(erp, principal)]
    interceptors.extend(cls(facts, sink) for cls in criteria_in_order())
    return InterceptorChain(tuple(interceptors))
