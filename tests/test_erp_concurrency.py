"""Two writers at once: the guards that make the transition table hold, and the lock's own errors.

Regression tests for the defect this slice shipped at first. `stage_posting` read the invoice's
state, checked it, and only then opened its transaction, so two connections could both read
`matched`, serialize, and the second never looked again -- one invoice, two postings, and both
applied cleanly, because applying a posting whose invoice is already `posted` is the apply path's
documented no-op. Nothing anywhere reported an error.

**These tests are deliberately not written as "read it, then watch someone else change it".** The
first round of them was, and review measured what that was worth: a stale value read into a local
and never used by the write never enters the window at all, so three of those four tests passed
against the buggy code every time, and the fourth -- a two-thread race on one invoice -- caught it
in 7 runs out of 12. A test that finds a defect three times in five is not a regression test. So
the stale row is injected where the accessor actually reads it, which fails deterministically
without the fix, and the genuine two-connection race runs over every seeded invoice rather than
once, so that it stops being a coin flip.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from p2p.core.access import Principal, Role
from p2p.core.erp import (
    BatchRun,
    CaseState,
    Decision,
    DisputeCase,
    Erp,
    InvoiceState,
    _one_row_changed,
)
from p2p.core.errors import InternalFailure, TransientFailure, ValidationFailed

BATCH_INVOICES = tuple(f"INV-{1000 + n:04d}" for n in range(1, 13))
BATCH_INVOICE = BATCH_INVOICES[0]


def _clerk(subject: str = "clerk@demo") -> Principal:
    return Principal(subject, Role.CLERK, "1000")


def _approver() -> Principal:
    return Principal("approver@demo", Role.APPROVER, "1000")


def _to_matched(erp: Erp, principal: Principal, invoice_id: str) -> None:
    erp.advance_invoice(principal, invoice_id, InvoiceState.EXTRACTED)
    erp.advance_invoice(principal, invoice_id, InvoiceState.MATCHED)


def _post(erp: Erp, invoice_id: str) -> None:
    clerk = _clerk()
    _to_matched(erp, clerk, invoice_id)
    change = erp.stage_posting(clerk, invoice_id, {})
    erp.apply_change(_approver(), change.change_key, Decision.applied())


def _freeze_row(erp: Erp, monkeypatch: pytest.MonkeyPatch, row: sqlite3.Row) -> None:
    """Make the accessor's own invoice read return `row`, whatever the table now says.

    This is the window itself: the row the accessor checks was true when it was read and is not
    true any more. Injected at `_invoice_row` because that is where a write accessor reads it --
    the point the first version of these tests never reached.
    """
    monkeypatch.setattr(Erp, "_invoice_row", lambda _self, _principal, _invoice_id: row)


def _freeze_case(erp: Erp, monkeypatch: pytest.MonkeyPatch, row: sqlite3.Row) -> None:
    monkeypatch.setattr(Erp, "_case_row", lambda _self, _principal, _case_id: row)


@pytest.fixture
def plain(db_path: Path, erp: Erp) -> Iterator[sqlite3.Connection]:
    del erp
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------------------------
# The state a write checked must still hold when the write runs
# ---------------------------------------------------------------------------------------------


def test_a_stale_matched_row_cannot_stage_a_posting_on_a_posted_invoice(
    erp: Erp, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    clerk = _clerk()
    stale = erp._invoice_row(clerk, BATCH_INVOICE)
    _to_matched(erp, clerk, BATCH_INVOICE)
    matched = erp._invoice_row(clerk, BATCH_INVOICE)
    del stale
    erp.apply_change(
        _approver(), erp.stage_posting(clerk, BATCH_INVOICE, {}).change_key, Decision.applied()
    )
    assert erp.get_invoice(clerk, BATCH_INVOICE).state is InvoiceState.POSTED

    _freeze_row(erp, monkeypatch, matched)
    with pytest.raises(TransientFailure, match="changed state"):
        erp.stage_posting(clerk, BATCH_INVOICE, {"by": "the stale view"})

    # The whole staging rolled back: no orphan posting row, and the invoice is untouched.
    assert plain.execute("SELECT COUNT(*) AS n FROM posting").fetchone()["n"] == 1
    assert (
        plain.execute("SELECT state FROM invoice WHERE id = ?", (BATCH_INVOICE,)).fetchone()[
            "state"
        ]
        == "posted"
    )


def test_a_stale_extracted_row_cannot_un_post_an_invoice(
    erp: Erp, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    """`matched` written over `posted` was the other half of the same defect."""
    clerk = _clerk()
    erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED)
    extracted = erp._invoice_row(clerk, BATCH_INVOICE)
    _post(erp, BATCH_INVOICE)

    _freeze_row(erp, monkeypatch, extracted)
    with pytest.raises(TransientFailure, match="changed state"):
        erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.MATCHED)
    assert (
        plain.execute("SELECT state FROM invoice WHERE id = ?", (BATCH_INVOICE,)).fetchone()[
            "state"
        ]
        == "posted"
    )


def test_a_stale_case_row_cannot_walk_a_case_backwards(
    erp: Erp, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    disputes = Principal("disputes@demo", Role.DISPUTES, "1000")
    case = erp.open_case(disputes, supplier_id="SUP-1001")
    open_row = erp._case_row(disputes, case.id)
    erp.advance_case(disputes, case.id, CaseState.ESCALATED, escalated_reason="no PO")

    _freeze_case(erp, monkeypatch, open_row)
    with pytest.raises(TransientFailure, match="changed state"):
        erp.advance_case(disputes, case.id, CaseState.INVESTIGATING)
    assert (
        plain.execute("SELECT state FROM dispute_case WHERE id = ?", (case.id,)).fetchone()["state"]
        == "escalated"
    )


# ---------------------------------------------------------------------------------------------
# The genuine race, over every seeded invoice rather than once
# ---------------------------------------------------------------------------------------------


def _race_to_stage(db_path: Path, invoice_id: str) -> list[object]:
    """Two threads, each with its own connection, staging a posting for one invoice at once.

    A free function rather than a closure in the loop below: a nested one would capture the loop's
    own variables, which is both what ruff's B023 is about and a real hazard in a threaded test.
    """
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def stage(subject: str) -> None:
        # Each thread opens its own connection: sqlite3 refuses one across threads, and two
        # connections is the shape under test.
        with Erp.open(db_path) as erp:
            barrier.wait(timeout=20)
            try:
                change = erp.stage_posting(_clerk(subject), invoice_id, {"by": subject})
            except Exception as failure:  # the test records whatever came back, pass or fail
                with lock:
                    results.append(failure)
            else:
                with lock:
                    results.append(change.change_key)

    threads = [threading.Thread(target=stage, args=(f"clerk{n}@demo",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), invoice_id
    return results


def test_two_connections_racing_to_stage_produce_exactly_one_posting_every_time(
    db_path: Path,
) -> None:
    """Twelve races, not one. A single race caught the original defect about three times in five."""
    with Erp.open(db_path) as setup:
        for invoice_id in BATCH_INVOICES:
            _to_matched(setup, _clerk(), invoice_id)

    for invoice_id in BATCH_INVOICES:
        results = _race_to_stage(db_path, invoice_id)
        keys = [item for item in results if isinstance(item, str)]
        failures = [item for item in results if isinstance(item, Exception)]
        assert len(keys) == 1, (invoice_id, results)
        # The loser is told why in the ERP's own terms, never as a driver exception.
        assert len(failures) == 1, (invoice_id, results)
        assert isinstance(failures[0], ValidationFailed | TransientFailure), failures[0]

    probe = sqlite3.connect(db_path)
    try:
        assert probe.execute("SELECT COUNT(*) FROM posting").fetchone()[0] == len(BATCH_INVOICES)
        states = {
            row[0]
            for row in probe.execute(
                "SELECT DISTINCT state FROM invoice WHERE id IN (SELECT invoice_id FROM posting)"
            )
        }
        assert states == {"staged"}
    finally:
        probe.close()


def test_the_affected_row_guard_refuses_a_count_that_is_not_one(tmp_path: Path) -> None:
    """The guard every state UPDATE in `erp.py` goes through, on its own."""
    connection = sqlite3.connect(tmp_path / "scratch.sqlite3")
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


# ---------------------------------------------------------------------------------------------
# A busy database is `transient`, decided in `erp.py` and not by a caller
# ---------------------------------------------------------------------------------------------


def test_a_locked_database_is_reported_as_transient_not_as_a_driver_error(
    db_path: Path,
) -> None:
    """Slice 18.7 must be able to turn a locked document into a `failed` row, not a traceback."""
    with Erp.open(db_path) as built:
        _to_matched(built, _clerk(), BATCH_INVOICE)

    holder = sqlite3.connect(db_path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE invoice SET state = 'matched' WHERE id = ?", (BATCH_INVOICE,))
        # 50 ms rather than the default five seconds: the mapping is what is under test, not the
        # wait. That `busy_timeout_ms` is a parameter is why this test takes no time at all.
        with Erp.open(db_path, busy_timeout_ms=50) as blocked:
            with pytest.raises(TransientFailure, match="busy") as refused:
                blocked.stage_posting(_clerk(), BATCH_INVOICE, {})
            assert refused.value.category == "transient"
            assert refused.value.is_retryable is True
    finally:
        holder.close()


def test_a_locked_read_is_transient_too_not_a_dead_end(db_path: Path) -> None:
    """A blocked READ is retryable, and it reaches the gateway's six read tools.

    This was the unfinished half of the contention fix: the mapping was wired into `_write`, and no
    read goes through `_write`, so a busy read escaped as a driver error and the adapter reported it
    `internal` -- a dead end, on the frozen contract app 19 consumes, where `is_retryable` is the
    whole of exam statement 2.2.
    """
    # The connection is opened BEFORE the lock is taken: an exclusive lock blocks even the
    # `PRAGMA user_version` read that `Erp.open` makes, which the next test is about.
    with Erp.open(db_path, busy_timeout_ms=50) as blocked:
        holder = sqlite3.connect(db_path, isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            for read in (
                lambda: blocked.get_invoice(_clerk(), BATCH_INVOICE),
                lambda: blocked.list_open_invoices(_clerk()),
                lambda: blocked.get_payment_status(_clerk(), BATCH_INVOICE),
                lambda: blocked.find_supplier(_clerk(), "Rhine"),
                lambda: blocked.list_staged(_approver()),
            ):
                with pytest.raises(TransientFailure, match="busy") as refused:
                    read()
                assert refused.value.category == "transient"
                assert refused.value.is_retryable is True
        finally:
            holder.close()


def test_opening_a_locked_database_for_the_first_time_is_transient(tmp_path: Path) -> None:
    """Mid-build is neither "already built" nor corrupt: the caller may simply try again."""
    target = tmp_path / "contended.sqlite3"
    holder = sqlite3.connect(target, isolation_level=None)
    try:
        holder.execute("CREATE TABLE placeholder (x INTEGER)")
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(TransientFailure, match="busy"):
            Erp.open(target, busy_timeout_ms=50)
    finally:
        holder.close()


def test_a_bug_in_a_statement_is_not_reported_as_worth_retrying(erp: Erp) -> None:
    """`sqlite3.OperationalError` also means "no such column", which is not transient."""
    with pytest.raises(sqlite3.OperationalError, match="no such column"), erp._write() as conn:
        conn.execute("UPDATE invoice SET state = nonexistent_column WHERE id = 'INV-1001'")
    # The failed transaction was rolled back, and the connection is usable again.
    assert erp.get_invoice(_clerk(), BATCH_INVOICE).state is InvoiceState.RECEIVED


# ---------------------------------------------------------------------------------------------
# Two callers opening the same path for the first time
# ---------------------------------------------------------------------------------------------


def test_two_first_opens_of_one_path_both_succeed(tmp_path: Path) -> None:
    """`_initialize` reads `user_version` before anything holds a write lock, so both see 0."""
    target = tmp_path / "shared.sqlite3"
    barrier = threading.Barrier(4)
    failures: list[BaseException] = []
    lock = threading.Lock()

    def first_open() -> None:
        try:
            barrier.wait(timeout=20)
            with Erp.open(target) as erp:
                assert len(erp.list_open_invoices(_clerk())) == 12
        except BaseException as failure:  # the loser used to get a raw driver exception
            with lock:
                failures.append(failure)

    threads = [threading.Thread(target=first_open) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert failures == []


def test_a_loser_of_the_first_open_race_returns_once_the_schema_is_there(
    db_path: Path, erp: Erp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery branch, deterministically: `user_version` read as 0 on a built database.

    That is exactly what the loser of the race above sees, and the threaded test cannot be relied
    on to produce it. Here `_initialize` runs the script, SQLite says the tables already exist, and
    the re-read finds the schema in place -- which is what the caller wanted.
    """
    del erp
    answers = iter([0])
    monkeypatch.setattr(
        Erp,
        "_applied_version",
        lambda self: next(answers, 1),
    )
    with Erp.open(db_path) as reopened:
        assert len(reopened.list_open_invoices(_clerk())) == 12


def test_a_database_that_is_broken_rather_than_already_built_still_raises(
    db_path: Path, erp: Erp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery must not swallow a genuine failure: only "it is already built" is forgiven."""
    del erp
    monkeypatch.setattr(Erp, "_applied_version", lambda self: 0)
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        Erp.open(db_path)


# ---------------------------------------------------------------------------------------------
# Ids are generated by counting rows, which is safe only inside the transaction that uses them
# ---------------------------------------------------------------------------------------------


def test_an_id_cannot_be_generated_outside_the_transaction_that_will_use_it(erp: Erp) -> None:
    """The guard behind the defect class the suite could not see.

    `MAX(seq) + 1` and `COUNT(*) + 1` are unique only while the count and its INSERT are in one
    `BEGIN IMMEDIATE`. Hoisting any of the three reads out used to leave the whole suite green while
    two writers collided on the counter under load, so the invariant is asserted in the code and
    this is the test of that assertion.
    """
    from p2p.core.erp import ChangeKind

    with pytest.raises(InternalFailure, match="inside the write's transaction"):
        erp._next_key(erp._conn, ChangeKind.POSTING)
    # Inside one, the same call is fine -- and claims the next number.
    with erp._write() as conn:
        key, seq = erp._next_key(conn, ChangeKind.POSTING)
    assert (key, seq) == ("pst_000001", 1)


@contextmanager
def _without_a_transaction(self: Erp) -> Iterator[sqlite3.Connection]:
    """`_write` with the `BEGIN IMMEDIATE` taken away: the shape a hoisted read would produce."""
    yield self._conn


def test_every_generated_id_is_claimed_inside_the_transaction(
    erp: Erp, disputes: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three generators, not just the change key.

    Removing the guard from `_next_key` fails a test; removing it from the case or batch-run id did
    not, until this one. Each is a `COUNT(*) + 1` with the same collision under load, and `erp.py`
    is a foundation path nobody may edit later, so each call site is pinned rather than the helper.
    """
    monkeypatch.setattr(Erp, "_write", _without_a_transaction)
    with pytest.raises(InternalFailure, match="case id"):
        erp.open_case(disputes, supplier_id="SUP-1001")
    with pytest.raises(InternalFailure, match="batch run id"):
        erp.record_batch_run(_clerk(), items=0, ok=0, failed=0, skipped=0, summary={})
    with pytest.raises(InternalFailure, match="change key"):
        erp.stage_credit_memo(
            disputes, supplier_id="SUP-1001", amount=100, currency="USD", reason="x"
        )


def _race_to_stage_two(db_path: Path, left: str, right: str) -> list[object]:
    """Two threads staging postings for two DIFFERENT invoices at the same moment.

    A free function, not a closure in the caller's loop: a nested one would capture the loop's own
    variables, which is ruff's B023 and a real hazard in a threaded test.
    """
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def stage(invoice_id: str) -> None:
        with Erp.open(db_path) as erp:
            barrier.wait(timeout=20)
            try:
                change = erp.stage_posting(_clerk(), invoice_id, {})
            except Exception as failure:  # a key collision would arrive as a driver exception
                with lock:
                    results.append(failure)
            else:
                with lock:
                    results.append(change.change_key)

    threads = [threading.Thread(target=stage, args=(invoice,)) for invoice in (left, right)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return results


def test_two_clerks_staging_different_invoices_at_once_get_different_keys(db_path: Path) -> None:
    """The failure the guard prevents: a *shared* per-table counter, not a per-invoice one.

    Two postings for two different invoices never conflict on the invoice, so nothing else in the
    suite makes them contend -- but they take their key from the same `MAX(seq)` and so must be
    serialized. Six pairs, because one pair reproduced the collision only two times in three.
    """
    with Erp.open(db_path) as setup:
        for invoice_id in BATCH_INVOICES:
            _to_matched(setup, _clerk(), invoice_id)

    pairs = [(BATCH_INVOICES[n], BATCH_INVOICES[n + 1]) for n in range(0, 12, 2)]
    for left, right in pairs:
        results = _race_to_stage_two(db_path, left, right)
        assert all(isinstance(item, str) for item in results), (left, right, results)
        assert len(set(results)) == 2, (left, right, results)

    probe = sqlite3.connect(db_path)
    try:
        keys = [row[0] for row in probe.execute("SELECT id FROM posting ORDER BY seq")]
    finally:
        probe.close()
    assert keys == [f"pst_{n:06d}" for n in range(1, 13)]


def test_a_duplicate_change_key_is_refused_by_the_schema_and_rolls_the_write_back(
    erp: Erp, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    """What the guard above is protecting: uniqueness itself is the schema's, not Python's."""
    from p2p.core.erp import ChangeKind

    clerk = _clerk()
    _to_matched(erp, clerk, BATCH_INVOICES[0])
    first = erp.stage_posting(clerk, BATCH_INVOICES[0], {})
    _to_matched(erp, clerk, BATCH_INVOICES[1])

    monkeypatch.setattr(
        Erp,
        "_next_key",
        lambda _self, _conn, _kind: (first.change_key, 1),
    )
    with pytest.raises(sqlite3.IntegrityError):
        erp.stage_posting(clerk, BATCH_INVOICES[1], {})
    assert ChangeKind.POSTING is ChangeKind("posting")
    # Nothing half-written: one posting, and the second invoice never left `matched`.
    assert plain.execute("SELECT COUNT(*) AS n FROM posting").fetchone()["n"] == 1
    assert (
        plain.execute("SELECT state FROM invoice WHERE id = ?", (BATCH_INVOICES[1],)).fetchone()[
            "state"
        ]
        == "matched"
    )


# ---------------------------------------------------------------------------------------------
# A write accessor's own response is read inside its transaction
# ---------------------------------------------------------------------------------------------
#
# Otherwise a lock taken between the commit and the response read raises `TransientFailure` on a
# write that has already committed -- and `AGENTS.md` tells the caller in so many words that a
# `TransientFailure` means nothing committed and is safe to retry. Codex reproduced the
# consequence: the failed call left one credit, and retrying the same arguments created a second.
#
# Each test makes the response read fail and asserts the write is not there afterwards. The failure
# is injected rather than raced because the window is between a commit and the next statement.


def test_a_failed_response_read_takes_the_staged_write_with_it(
    erp: Erp, disputes: Principal, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    def blocked(_self: Erp, _principal: Principal, _change_key: str) -> sqlite3.Row:
        raise TransientFailure("the ERP was busy; this can be tried again")

    monkeypatch.setattr(Erp, "_change_row", blocked)
    with pytest.raises(TransientFailure):
        erp.stage_credit_memo(
            disputes, supplier_id="SUP-1003", amount=1000, currency="USD", reason="x"
        )
    # Nothing committed, so the caller may retry on the strength of the category it was given.
    assert plain.execute("SELECT COUNT(*) AS n FROM credit_memo").fetchone()["n"] == 0


def test_a_failed_response_read_takes_a_state_change_with_it(
    erp: Erp, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    clerk = _clerk()
    calls: list[int] = []
    real = Erp._invoice_row

    def blocked_on_the_second_read(self: Erp, principal: Principal, invoice_id: str) -> sqlite3.Row:
        calls.append(1)
        if len(calls) > 1:  # the first read is the accessor's check; the second is its response
            raise TransientFailure("the ERP was busy; this can be tried again")
        return real(self, principal, invoice_id)

    monkeypatch.setattr(Erp, "_invoice_row", blocked_on_the_second_read)
    with pytest.raises(TransientFailure):
        erp.advance_invoice(clerk, BATCH_INVOICE, InvoiceState.EXTRACTED)
    assert (
        plain.execute("SELECT state FROM invoice WHERE id = ?", (BATCH_INVOICE,)).fetchone()[
            "state"
        ]
        == "received"
    )


def test_a_failed_response_read_takes_a_new_case_with_it(
    erp: Erp, disputes: Principal, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    before = plain.execute("SELECT COUNT(*) AS n FROM dispute_case").fetchone()["n"]

    def blocked(_self: Erp, _principal: Principal, _case_id: str) -> DisputeCase:
        raise TransientFailure("the ERP was busy; this can be tried again")

    monkeypatch.setattr(Erp, "get_case", blocked)
    with pytest.raises(TransientFailure):
        erp.open_case(disputes, supplier_id="SUP-1001")
    assert plain.execute("SELECT COUNT(*) AS n FROM dispute_case").fetchone()["n"] == before


def test_a_failed_response_read_takes_a_batch_run_with_it(
    erp: Erp, monkeypatch: pytest.MonkeyPatch, plain: sqlite3.Connection
) -> None:
    def blocked(_self: Erp, _principal: Principal, _run_id: str) -> BatchRun:
        raise TransientFailure("the ERP was busy; this can be tried again")

    monkeypatch.setattr(Erp, "get_batch_run", blocked)
    with pytest.raises(TransientFailure):
        erp.record_batch_run(_clerk(), items=1, ok=1, failed=0, skipped=0, summary={})
    assert plain.execute("SELECT COUNT(*) AS n FROM batch_run").fetchone()["n"] == 0
