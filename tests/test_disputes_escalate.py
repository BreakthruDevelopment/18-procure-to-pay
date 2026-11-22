"""The five-criterion interceptor chain and the posted-invoice hold guard."""

from __future__ import annotations

import asyncio

from agent_container.core.interceptors import Interceptor
from agent_container.core.tool import ErrorCategory, Identity, ToolContext, ToolResult
from pydantic import BaseModel

from p2p.core.access import Principal
from p2p.core.erp import Erp, InvoiceState
from p2p.disputes.context import load_facts
from p2p.disputes.escalate import (
    HOLD_NOT_POSTED,
    CreditAmountInterceptor,
    EscalationSink,
    FlaggedSupplierInterceptor,
    HoldPostedGuard,
    LoopInterceptor,
    NoPoInterceptor,
    VarianceInterceptor,
    criteria_in_order,
    escalation_chain,
    first_match,
)
from p2p.disputes.schema import CREDIT_LIMIT_MINOR, Criterion


class _HoldArgs(BaseModel):
    invoice_id: str
    reason: str
    case_id: str | None = None


class _CreditArgs(BaseModel):
    amount: int
    currency: str = "USD"
    reason: str = "claim"
    supplier_id: str = "SUP-1003"


_CTX = ToolContext(run_id="test", turn=1, tool_use_id="t1", identity=Identity.anonymous())


def test_the_five_criteria_are_interceptors_in_written_order() -> None:
    order = criteria_in_order()
    assert [cls.criterion for cls in order] == [
        Criterion.NO_PO,
        Criterion.VARIANCE,
        Criterion.CREDIT_AMOUNT,
        Criterion.FLAGGED_SUPPLIER,
        Criterion.LOOPED,
    ]
    for cls in order:
        assert issubclass(cls, Interceptor)


def test_a_case_with_no_po_matches_that_criterion_first(erp: Erp, disputes: Principal) -> None:
    case = erp.open_case(disputes, supplier_id="SUP-1006", invoice_id="INV-1011")
    facts = load_facts(erp, disputes, case.id)
    assert facts.invoice is not None
    assert facts.invoice.po_ref is None
    assert first_match(facts) is Criterion.NO_PO


def test_a_credit_above_the_limit_matches_the_amount_criterion(
    erp: Erp, disputes: Principal
) -> None:
    facts = load_facts(erp, disputes, "DC-0001").with_amount(CREDIT_LIMIT_MINOR + 1)
    assert first_match(facts) is Criterion.CREDIT_AMOUNT
    exact = load_facts(erp, disputes, "DC-0001").with_amount(CREDIT_LIMIT_MINOR)
    assert first_match(exact) is None


def test_a_flagged_supplier_matches_that_criterion(erp: Erp, disputes: Principal) -> None:
    case = erp.open_case(disputes, supplier_id="SUP-1005")
    facts = load_facts(erp, disputes, case.id)
    assert facts.supplier.risk_flag is True
    assert first_match(facts) is Criterion.FLAGGED_SUPPLIER


def test_a_variance_over_five_percent_of_the_po_total_matches(
    erp: Erp, disputes: Principal
) -> None:
    # INV-1004 bills 550000 against PO-1004 totalling 500000: 10%.
    case = erp.open_case(disputes, supplier_id="SUP-1002", invoice_id="INV-1004")
    facts = load_facts(erp, disputes, case.id)
    assert first_match(facts) is Criterion.VARIANCE


def test_two_passes_without_new_facts_match_the_loop_criterion(
    erp: Erp, disputes: Principal
) -> None:
    for _ in range(2):
        erp.append_case_event(
            disputes, "DC-0001", kind="step", payload={"action": "ask_question", "new_facts": False}
        )
    facts = load_facts(erp, disputes, "DC-0001")
    assert facts.passes_without_new_facts == 2
    assert first_match(facts) is Criterion.LOOPED


def test_the_seeded_dispute_matches_none_of_the_criteria(erp: Erp, disputes: Principal) -> None:
    facts = load_facts(erp, disputes, "DC-0001")
    assert first_match(facts) is None


def test_a_hold_on_a_received_invoice_is_refused_and_stages_nothing(
    erp: Erp, disputes: Principal
) -> None:
    case = erp.open_case(disputes, supplier_id="SUP-1001", invoice_id="INV-1001")
    guard = HoldPostedGuard(erp, disputes)
    result = asyncio.run(
        guard.before(
            "hold_payment",
            _HoldArgs(invoice_id="INV-1001", reason="under dispute", case_id=case.id),
            _CTX,
        )
    )
    assert result is not None
    assert result.is_error is True
    assert result.error_category is ErrorCategory.VALIDATION
    assert result.content == HOLD_NOT_POSTED.format(state=InvoiceState.RECEIVED)
    assert "INV-1001" not in result.content


def test_a_hold_on_the_posted_disputed_invoice_is_allowed(erp: Erp, disputes: Principal) -> None:
    guard = HoldPostedGuard(erp, disputes)
    result = asyncio.run(
        guard.before(
            "hold_payment",
            _HoldArgs(invoice_id="INV-1013", reason="under dispute", case_id="DC-0001"),
            _CTX,
        )
    )
    assert result is None


def test_the_credit_interceptor_records_the_amount_from_the_tool_args(
    erp: Erp, disputes: Principal
) -> None:
    facts = load_facts(erp, disputes, "DC-0001")
    sink = EscalationSink()
    interceptor = CreditAmountInterceptor(facts, sink)
    asyncio.run(interceptor.before("issue_credit_memo", _CreditArgs(amount=400_000), _CTX))
    assert sink.criterion is Criterion.CREDIT_AMOUNT


def test_the_chain_starts_with_the_hold_guard(erp: Erp, disputes: Principal) -> None:
    facts = load_facts(erp, disputes, "DC-0001")
    sink = EscalationSink()
    chain = escalation_chain(facts, erp, disputes, sink)
    first = asyncio.run(
        chain.run(
            "hold_payment",
            _HoldArgs(invoice_id="INV-1001", reason="x"),
            _CTX,
            lambda: _ok(),
        )
    )
    assert first.is_error is True
    assert "posted" in first.content


async def _ok() -> ToolResult:
    return ToolResult.ok("should not run")


def test_later_criteria_do_not_overwrite_the_first_match(erp: Erp, disputes: Principal) -> None:
    case = erp.open_case(disputes, supplier_id="SUP-1006", invoice_id="INV-1011")
    facts = load_facts(erp, disputes, case.id).with_amount(400_000)
    # No PO and a large credit: no PO is first in the written order.
    assert first_match(facts) is Criterion.NO_PO
    sink = EscalationSink()
    NoPoInterceptor(facts, sink).check(facts)
    sink.record(Criterion.NO_PO)
    FlaggedSupplierInterceptor(facts, sink).check(facts)
    LoopInterceptor(facts, sink).check(facts)
    VarianceInterceptor(facts, sink).check(facts)
    assert sink.criterion is Criterion.NO_PO
