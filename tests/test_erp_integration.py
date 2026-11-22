"""The accessors, through a real database: one test per access rule, per table.

The company-code rule is checked on **every** table a principal can reach, not once on a
representative one: the filter is written out per query in `erp.py`, so one query missing its WHERE
clause is exactly the defect these tests exist to catch, and a single sample would not catch it.
"""

from __future__ import annotations

import sqlite3

import pytest

from p2p.core.access import Principal, Role
from p2p.core.erp import (
    CaseState,
    ChangeKind,
    Decision,
    DecisionKind,
    Erp,
    InvoiceState,
)
from p2p.core.errors import AccessDenied, ValidationFailed

BATCH_INVOICE = "INV-1001"
DISPUTED_INVOICE = "INV-1013"
OTHER_INVOICE = "INV-2001"
MY_CASE = "DC-0001"
OTHER_AGENTS_CASE = "DC-0002"


def _match(erp: Erp, clerk: Principal, invoice_id: str) -> None:
    """Walk an invoice to `matched` the way slices 18.4 and 18.5 will."""
    erp.advance_invoice(clerk, invoice_id, InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, invoice_id, InvoiceState.MATCHED)


def _staged_posting(erp: Erp, clerk: Principal, invoice_id: str = BATCH_INVOICE) -> str:
    _match(erp, clerk, invoice_id)
    return erp.stage_posting(clerk, invoice_id, {"account": "40100"}).change_key


# ---------------------------------------------------------------------------------------------
# Rule 2, the company-code filter: every table
# ---------------------------------------------------------------------------------------------


def test_supplier_reads_are_scoped_to_the_company_code(erp: Erp, clerk: Principal) -> None:
    assert erp.get_supplier(clerk, "SUP-1001").company_code == "1000"
    with pytest.raises(AccessDenied):
        erp.get_supplier(clerk, "SUP-2001")
    assert all(s.company_code == "1000" for s in erp.find_supplier(clerk, "a"))
    assert erp.find_supplier(clerk, "Lysander") == ()


def test_purchase_order_reads_are_scoped_to_the_company_code(erp: Erp, clerk: Principal) -> None:
    assert erp.get_purchase_order(clerk, "PO-1001").lines[0].qty == 100
    with pytest.raises(AccessDenied):
        erp.get_purchase_order(clerk, "PO-2001")


def test_goods_receipt_reads_are_scoped_to_the_company_code(erp: Erp, clerk: Principal) -> None:
    assert len(erp.get_goods_receipt(clerk, "PO-1006")) == 2
    assert erp.get_goods_receipt(clerk, "PO-2001") == ()


def test_invoice_reads_are_scoped_to_the_company_code(erp: Erp, clerk: Principal) -> None:
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.RECEIVED
    with pytest.raises(AccessDenied):
        erp.get_invoice(clerk, OTHER_INVOICE)
    assert all(i.company_code == "1000" for i in erp.list_open_invoices(clerk))


def test_a_cross_company_read_returns_a_permission_failure_and_no_row_data(
    erp: Erp, clerk: Principal
) -> None:
    """The spec's eval: `permission`, not retryable, and nothing of the row in the message."""
    with pytest.raises(AccessDenied) as refused:
        erp.get_invoice(clerk, OTHER_INVOICE)
    assert refused.value.category == "permission"
    assert refused.value.is_retryable is False
    message = str(refused.value)
    for leak in (OTHER_INVOICE, "2000", "2001-A", "Lysander"):
        assert leak not in message, leak


def test_payment_status_is_scoped_to_the_company_code(erp: Erp, clerk: Principal) -> None:
    assert erp.get_payment_status(clerk, DISPUTED_INVOICE).state is InvoiceState.POSTED
    with pytest.raises(AccessDenied):
        erp.get_payment_status(clerk, OTHER_INVOICE)


def test_extraction_reads_are_scoped_to_the_company_code(
    erp: Erp, clerk: Principal, other_clerk: Principal
) -> None:
    erp.record_extraction(
        clerk,
        BATCH_INVOICE,
        attempts=1,
        repaired=False,
        confidence=0.95,
        fields={"number": "1001-A"},
        model_task="invoice-extract",
    )
    assert len(erp.list_extractions(clerk, BATCH_INVOICE)) == 1
    assert erp.list_extractions(other_clerk, BATCH_INVOICE) == ()


def test_dispute_case_reads_are_scoped_to_the_company_code(erp: Erp, approver: Principal) -> None:
    other_approver = Principal("approver@other", Role.APPROVER, "2000")
    assert erp.get_case(approver, MY_CASE).company_code == "1000"
    with pytest.raises(AccessDenied):
        erp.get_case(other_approver, MY_CASE)
    assert erp.list_cases(other_approver) == ()


def test_case_event_reads_follow_the_case(erp: Erp, approver: Principal) -> None:
    other_approver = Principal("approver@other", Role.APPROVER, "2000")
    assert len(erp.list_case_events(approver, MY_CASE)) == 1
    with pytest.raises(AccessDenied):
        erp.list_case_events(other_approver, MY_CASE)


def test_batch_run_reads_are_scoped_to_the_company_code_and_to_who_started_it(
    erp: Erp, clerk: Principal, other_clerk: Principal, approver: Principal
) -> None:
    run = erp.record_batch_run(clerk, items=3, ok=1, failed=1, skipped=1, summary={"rows": []})
    assert erp.get_batch_run(clerk, run.id).items == 3
    with pytest.raises(AccessDenied):
        erp.get_batch_run(other_clerk, run.id)
    # `started_by` is an owner column: another clerk in the same company code does not see it.
    another_clerk = Principal("clerk2@demo", Role.CLERK, "1000")
    with pytest.raises(AccessDenied):
        erp.get_batch_run(another_clerk, run.id)
    # An approver reads everything in its company code.
    assert erp.get_batch_run(approver, run.id).started_by == "clerk@demo"


def test_staged_change_reads_are_scoped_to_the_company_code(
    erp: Erp, clerk: Principal, disputes: Principal
) -> None:
    key = _staged_posting(erp, clerk)
    other_approver = Principal("approver@other", Role.APPROVER, "2000")
    with pytest.raises(AccessDenied):
        other_approver_change = erp.get_staged(other_approver, key)
        del other_approver_change
    assert erp.list_staged(other_approver) == ()
    del disputes


def test_a_write_against_another_company_code_is_refused_on_every_staged_table(
    erp: Erp, other_clerk: Principal
) -> None:
    other_disputes = Principal("disputes@other", Role.DISPUTES, "2000")
    with pytest.raises(AccessDenied):
        erp.stage_posting(other_clerk, BATCH_INVOICE, {"account": "40100"})
    with pytest.raises(AccessDenied):
        erp.stage_credit_memo(
            other_disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
        )
    with pytest.raises(AccessDenied):
        erp.stage_payment_hold(other_disputes, invoice_id=DISPUTED_INVOICE, reason="x")


# ---------------------------------------------------------------------------------------------
# Rule 3, the disputes assignment rule
# ---------------------------------------------------------------------------------------------


def test_a_disputes_principal_reads_its_own_case_and_not_another_agents(
    erp: Erp, disputes: Principal
) -> None:
    assert erp.get_case(disputes, MY_CASE).assigned_to == "disputes@demo"
    with pytest.raises(AccessDenied, match="assigned to it"):
        erp.get_case(disputes, OTHER_AGENTS_CASE)
    # Both cases are in this principal's company code, so the filter alone would not catch it.
    assert [case.id for case in erp.list_cases(disputes)] == [MY_CASE]


def test_a_disputes_principal_cannot_read_another_agents_transcript(
    erp: Erp, disputes: Principal
) -> None:
    assert len(erp.list_case_events(disputes, MY_CASE)) == 1
    with pytest.raises(AccessDenied, match="assigned to it"):
        erp.list_case_events(disputes, OTHER_AGENTS_CASE)


def test_a_disputes_principal_cannot_write_to_another_agents_case(
    erp: Erp, disputes: Principal
) -> None:
    with pytest.raises(AccessDenied, match="assigned to it"):
        erp.append_case_event(disputes, OTHER_AGENTS_CASE, kind="note", payload={})
    with pytest.raises(AccessDenied, match="assigned to it"):
        erp.advance_case(disputes, OTHER_AGENTS_CASE, CaseState.RESOLVED)
    with pytest.raises(AccessDenied, match="assigned to it"):
        erp.set_case_summary(disputes, OTHER_AGENTS_CASE, "mine now")
    with pytest.raises(AccessDenied, match="assigned to it"):
        erp.stage_payment_hold(
            disputes, invoice_id=DISPUTED_INVOICE, reason="x", case_id=OTHER_AGENTS_CASE
        )


# ---------------------------------------------------------------------------------------------
# Rule 1, the role table, through the accessors
# ---------------------------------------------------------------------------------------------


def test_the_service_principal_reads_the_gateway_six_and_writes_nothing(
    erp: Erp, service: Principal
) -> None:
    assert erp.get_invoice(service, BATCH_INVOICE).id == BATCH_INVOICE
    assert erp.get_supplier(service, "SUP-1001").id == "SUP-1001"
    assert erp.get_purchase_order(service, "PO-1001").id == "PO-1001"
    assert erp.get_goods_receipt(service, "PO-1001")
    assert erp.list_open_invoices(service)
    assert erp.get_payment_status(service, BATCH_INVOICE)
    for forbidden in (
        lambda: erp.advance_invoice(service, BATCH_INVOICE, InvoiceState.EXTRACTED),
        lambda: erp.stage_posting(service, BATCH_INVOICE, {}),
        lambda: erp.record_extraction(
            service,
            BATCH_INVOICE,
            attempts=1,
            repaired=False,
            confidence=None,
            fields={},
            model_task="invoice-extract",
        ),
        lambda: erp.record_batch_run(service, items=0, ok=0, failed=0, skipped=0, summary={}),
        lambda: erp.open_case(service, supplier_id="SUP-1001"),
        lambda: erp.list_staged(service),
        lambda: erp.get_case(service, MY_CASE),
        lambda: erp.apply_change(service, "pst_000001", Decision.applied()),
    ):
        with pytest.raises(AccessDenied):
            forbidden()


def test_a_clerk_may_not_decide_and_an_approver_may_not_stage(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    key = _staged_posting(erp, clerk)
    with pytest.raises(AccessDenied, match="may not decide_change"):
        erp.apply_change(clerk, key, Decision.applied())
    with pytest.raises(AccessDenied, match="may not stage_posting"):
        erp.stage_posting(approver, "INV-1002", {})


def test_a_clerk_may_not_stage_money_and_a_disputes_agent_may_not_stage_a_posting(
    erp: Erp, clerk: Principal, disputes: Principal
) -> None:
    with pytest.raises(AccessDenied, match="may not stage_credit_memo"):
        erp.stage_credit_memo(clerk, supplier_id="SUP-1001", amount=100, currency="USD", reason="x")
    with pytest.raises(AccessDenied, match="may not stage_payment_hold"):
        erp.stage_payment_hold(clerk, invoice_id=DISPUTED_INVOICE, reason="x")
    _match(erp, clerk, BATCH_INVOICE)
    with pytest.raises(AccessDenied, match="may not stage_posting"):
        erp.stage_posting(disputes, BATCH_INVOICE, {})


# ---------------------------------------------------------------------------------------------
# Rule 4, separation of duties -- on all three staged-change tables
# ---------------------------------------------------------------------------------------------


def test_separation_of_duties_on_a_posting(erp: Erp, clerk: Principal) -> None:
    """The principal that staged a posting cannot decide it, whatever role it then holds.

    The role table already stops a clerk from deciding anything, so the shape this rule is really
    for is the same *subject* appearing as an approver -- one person with both hats. Separation of
    duties is enforced on `staged_by`, not on the role, which is why that case is refused too.
    """
    key = _staged_posting(erp, clerk)
    same_subject = Principal("clerk@demo", Role.APPROVER, "1000")
    with pytest.raises(AccessDenied, match="separation of duties"):
        erp.apply_change(same_subject, key, Decision.applied())
    # A different subject, who staged nothing, may decide it.
    other = Principal("approver@demo", Role.APPROVER, "1000")
    assert erp.apply_change(other, key, Decision.applied()).already_decided is False


def test_separation_of_duties_on_a_credit_memo(erp: Erp, disputes: Principal) -> None:
    change = erp.stage_credit_memo(
        disputes, supplier_id="SUP-1003", amount=400_000, currency="USD", reason="short delivery"
    )
    same_subject = Principal("disputes@demo", Role.APPROVER, "1000")
    with pytest.raises(AccessDenied, match="separation of duties"):
        erp.apply_change(same_subject, change.change_key, Decision.applied())


def test_separation_of_duties_on_a_payment_hold(erp: Erp, disputes: Principal) -> None:
    change = erp.stage_payment_hold(
        disputes, invoice_id=DISPUTED_INVOICE, reason="under dispute", case_id=MY_CASE
    )
    same_subject = Principal("disputes@demo", Role.APPROVER, "1000")
    with pytest.raises(AccessDenied, match="separation of duties"):
        erp.apply_change(same_subject, change.change_key, Decision.applied())


def test_an_approver_does_not_review_a_change_it_staged(erp: Erp, disputes: Principal) -> None:
    change = erp.stage_credit_memo(
        disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
    )
    same_subject = Principal("disputes@demo", Role.APPROVER, "1000")
    with pytest.raises(AccessDenied, match="separation of duties"):
        same_subject_change = erp.get_staged(same_subject, change.change_key)
        del same_subject_change


# ---------------------------------------------------------------------------------------------
# `list_staged`: the visibility rule, which is this slice's and not the command's
# ---------------------------------------------------------------------------------------------


def test_list_staged_shows_a_clerk_what_it_staged(erp: Erp, clerk: Principal) -> None:
    key = _staged_posting(erp, clerk)
    assert [change.change_key for change in erp.list_staged(clerk)] == [key]
    another_clerk = Principal("clerk2@demo", Role.CLERK, "1000")
    assert erp.list_staged(another_clerk) == ()


def test_list_staged_shows_a_disputes_agent_only_its_own(erp: Erp, disputes: Principal) -> None:
    credit = erp.stage_credit_memo(
        disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
    )
    hold = erp.stage_payment_hold(disputes, invoice_id=DISPUTED_INVOICE, reason="y")
    keys = {change.change_key for change in erp.list_staged(disputes)}
    assert keys == {credit.change_key, hold.change_key}
    other_agent = Principal("disputes2@demo", Role.DISPUTES, "1000")
    assert erp.list_staged(other_agent) == ()


def test_list_staged_shows_an_approver_everything_undecided_except_its_own(
    erp: Erp, clerk: Principal, disputes: Principal, approver: Principal
) -> None:
    posting = _staged_posting(erp, clerk)
    credit = erp.stage_credit_memo(
        disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
    )
    hold = erp.stage_payment_hold(disputes, invoice_id=DISPUTED_INVOICE, reason="y")
    assert {change.change_key for change in erp.list_staged(approver)} == {
        posting,
        credit.change_key,
        hold.change_key,
    }
    # A change this approver staged is off its own list: it could not decide it anyway.
    approver_as_disputes = Principal("approver@demo", Role.DISPUTES, "1000")
    mine = erp.stage_credit_memo(
        approver_as_disputes, supplier_id="SUP-1001", amount=50, currency="USD", reason="z"
    )
    listed = {change.change_key for change in erp.list_staged(approver)}
    assert mine.change_key not in listed


def test_list_staged_drops_a_change_once_it_is_decided(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    key = _staged_posting(erp, clerk)
    assert erp.list_staged(approver)
    erp.apply_change(approver, key, Decision.applied())
    assert erp.list_staged(approver) == ()
    assert erp.list_staged(clerk) == ()


def test_list_staged_carries_what_a_person_needs_to_decide(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    key = _staged_posting(erp, clerk)
    (change,) = erp.list_staged(approver)
    assert change.change_key == key
    assert change.staged_by == "clerk@demo"
    assert change.summary == f"post invoice {BATCH_INVOICE}"
    assert change.kind is ChangeKind.POSTING


# ---------------------------------------------------------------------------------------------
# The invoice transition table
# ---------------------------------------------------------------------------------------------


def test_the_two_clerk_transitions_are_the_only_ones_the_accessor_takes(
    erp: Erp, clerk: Principal
) -> None:
    assert erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED).state is (
        InvoiceState.EXTRACTED
    )
    assert erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED).state is (
        InvoiceState.MATCHED
    )
    with pytest.raises(ValidationFailed, match="only by staging a posting"):
        erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.STAGED)


def test_the_approval_transitions_are_refused_to_a_caller(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    """Neither role can reach `posted` through the accessor, and for two different reasons."""
    _staged_posting(erp, clerk)
    # The clerk holds the action, so it is the transition that refuses it.
    with pytest.raises(ValidationFailed, match="only by an approver deciding"):
        erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.POSTED)
    # The approver does not hold the action at all: its only route to a state change is a decision.
    with pytest.raises(AccessDenied, match="may not advance_invoice"):
        erp.advance_invoice(approver, BATCH_INVOICE, InvoiceState.POSTED)


def test_a_move_outside_the_table_is_refused(erp: Erp, clerk: Principal) -> None:
    with pytest.raises(ValidationFailed, match="not a transition"):
        erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
    with pytest.raises(ValidationFailed, match="not a transition"):
        erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.HELD)


def test_held_is_terminal(
    erp: Erp, clerk: Principal, disputes: Principal, approver: Principal
) -> None:
    hold = erp.stage_payment_hold(disputes, invoice_id=DISPUTED_INVOICE, reason="disputed")
    erp.apply_change(approver, hold.change_key, Decision.applied())
    assert erp.get_invoice(clerk, DISPUTED_INVOICE).state is InvoiceState.HELD
    for target in (InvoiceState.POSTED, InvoiceState.MATCHED, InvoiceState.EXTRACTED):
        with pytest.raises(ValidationFailed):
            erp.advance_invoice(clerk, DISPUTED_INVOICE, target)


def test_a_move_to_the_same_state_is_the_tables_no_op(erp: Erp, clerk: Principal) -> None:
    before = erp.get_invoice(clerk, BATCH_INVOICE)
    after = erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.RECEIVED)
    assert after == before


# ---------------------------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------------------------


def test_staging_a_posting_moves_the_invoice_with_it(erp: Erp, clerk: Principal) -> None:
    _match(erp, clerk, BATCH_INVOICE)
    change = erp.stage_posting(clerk, BATCH_INVOICE, {"account": "40100"})
    assert change.change_key.startswith("pst_")
    assert change.kind is ChangeKind.POSTING
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.STAGED
    assert change.decision is None


def test_a_posting_is_staged_only_against_a_matched_invoice(erp: Erp, clerk: Principal) -> None:
    with pytest.raises(ValidationFailed, match="matched invoice"):
        erp.stage_posting(clerk, BATCH_INVOICE, {})


def test_the_three_kinds_have_their_own_key_spaces(
    erp: Erp, clerk: Principal, disputes: Principal
) -> None:
    posting = _staged_posting(erp, clerk)
    credit = erp.stage_credit_memo(
        disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
    )
    hold = erp.stage_payment_hold(disputes, invoice_id=DISPUTED_INVOICE, reason="y")
    assert (posting, credit.change_key, hold.change_key) == (
        "pst_000001",
        "crd_000001",
        "hld_000001",
    )
    # The same digits in three tables: the prefix is what makes one token name one row.
    assert len({posting, credit.change_key, hold.change_key}) == 3


def test_a_malformed_change_key_is_a_validation_failure_that_echoes_nothing(
    erp: Erp, approver: Principal
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        erp.apply_change(approver, "posting-7; DROP TABLE invoice", Decision.applied())
    assert refused.value.category == "validation"
    assert "DROP TABLE" not in str(refused.value)


def test_a_well_formed_key_for_a_change_that_does_not_exist_reveals_nothing(
    erp: Erp, approver: Principal
) -> None:
    with pytest.raises(AccessDenied) as refused:
        erp.apply_change(approver, "pst_999999", Decision.applied())
    assert refused.value.category == "permission"


# ---------------------------------------------------------------------------------------------
# `apply_change`: the one apply path, its state effects and the apply-once guard
# ---------------------------------------------------------------------------------------------


def test_applying_a_posting_posts_the_invoice(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    key = _staged_posting(erp, clerk)
    outcome = erp.apply_change(approver, key, Decision.applied())
    assert outcome.already_decided is False
    assert outcome.decision.kind is DecisionKind.APPLIED
    assert outcome.invoice_state is InvoiceState.POSTED
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.POSTED


def test_rejecting_a_posting_returns_the_invoice_to_matched_with_the_reason_recorded(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    key = _staged_posting(erp, clerk)
    outcome = erp.apply_change(approver, key, Decision.rejected("tax code is wrong"))
    assert outcome.invoice_state is InvoiceState.MATCHED
    stored = erp.get_staged(approver, key)
    assert stored.decision is not None
    assert stored.decision.kind is DecisionKind.REJECTED
    assert stored.decision.reason == "tax code is wrong"
    assert stored.decided_by == "approver@demo"
    # And it can be staged again, which is why the rejection goes back to `matched`.
    assert erp.stage_posting(clerk, BATCH_INVOICE, {}).change_key == "pst_000002"


def test_applying_a_payment_hold_is_the_only_path_to_held(
    erp: Erp, clerk: Principal, disputes: Principal, approver: Principal
) -> None:
    hold = erp.stage_payment_hold(
        disputes, invoice_id=DISPUTED_INVOICE, reason="disputed", case_id=MY_CASE
    )
    assert erp.get_invoice(clerk, DISPUTED_INVOICE).state is InvoiceState.POSTED
    outcome = erp.apply_change(approver, hold.change_key, Decision.applied())
    assert outcome.invoice_state is InvoiceState.HELD
    status = erp.get_payment_status(clerk, DISPUTED_INVOICE)
    assert status.state is InvoiceState.HELD
    assert status.applied_holds == (hold.change_key,)


def test_rejecting_a_payment_hold_moves_nothing(
    erp: Erp, clerk: Principal, disputes: Principal, approver: Principal
) -> None:
    hold = erp.stage_payment_hold(disputes, invoice_id=DISPUTED_INVOICE, reason="disputed")
    erp.apply_change(approver, hold.change_key, Decision.rejected("not our problem"))
    assert erp.get_invoice(clerk, DISPUTED_INVOICE).state is InvoiceState.POSTED


def test_a_hold_cannot_be_applied_to_an_invoice_that_is_not_posted(
    erp: Erp, clerk: Principal, disputes: Principal, approver: Principal
) -> None:
    hold = erp.stage_payment_hold(disputes, invoice_id=BATCH_INVOICE, reason="early")
    with pytest.raises(ValidationFailed, match="not a transition"):
        erp.apply_change(approver, hold.change_key, Decision.applied())
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.RECEIVED
    # Refused, and the change is still undecided: nothing was half-written.
    assert erp.get_staged(approver, hold.change_key).decision is None


def test_applying_a_credit_memo_moves_no_invoice_state(
    erp: Erp, clerk: Principal, disputes: Principal, approver: Principal
) -> None:
    credit = erp.stage_credit_memo(
        disputes,
        supplier_id="SUP-1003",
        invoice_id=DISPUTED_INVOICE,
        amount=400_000,
        currency="USD",
        reason="short delivery",
    )
    outcome = erp.apply_change(approver, credit.change_key, Decision.applied())
    assert outcome.invoice_state is InvoiceState.POSTED
    assert erp.get_invoice(clerk, DISPUTED_INVOICE).state is InvoiceState.POSTED
    assert erp.get_payment_status(clerk, DISPUTED_INVOICE).applied_credit_total == 400_000


def test_a_second_apply_is_the_stated_no_op_on_every_kind(
    erp: Erp, clerk: Principal, disputes: Principal, approver: Principal
) -> None:
    posting = _staged_posting(erp, clerk)
    credit = erp.stage_credit_memo(
        disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
    )
    hold = erp.stage_payment_hold(disputes, invoice_id=DISPUTED_INVOICE, reason="y")
    for key in (posting, credit.change_key, hold.change_key):
        first = erp.apply_change(approver, key, Decision.applied())
        assert first.already_decided is False, key
        again = erp.apply_change(approver, key, Decision.applied())
        assert again.already_decided is True, key
        assert "already decided" in again.detail
        assert again.decision.kind is DecisionKind.APPLIED


def test_a_rejected_change_cannot_then_be_applied(
    erp: Erp, clerk: Principal, approver: Principal
) -> None:
    key = _staged_posting(erp, clerk)
    erp.apply_change(approver, key, Decision.rejected("wrong account"))
    again = erp.apply_change(approver, key, Decision.applied())
    assert again.already_decided is True
    assert again.decision.kind is DecisionKind.REJECTED
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.MATCHED


def test_the_apply_once_guard_is_in_the_database_not_in_the_caller(
    erp: Erp, clerk: Principal, approver: Principal, raw: sqlite3.Connection
) -> None:
    """Prove the guard without going through `apply_change` at all.

    If the only thing stopping a second decision were Python, this UPDATE would succeed.
    """
    key = _staged_posting(erp, clerk)
    erp.apply_change(approver, key, Decision.applied())
    for table_update in (
        "UPDATE posting SET decision = 'applied', decided_by = 'x', applied_at = 'y'"
        " WHERE id = 'pst_000001'",
        "UPDATE posting SET decision = 'rejected: changed my mind', decided_by = 'x',"
        " applied_at = 'y' WHERE id = 'pst_000001'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="already been decided"):
            raw.execute(table_update)


def test_the_apply_once_guard_is_on_all_three_tables(
    erp: Erp, disputes: Principal, approver: Principal, raw: sqlite3.Connection
) -> None:
    credit = erp.stage_credit_memo(
        disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
    )
    hold = erp.stage_payment_hold(disputes, invoice_id=DISPUTED_INVOICE, reason="y")
    erp.apply_change(approver, credit.change_key, Decision.applied())
    erp.apply_change(approver, hold.change_key, Decision.applied())
    with pytest.raises(sqlite3.IntegrityError, match="already been decided"):
        raw.execute(
            "UPDATE credit_memo SET decision = 'applied', decided_by = 'x', applied_at = 'y'"
            " WHERE id = 'crd_000001'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="already been decided"):
        raw.execute(
            "UPDATE payment_hold SET decision = 'applied', decided_by = 'x', applied_at = 'y'"
            " WHERE id = 'hld_000001'"
        )


def test_a_decision_racing_another_reports_what_was_recorded(
    erp: Erp, clerk: Principal, approver: Principal, raw: sqlite3.Connection
) -> None:
    """The branch that catches the trigger: a decision landing between the read and the write."""
    key = _staged_posting(erp, clerk)
    raw.execute(
        "UPDATE posting SET decision = 'rejected: decided elsewhere', decided_by = 'approver2',"
        " applied_at = '2026-11-21T10:00:00Z' WHERE id = ?",
        (key,),
    )
    outcome = erp.apply_change(approver, key, Decision.applied())
    assert outcome.already_decided is True
    assert outcome.decision.reason == "decided elsewhere"
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.STAGED


# ---------------------------------------------------------------------------------------------
# The remaining accessors later slices depend on
# ---------------------------------------------------------------------------------------------


def test_the_extraction_trail_is_append_only_and_records_a_repair(
    erp: Erp, clerk: Principal
) -> None:
    first = erp.record_extraction(
        clerk,
        BATCH_INVOICE,
        attempts=1,
        repaired=False,
        confidence=0.4,
        fields={"total": 125000},
        model_task="invoice-extract",
    )
    second = erp.record_extraction(
        clerk,
        BATCH_INVOICE,
        attempts=2,
        repaired=True,
        confidence=0.93,
        fields={"total": 125000},
        model_task="invoice-extract",
    )
    trail = erp.list_extractions(clerk, BATCH_INVOICE)
    assert [row.id for row in trail] == [first.id, second.id]
    assert [row.repaired for row in trail] == [False, True]
    with pytest.raises(ValidationFailed, match="attempts must be 1 or 2"):
        erp.record_extraction(
            clerk,
            BATCH_INVOICE,
            attempts=3,
            repaired=True,
            confidence=None,
            fields={},
            model_task="invoice-extract",
        )
    with pytest.raises(ValidationFailed, match="two attempts"):
        erp.record_extraction(
            clerk,
            BATCH_INVOICE,
            attempts=1,
            repaired=True,
            confidence=None,
            fields={},
            model_task="invoice-extract",
        )


def test_recording_an_extraction_never_moves_the_invoice(erp: Erp, clerk: Principal) -> None:
    """A re-ingest of an advanced invoice records a row and writes no state (spec, batch rules)."""
    _match(erp, clerk, BATCH_INVOICE)
    erp.record_extraction(
        clerk,
        BATCH_INVOICE,
        attempts=1,
        repaired=False,
        confidence=0.99,
        fields={},
        model_task="invoice-extract",
    )
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.MATCHED


def test_a_nul_byte_is_refused_at_the_boundary(erp: Erp, clerk: Principal) -> None:
    with pytest.raises(ValidationFailed, match="NUL byte"):
        erp.record_extraction(
            clerk,
            BATCH_INVOICE,
            attempts=1,
            repaired=False,
            confidence=None,
            fields={"note": "a\x00b"},
            model_task="invoice-extract",
        )
    with pytest.raises(ValidationFailed, match="NUL byte"):
        Decision.rejected("why\x00not")


def test_a_case_runs_forward_and_never_back(erp: Erp, disputes: Principal) -> None:
    case = erp.open_case(disputes, supplier_id="SUP-1001", invoice_id=BATCH_INVOICE)
    assert case.state is CaseState.OPEN
    assert case.assigned_to == "disputes@demo"
    erp.advance_case(disputes, case.id, CaseState.INVESTIGATING)
    escalated = erp.advance_case(
        disputes, case.id, CaseState.ESCALATED, escalated_reason="the invoice has no PO"
    )
    assert escalated.escalated_reason == "the invoice has no PO"
    with pytest.raises(ValidationFailed, match="does not move back"):
        erp.advance_case(disputes, case.id, CaseState.OPEN)
    with pytest.raises(ValidationFailed, match="criterion"):
        erp.advance_case(disputes, "DC-0001", CaseState.ESCALATED)


def test_a_case_keeps_a_rolling_summary_and_the_last_ten_events(
    erp: Erp, disputes: Principal
) -> None:
    for index in range(12):
        erp.append_case_event(disputes, MY_CASE, kind="note", payload={"i": index})
    events = erp.list_case_events(disputes, MY_CASE)
    assert len(events) == 10
    assert [event.seq for event in events] == list(range(4, 14))
    assert events[-1].payload["i"] == 11
    assert events[0].actor == "disputes@demo"
    updated = erp.set_case_summary(disputes, MY_CASE, "supplier claims a short delivery")
    assert updated.summary == "supplier claims a short delivery"
    replaced = erp.set_case_summary(disputes, MY_CASE, "credit agreed in principle")
    assert replaced.summary == "credit agreed in principle"


def test_a_batch_run_must_add_up(erp: Erp, clerk: Principal) -> None:
    run = erp.record_batch_run(
        clerk, items=12, ok=10, failed=1, skipped=1, summary={"stopped_early": None, "rows": []}
    )
    assert run.items == run.ok + run.failed + run.skipped
    assert run.started_by == "clerk@demo"
    assert run.summary["rows"] == []
    with pytest.raises(ValidationFailed, match="items must equal"):
        erp.record_batch_run(clerk, items=12, ok=10, failed=1, skipped=0, summary={})


def test_reads_are_capped_and_paged(erp: Erp, clerk: Principal) -> None:
    first = erp.list_open_invoices(clerk, limit=5)
    assert len(first) == 5
    second = erp.list_open_invoices(clerk, limit=5, offset=5)
    assert len(second) == 5
    assert {i.id for i in first} & {i.id for i in second} == set()
    assert len(erp.list_open_invoices(clerk, limit=10_000)) <= 100
    with pytest.raises(ValidationFailed, match="at least 1"):
        erp.list_open_invoices(clerk, limit=0)
    with pytest.raises(ValidationFailed, match="negative"):
        erp.list_open_invoices(clerk, offset=-1)


def test_a_supplier_search_treats_a_wildcard_as_text(erp: Erp, clerk: Principal) -> None:
    assert erp.find_supplier(clerk, "%") == ()
    assert [s.id for s in erp.find_supplier(clerk, "Rhine")] == ["SUP-1003"]


def test_posted_and_held_invoices_are_not_open(erp: Erp, clerk: Principal) -> None:
    ids = {invoice.id for invoice in erp.list_open_invoices(clerk)}
    assert DISPUTED_INVOICE not in ids
    assert BATCH_INVOICE in ids
