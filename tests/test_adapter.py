"""The Adapter: one ERP call as an app-17 `Tool`, with the right effect and structured failures.

The nine gateway operations themselves belong to slice 18.3; what is under test here is the
mechanism they will be declared with, exercised through operations defined in this file.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from agent_container.core.gate import PermissionGate, Policy
from agent_container.core.tool import (
    AnyTool,
    Effect,
    ErrorCategory,
    Identity,
    ToolContext,
    ToolResult,
    has_write_gate,
    innermost,
    writes,
)
from pydantic import BaseModel, JsonValue

from p2p.core.access import Action, Principal, Role
from p2p.core.adapter import (
    MAX_RESULT_CHARS,
    AnyErpOperation,
    ErpOperation,
    OperationOutcome,
    StagedErpChange,
    ToolSet,
    build_tool_set,
)
from p2p.core.erp import Decision, Erp, InvoiceState, StagedChangeRow
from p2p.core.errors import AccessDenied, ValidationFailed

BATCH_INVOICE = "INV-1001"
OTHER_INVOICE = "INV-2001"


class InvoiceArgs(BaseModel):
    invoice_id: str


class PostArgs(BaseModel):
    invoice_id: str
    account: str


class BigArgs(BaseModel):
    size: int


def _read_invoice(erp: Erp, principal: Principal, args: InvoiceArgs) -> OperationOutcome:
    invoice = erp.get_invoice(principal, args.invoice_id)
    return OperationOutcome(content=f"{invoice.id} {invoice.state} {invoice.total}")


def _post_invoice(erp: Erp, principal: Principal, args: PostArgs) -> OperationOutcome:
    change = erp.stage_posting(principal, args.invoice_id, {"account": args.account})
    # The key travels in the success result, beside the staged change: `StagedChange` has no id,
    # so without this a clerk could never name the change again.
    return OperationOutcome(content=f"staged {change.change_key}", staged=change)


def _oversized(erp: Erp, principal: Principal, args: BigArgs) -> OperationOutcome:
    del erp, principal
    return OperationOutcome(content="x" * args.size)


GET_INVOICE = ErpOperation(
    name="get_invoice",
    description="Read one invoice in the caller's company code.",
    effect=Effect.READ,
    input_model=InvoiceArgs,
    action=Action.READ_INVOICE,
    run=_read_invoice,
)
POST_INVOICE = ErpOperation(
    name="post_invoice",
    description="Stage a posting for an invoice. Acts on nothing until an approver applies it.",
    effect=Effect.WRITE,
    input_model=PostArgs,
    action=Action.STAGE_POSTING,
    run=_post_invoice,
)
OVERSIZED = ErpOperation(
    name="oversized_read",
    description="A read whose result is too large to return.",
    effect=Effect.READ,
    input_model=BigArgs,
    action=Action.READ_INVOICE,
    run=_oversized,
)


@pytest.fixture
def ctx() -> Iterator[ToolContext]:
    yield ToolContext(run_id="run-1", turn=1, tool_use_id="tu-1", identity=Identity("clerk@demo"))


def _call(tool: AnyTool, raw: Mapping[str, JsonValue], ctx: ToolContext) -> ToolResult:
    return asyncio.run(tool.invoke(raw, ctx))


def _row_of(erp: Erp, change_key: str) -> StagedChangeRow:
    """Re-read a staged row as an approver would, to build a `StagedErpChange` over it."""
    return erp.get_staged(Principal("approver@demo", Role.APPROVER, "1000"), change_key)


def test_an_operations_effect_is_passed_through_unchanged(erp: Erp, clerk: Principal) -> None:
    read = GET_INVOICE.bind(erp, clerk)
    write = POST_INVOICE.bind(erp, clerk)
    assert read.effect is Effect.READ
    assert write.effect is Effect.WRITE
    # App 17 reads the whole stack, so declaring the truth here is what makes its gate check work.
    assert writes(write) is True
    assert writes(read) is False
    assert innermost(write) is write
    assert write.wrapped is None


def test_a_write_tool_satisfies_app_17s_gate_check_once_wrapped(erp: Erp, clerk: Principal) -> None:
    write = POST_INVOICE.bind(erp, clerk)
    assert has_write_gate(write) is False
    gated = PermissionGate(write, Policy.STAGED_WRITE)
    assert has_write_gate(gated) is True
    assert writes(gated) is True
    # And a read cannot be dressed as a staged write, nor a write gated as a read.
    with pytest.raises(ValueError, match="needs a writing tool"):
        PermissionGate(GET_INVOICE.bind(erp, clerk), Policy.STAGED_WRITE)
    with pytest.raises(ValueError, match="it writes"):
        PermissionGate(write, Policy.READ)


def test_an_operation_cannot_declare_an_effect_its_action_contradicts() -> None:
    with pytest.raises(ValueError, match="effect 'read' for a write action"):
        ErpOperation(
            name="sneaky",
            description="A write wearing a read's effect.",
            effect=Effect.READ,
            input_model=PostArgs,
            action=Action.STAGE_POSTING,
            run=_post_invoice,
        )
    with pytest.raises(ValueError, match="effect 'write' for a read action"):
        ErpOperation(
            name="overcautious",
            description="A read wearing a write's effect.",
            effect=Effect.WRITE,
            input_model=InvoiceArgs,
            action=Action.READ_INVOICE,
            run=_read_invoice,
        )


def test_a_read_returns_its_content_as_a_successful_result(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    result = _call(GET_INVOICE.bind(erp, clerk), {"invoice_id": BATCH_INVOICE}, ctx)
    assert result.is_error is False
    assert result.staged is None
    assert BATCH_INVOICE in result.content


def test_invalid_arguments_come_back_as_a_validation_result_naming_the_field(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    result = _call(GET_INVOICE.bind(erp, clerk), {"wrong": 1}, ctx)
    assert result.is_error is True
    assert result.error_category is ErrorCategory.VALIDATION
    assert result.is_retryable is False
    assert "invoice_id" in result.content


def test_a_failure_becomes_a_result_carrying_the_category_the_error_declares(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    result = _call(GET_INVOICE.bind(erp, clerk), {"invoice_id": OTHER_INVOICE}, ctx)
    assert result.is_error is True
    assert result.error_category is ErrorCategory.PERMISSION
    assert result.is_retryable is False
    # The spec's eval: a permission failure carries no row data.
    for leak in (OTHER_INVOICE, "2000", "EUR"):
        assert leak not in result.content, leak


def test_a_tool_the_role_may_not_use_is_refused_before_the_call_is_made(
    erp: Erp, service: Principal, ctx: ToolContext
) -> None:
    result = _call(
        POST_INVOICE.bind(erp, service),
        {"invoice_id": BATCH_INVOICE, "account": "1"},
        ctx,
    )
    assert result.error_category is ErrorCategory.PERMISSION
    # A `service` principal is told it may not use the tool, not that a row is invisible.
    assert "may not stage_posting" in result.content


def test_a_staged_write_returns_a_staged_change_and_its_key_and_acts_on_nothing(
    erp: Erp, clerk: Principal, approver: Principal, ctx: ToolContext
) -> None:
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
    result = _call(
        POST_INVOICE.bind(erp, clerk), {"invoice_id": BATCH_INVOICE, "account": "40100"}, ctx
    )
    assert result.is_error is False
    staged = result.staged
    assert isinstance(staged, StagedErpChange)
    assert staged.change_key in result.content
    assert staged.change_key.startswith("pst_")
    assert BATCH_INVOICE in staged.description
    # Nothing was posted: the invoice is waiting for an approver.
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.STAGED
    assert erp.get_staged(approver, staged.change_key).decision is None


def test_the_principal_that_staged_a_change_cannot_apply_it_through_the_staged_change(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    """A clerk holding a `StagedChange` must not be one await away from posting it.

    Two refusals stand between it and a write, and the order matters: a clerk holds no
    `decide_change` action at all, so rule 1 of `assert_can` stops it before separation of duties
    is even reached. The same subject wearing an approver's role is the case separation of duties
    is for, and it is refused too.
    """
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
    result = _call(
        POST_INVOICE.bind(erp, clerk),
        {"invoice_id": BATCH_INVOICE, "account": "40100"},
        ctx,
    )
    staged = result.staged
    assert staged is not None
    with pytest.raises(AccessDenied, match="may not decide_change"):
        asyncio.run(staged.apply())
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.STAGED

    same_subject = Principal("clerk@demo", Role.APPROVER, "1000")
    assert isinstance(staged, StagedErpChange)
    with pytest.raises(AccessDenied, match="separation of duties"):
        asyncio.run(StagedErpChange(erp, same_subject, _row_of(erp, staged.change_key)).apply())
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.STAGED


def test_an_approvers_staged_change_applies_through_the_one_apply_path(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
    change = erp.stage_posting(clerk, BATCH_INVOICE, {"account": "40100"})
    staged = StagedErpChange(erp, approver, change)
    assert staged.change_key == change.change_key
    asyncio.run(staged.apply())
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.POSTED
    # A second apply is the stated no-op, because it goes through `apply_change` like everything.
    asyncio.run(staged.apply())
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.POSTED


def test_a_staged_change_can_carry_a_rejection(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
    change = erp.stage_posting(clerk, BATCH_INVOICE, {"account": "40100"})
    asyncio.run(
        StagedErpChange(erp, approver, change, decision=Decision.rejected("wrong account")).apply()
    )
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.MATCHED


def test_an_oversized_result_is_refused_rather_than_truncated(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    result = _call(OVERSIZED.bind(erp, clerk), {"size": MAX_RESULT_CHARS + 1}, ctx)
    assert result.is_error is True
    assert result.error_category is ErrorCategory.VALIDATION
    assert "smaller page" in result.content
    ok = _call(OVERSIZED.bind(erp, clerk), {"size": MAX_RESULT_CHARS}, ctx)
    assert ok.is_error is False


def test_a_tool_set_holds_only_what_the_role_may_call(
    erp: Erp, clerk: Principal, service: Principal
) -> None:
    operations: list[AnyErpOperation] = [GET_INVOICE, POST_INVOICE]
    clerk_set = build_tool_set(operations, erp, clerk)
    service_set = build_tool_set(operations, erp, service)
    assert clerk_set.names == ("get_invoice", "post_invoice")
    # A model is never shown a tool it cannot call: the write is left out, not included and refused.
    assert service_set.names == ("get_invoice",)
    with pytest.raises(ValidationFailed, match="no such tool"):
        service_set.get("post_invoice")


def test_a_manifest_is_a_subset_and_refuses_a_name_outside_it(erp: Erp, clerk: Principal) -> None:
    operations: list[AnyErpOperation] = [GET_INVOICE, POST_INVOICE]
    full = build_tool_set(operations, erp, clerk)
    step = full.subset(["get_invoice"])
    assert step.names == ("get_invoice",)
    with pytest.raises(ValidationFailed, match="no such tool"):
        full.subset(["nope"])


def test_two_operations_cannot_share_a_name(erp: Erp, clerk: Principal) -> None:
    from p2p.core.errors import InternalFailure

    duplicated: list[AnyErpOperation] = [GET_INVOICE, GET_INVOICE]
    with pytest.raises(InternalFailure, match="one name"):
        build_tool_set(duplicated, erp, clerk)


def test_an_empty_tool_set_is_a_tool_set(erp: Erp, clerk: Principal) -> None:
    del erp, clerk
    assert ToolSet().names == ()


def test_a_tool_is_bound_to_one_principal_for_its_whole_life(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    """The identity in the context does not change who the tool acts as."""
    tool = GET_INVOICE.bind(erp, Principal("svc-app19", Role.SERVICE, "2000"))
    pretending = ToolContext(
        run_id=ctx.run_id, turn=1, tool_use_id="tu-2", identity=Identity("clerk@demo")
    )
    result = _call(tool, {"invoice_id": BATCH_INVOICE}, pretending)
    # Bound to company 2000, so company 1000's invoice stays invisible whatever the context says.
    assert result.is_error is True
    assert result.error_category is ErrorCategory.PERMISSION
    del clerk


# ---------------------------------------------------------------------------------------------
# App 17's contract: a tool returns failures as results and does not raise -- including the
# failures this app did not name
# ---------------------------------------------------------------------------------------------


def _explodes(erp: Erp, principal: Principal, args: InvoiceArgs) -> OperationOutcome:
    del erp, principal, args
    raise RuntimeError("a bug, carrying 1001-A and company 1000 in its message")


def _broken_sql(erp: Erp, principal: Principal, args: InvoiceArgs) -> OperationOutcome:
    del erp, principal, args
    raise sqlite3.OperationalError("no such column: nonexistent_column")


EXPLODES = ErpOperation(
    name="explodes",
    description="A read with a bug in it.",
    effect=Effect.READ,
    input_model=InvoiceArgs,
    action=Action.READ_INVOICE,
    run=_explodes,
)
BROKEN_SQL = ErpOperation(
    name="broken_sql",
    description="A read with a mistake in its SQL.",
    effect=Effect.READ,
    input_model=InvoiceArgs,
    action=Action.READ_INVOICE,
    run=_broken_sql,
)


def test_an_unexpected_exception_becomes_an_internal_result_and_leaks_nothing(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    """`TransientFailure` being defined and never raised was the tell that this path was missing."""
    result = _call(EXPLODES.bind(erp, clerk), {"invoice_id": BATCH_INVOICE}, ctx)
    assert result.is_error is True
    assert result.error_category is ErrorCategory.INTERNAL
    assert result.is_retryable is False
    # The exception's own text is unreviewed: it may carry a row's values, so it is not returned.
    for leak in ("1001-A", "1000", "a bug"):
        assert leak not in result.content, leak


def test_a_driver_error_is_internal_rather_than_transient(
    erp: Erp, clerk: Principal, ctx: ToolContext
) -> None:
    """A contended database is already `TransientFailure` from `erp.py`, which is where the category
    is decided. So a `sqlite3.Error` that reaches the adapter is one nobody classified -- "no such
    column", a bug -- and reporting it as worth retrying would have a model try it for ever.
    """
    result = _call(BROKEN_SQL.bind(erp, clerk), {"invoice_id": BATCH_INVOICE}, ctx)
    assert result.error_category is ErrorCategory.INTERNAL
    assert result.is_retryable is False
    assert "nonexistent_column" not in result.content


def test_the_internal_log_line_carries_no_unreviewed_text(
    erp: Erp, clerk: Principal, ctx: ToolContext, caplog: pytest.LogCaptureFixture
) -> None:
    """`AGENTS.md`: the logger never carries document text, and an exception message is exactly
    that -- pydantic and sqlite3 both quote the value or the statement that caused the failure."""
    with caplog.at_level(logging.ERROR, logger="p2p.core.adapter"):
        _call(EXPLODES.bind(erp, clerk), {"invoice_id": BATCH_INVOICE}, ctx)
    (record,) = [entry for entry in caplog.records if entry.name == "p2p.core.adapter"]
    assert record.getMessage() == "tool failed internally"
    assert record.exc_info is None
    # `extra=` fields land in the record's `__dict__`, which its type stub does not declare.
    fields = record.__dict__
    assert fields["failure"] == "RuntimeError"
    assert fields["tool"] == "explodes"
    assert str(fields["where"]).startswith("test_adapter.py:")
    for leak in ("1001-A", "company 1000", "a bug"):
        assert leak not in caplog.text, leak


def test_a_locked_read_reaches_a_tool_as_transient_and_retryable(
    db_path: Path, ctx: ToolContext
) -> None:
    """The gateway's reads as app 19 would see them when the database is contended.

    `is_retryable` on a read tool is the whole of exam statement 2.2, and this is the assertion that
    pins it end to end: `erp.py` classifies the lock, the adapter passes the classification through.
    """
    with Erp.open(db_path, busy_timeout_ms=50) as blocked:
        holder = sqlite3.connect(db_path, isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            result = _call(
                GET_INVOICE.bind(blocked, Principal("clerk@demo", Role.CLERK, "1000")),
                {"invoice_id": BATCH_INVOICE},
                ctx,
            )
        finally:
            holder.close()
    assert result.is_error is True
    assert result.error_category is ErrorCategory.TRANSIENT
    assert result.is_retryable is True
