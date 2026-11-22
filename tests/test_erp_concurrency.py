"""Two principals writing at once, and the guards that make the transition table hold anyway.

These are regression tests for the defect this slice shipped at first: `stage_posting` read the
invoice's state, checked it, and only then opened its transaction. Two connections could both read
`matched`, serialize, and the second never looked again -- so one invoice got two postings, and both
applied cleanly, because applying a posting whose invoice is already `posted` is the apply path's
documented no-op. Nothing anywhere reported an error.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from p2p.core.access import Principal, Role
from p2p.core.erp import CaseState, Decision, Erp, InvoiceState, _one_row_changed
from p2p.core.errors import TransientFailure, ValidationFailed

BATCH_INVOICE = "INV-1001"


def _clerk(subject: str = "clerk@demo") -> Principal:
    return Principal(subject, Role.CLERK, "1000")


def _to_matched(erp: Erp, principal: Principal) -> None:
    erp.advance_invoice(principal, BATCH_INVOICE, InvoiceState.EXTRACTED)
    erp.advance_invoice(principal, BATCH_INVOICE, InvoiceState.MATCHED)


def test_two_connections_racing_to_stage_produce_exactly_one_posting(db_path: Path) -> None:
    with Erp.open(db_path) as setup:
        _to_matched(setup, _clerk())

    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def stage(subject: str) -> None:
        # Each thread opens its own connection: sqlite3 refuses one across threads, and two
        # connections is the shape under test.
        with Erp.open(db_path) as erp:
            barrier.wait(timeout=20)
            try:
                change = erp.stage_posting(_clerk(subject), BATCH_INVOICE, {"by": subject})
            except Exception as failure:  # the test records whatever came back, pass or fail
                with lock:
                    outcomes.append(failure)
            else:
                with lock:
                    outcomes.append(change.change_key)

    threads = [threading.Thread(target=stage, args=(f"clerk{n}@demo",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    keys = [outcome for outcome in outcomes if isinstance(outcome, str)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(keys) == 1, outcomes
    assert len(failures) == 1, outcomes
    # The loser is told why in the ERP's own terms, not by a driver error.
    assert isinstance(failures[0], ValidationFailed | TransientFailure)

    probe = sqlite3.connect(db_path)
    try:
        assert probe.execute("SELECT COUNT(*) FROM posting").fetchone()[0] == 1
        assert (
            probe.execute("SELECT state FROM invoice WHERE id = ?", (BATCH_INVOICE,)).fetchone()[0]
            == "staged"
        )
    finally:
        probe.close()


def test_a_second_handle_cannot_stage_a_posting_an_invoice_has_already_moved_past(
    db_path: Path,
) -> None:
    """The check is made from committed state at write time, never from a value read earlier."""
    with Erp.open(db_path) as first, Erp.open(db_path) as second:
        clerk = _clerk()
        _to_matched(first, clerk)
        # `second` reads it while it is still `matched` -- the stale view a caller would hold.
        assert second.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.MATCHED
        first.stage_posting(clerk, BATCH_INVOICE, {"by": "first"})
        with pytest.raises(ValidationFailed, match="this one is staged"):
            second.stage_posting(clerk, BATCH_INVOICE, {"by": "second"})


def test_a_second_handle_cannot_un_post_an_invoice_with_a_stale_transition(
    db_path: Path,
) -> None:
    """`matched` written over `posted` was the other half of the same defect."""
    with Erp.open(db_path) as first, Erp.open(db_path) as second:
        clerk = _clerk()
        approver = Principal("approver@demo", Role.APPROVER, "1000")
        first.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED)
        assert second.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.EXTRACTED
        first.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
        change = first.stage_posting(clerk, BATCH_INVOICE, {})
        first.apply_change(approver, change.change_key, Decision.applied())
        assert first.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.POSTED
        # `second` still believes it is `extracted`, and asks for the move that was legal then.
        with pytest.raises(ValidationFailed, match="not a transition"):
            second.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
        assert first.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.POSTED


def test_a_case_cannot_be_walked_backwards_from_a_stale_view(db_path: Path) -> None:
    with Erp.open(db_path) as first, Erp.open(db_path) as second:
        disputes = Principal("disputes@demo", Role.DISPUTES, "1000")
        case = first.open_case(disputes, supplier_id="SUP-1001")
        # `second` reads the case while it is still `open` -- the stale view.
        assert second.get_case(disputes, case.id).state is CaseState.OPEN
        first.advance_case(
            disputes, case.id, CaseState.ESCALATED, escalated_reason="no PO on the invoice"
        )
        with pytest.raises(ValidationFailed, match="does not move back"):
            second.advance_case(disputes, case.id, CaseState.INVESTIGATING)
        assert second.get_case(disputes, case.id).state is CaseState.ESCALATED


def test_the_affected_row_guard_refuses_a_count_that_is_not_one(db_path: Path) -> None:
    """The guard every state UPDATE in `erp.py` goes through, on its own."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, state TEXT)")
        connection.execute("INSERT INTO t (id, state) VALUES (1, 'a')")
        _one_row_changed(
            connection.execute("UPDATE t SET state = 'b' WHERE id = 1 AND state = 'a'"), "nope"
        )
        with pytest.raises(TransientFailure, match="it moved"):
            _one_row_changed(
                connection.execute("UPDATE t SET state = 'c' WHERE id = 1 AND state = 'a'"),
                "it moved",
            )
    finally:
        connection.close()
