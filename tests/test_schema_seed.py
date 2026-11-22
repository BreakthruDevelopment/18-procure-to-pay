"""The schema applies once, the seed is the shape the spec describes, and the guards are real."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from p2p.core.erp import SCHEMA_VERSION, Erp, packaged_sql_path, repo_sql_path, sql_sources


def _count(raw: sqlite3.Connection, table: str) -> int:
    # Table names come from this module's own literals, never from input.
    counts = {
        "supplier": "SELECT COUNT(*) AS n FROM supplier",
        "purchase_order": "SELECT COUNT(*) AS n FROM purchase_order",
        "goods_receipt": "SELECT COUNT(*) AS n FROM goods_receipt",
        "invoice": "SELECT COUNT(*) AS n FROM invoice",
        "invoice_line": "SELECT COUNT(*) AS n FROM invoice_line",
        "extraction": "SELECT COUNT(*) AS n FROM extraction",
        "posting": "SELECT COUNT(*) AS n FROM posting",
        "credit_memo": "SELECT COUNT(*) AS n FROM credit_memo",
        "payment_hold": "SELECT COUNT(*) AS n FROM payment_hold",
        "dispute_case": "SELECT COUNT(*) AS n FROM dispute_case",
        "case_event": "SELECT COUNT(*) AS n FROM case_event",
        "batch_run": "SELECT COUNT(*) AS n FROM batch_run",
    }
    row = raw.execute(counts[table]).fetchone()
    assert row is not None
    value = row["n"]
    assert isinstance(value, int)
    return value


def test_every_table_in_the_data_model_exists(raw: sqlite3.Connection) -> None:
    names = {
        row["name"] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "supplier",
        "purchase_order",
        "goods_receipt",
        "invoice",
        "invoice_line",
        "extraction",
        "posting",
        "credit_memo",
        "payment_hold",
        "dispute_case",
        "case_event",
        "batch_run",
    } <= names


def test_seed_has_the_shape_the_spec_describes(raw: sqlite3.Connection) -> None:
    assert _count(raw, "supplier") == 9
    assert _count(raw, "purchase_order") == 24
    assert _count(raw, "goods_receipt") == 25
    assert _count(raw, "invoice") == 15
    assert _count(raw, "invoice_line") == 16
    # Two company codes, and twelve invoices for the batch.
    codes = {
        row["company_code"] for row in raw.execute("SELECT DISTINCT company_code FROM supplier")
    }
    assert codes == {"1000", "2000"}
    row = raw.execute(
        "SELECT COUNT(*) AS n FROM invoice WHERE company_code = '1000' AND state = 'received'"
    ).fetchone()
    assert row["n"] == 12


def test_the_three_staged_change_tables_and_the_trails_seed_empty(raw: sqlite3.Connection) -> None:
    for table in ("posting", "credit_memo", "payment_hold", "extraction", "batch_run"):
        assert _count(raw, table) == 0, table


def test_the_seed_holds_three_deliberate_variances(raw: sqlite3.Connection) -> None:
    # Quantity: the invoice bills 50 against 38 received -- the spec's "12 units over".
    received = raw.execute(
        "SELECT qty_received FROM goods_receipt WHERE po_id = 'PO-1002' AND line_no = 1"
    ).fetchone()
    billed = raw.execute(
        "SELECT qty FROM invoice_line WHERE invoice_id = 'INV-1002' AND line_no = 1"
    ).fetchone()
    assert billed["qty"] - received["qty_received"] == 12
    # Quantity, the other way: a short delivery billed in full.
    short = raw.execute(
        "SELECT qty_received FROM goods_receipt WHERE po_id = 'PO-1003' AND line_no = 1"
    ).fetchone()
    assert short["qty_received"] == 20
    # Price: the invoice line is 10% above the order line, with the quantity matching.
    order = raw.execute("SELECT lines FROM purchase_order WHERE id = 'PO-1004'").fetchone()
    assert '"unit_price":50000' in order["lines"]
    invoiced = raw.execute(
        "SELECT unit_price FROM invoice_line WHERE invoice_id = 'INV-1004' AND line_no = 1"
    ).fetchone()
    assert invoiced["unit_price"] == 55000


def test_the_seed_holds_one_invoice_under_dispute_and_one_case_for_another_agent(
    raw: sqlite3.Connection,
) -> None:
    assert _count(raw, "dispute_case") == 2
    assert _count(raw, "case_event") == 2
    disputed = raw.execute("SELECT state FROM invoice WHERE id = 'INV-1013'").fetchone()
    # `posted`, because applying a payment hold is the only path to `held` and it starts there.
    assert disputed["state"] == "posted"
    mine = raw.execute("SELECT assigned_to FROM dispute_case WHERE id = 'DC-0001'").fetchone()
    theirs = raw.execute(
        "SELECT company_code, assigned_to FROM dispute_case WHERE id = 'DC-0002'"
    ).fetchone()
    assert mine["assigned_to"] == "disputes@demo"
    # Same company code, different assignee: the row the company filter alone would let through.
    assert theirs["company_code"] == "1000"
    assert theirs["assigned_to"] != "disputes@demo"


def test_one_invoice_has_no_po_reference(raw: sqlite3.Connection) -> None:
    row = raw.execute("SELECT po_ref FROM invoice WHERE id = 'INV-1011'").fetchone()
    assert row["po_ref"] is None


def test_first_open_records_the_schema_version_and_later_opens_change_nothing(
    db_path: Path,
) -> None:
    with Erp.open(db_path) as first:
        del first
    probe = sqlite3.connect(db_path, isolation_level=None)
    probe.row_factory = sqlite3.Row
    try:
        version = probe.execute("PRAGMA user_version").fetchone()["user_version"]
        assert version == SCHEMA_VERSION
        probe.execute("UPDATE invoice SET state = 'matched' WHERE id = 'INV-1001'")
        with Erp.open(db_path) as second:
            del second
        # A second open neither re-applies the seed nor resets what a run did.
        assert probe.execute("SELECT COUNT(*) AS n FROM supplier").fetchone()["n"] == 9
        assert (
            probe.execute("SELECT state FROM invoice WHERE id = 'INV-1001'").fetchone()["state"]
            == "matched"
        )
    finally:
        probe.close()


def test_opening_refuses_a_database_from_another_schema_version(db_path: Path) -> None:
    probe = sqlite3.connect(db_path, isolation_level=None)
    try:
        probe.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7:d}")
    finally:
        probe.close()
    with pytest.raises(Exception, match="different schema version"):
        Erp.open(db_path)


def test_foreign_keys_are_enforced_on_every_connection(erp: Erp, raw: sqlite3.Connection) -> None:
    del erp
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO invoice_line (invoice_id, line_no, description, qty, unit_price, tax)"
            " VALUES ('INV-NOPE', 1, 'x', 1, 1, 0)"
        )


def test_the_append_only_trails_refuse_updates_and_deletes(raw: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("UPDATE case_event SET kind = 'rewritten' WHERE case_id = 'DC-0001'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("DELETE FROM case_event WHERE case_id = 'DC-0001'")
    raw.execute(
        "INSERT INTO extraction (company_code, invoice_id, attempts, repaired, confidence,"
        " fields_json, model_task, recorded_at)"
        " VALUES ('1000', 'INV-1001', 1, 0, 0.9, '{}', 'invoice-extract', '2026-11-21T09:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("UPDATE extraction SET attempts = 2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("DELETE FROM extraction")


def test_the_schema_refuses_an_invented_invoice_state(raw: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE invoice SET state = 'paid' WHERE id = 'INV-1001'")


def test_the_schema_refuses_a_batch_run_whose_totals_do_not_add_up(raw: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO batch_run (id, company_code, started_by, started_at, items, ok, failed,"
            " skipped, summary_json)"
            " VALUES ('BR-X', '1000', 'clerk@demo', '2026-11-21T09:00:00Z', 12, 11, 0, 0, '{}')"
        )


def test_the_schema_refuses_a_more_than_two_attempt_extraction(raw: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO extraction (company_code, invoice_id, attempts, repaired, confidence,"
            " fields_json, model_task, recorded_at)"
            " VALUES ('1000', 'INV-1001', 3, 1, 0.5, '{}', 'invoice-extract',"
            " '2026-11-21T09:00:00Z')"
        )


def test_a_half_written_decision_cannot_exist(raw: sqlite3.Connection) -> None:
    raw.execute(
        "INSERT INTO posting (seq, id, company_code, invoice_id, payload_json, staged_by,"
        " staged_at) VALUES (1, 'pst_000001', '1000', 'INV-1001', '{}', 'clerk@demo',"
        " '2026-11-21T09:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE posting SET decision = 'applied' WHERE id = 'pst_000001'")


def test_the_sql_resolves_from_somewhere_and_the_two_copies_agree() -> None:
    """Both halves matter, and the first is what makes this test say something in a checkout.

    A source checkout has only `erp/`; an installed wheel has only `p2p/_sql/`. Asserting the
    comparison alone passed vacuously in both, which is no test at all -- so assert first that the
    SQL resolves, then compare the copies wherever both are present.
    """
    for name in ("schema.sql", "seed.sql"):
        packaged, repo = packaged_sql_path(name), repo_sql_path(name)
        assert packaged is not None or repo is not None, name
        if packaged is not None and repo is not None:
            assert packaged.read_bytes() == repo.read_bytes(), name


def test_the_sql_the_app_would_apply_is_readable() -> None:
    schema, seed = sql_sources()
    assert "CREATE TABLE posting" in schema
    assert "INSERT INTO supplier" in seed
