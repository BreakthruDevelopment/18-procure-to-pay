-- The simulated ERP's schema. Slice 18.2 owns this file; later slices add no tables and change no
-- column, because every table in the spec's data model is here from the start.
--
-- There is no Oracle and no SOA Suite anywhere in this repo: this is a SQLite file, applied once by
-- `p2p/core/erp.py` on first open, together with `erp/seed.sql`.
--
-- Conventions that hold everywhere below:
--
--   * MONEY IS AN INTEGER IN MINOR CURRENCY UNITS. 125000 with currency 'USD' is 1,250.00. The
--     spec's data model names these columns `total`, `unit_price`, `tax` and `amount`; the names are
--     kept and the unit is stated here, once. No column anywhere is REAL except `confidence`, which
--     is a 0-1 probability and not money.
--   * `company_code` is the owner column every read filters on. The filter lives inside
--     `p2p/core/erp.py`, never in a caller.
--   * Booleans are INTEGER 0/1 with a CHECK, because SQLite has no boolean type.
--   * Timestamps are ISO-8601 UTC strings ('2026-11-21T09:00:00Z'), which sort lexicographically.
--   * Every `*_json` column carries a CHECK (json_valid(...)): a malformed audit row is worse than
--     a refused write.
--
-- `PRAGMA foreign_keys` is connection-scoped, not schema-scoped, so it cannot be set here. `erp.py`
-- sets it on every connection it opens; a test asserts it does.

-- ---------------------------------------------------------------------------------------------
-- Seeded, read-only reference data
-- ---------------------------------------------------------------------------------------------

CREATE TABLE supplier (
    id           TEXT    PRIMARY KEY,
    company_code TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    terms        TEXT    NOT NULL,
    -- The "flagged supplier" escalation criterion (spec, Escalation): one of the five.
    risk_flag    INTEGER NOT NULL DEFAULT 0 CHECK (risk_flag IN (0, 1))
);
CREATE INDEX supplier_by_company ON supplier (company_code);
CREATE INDEX supplier_by_company_name ON supplier (company_code, name);

CREATE TABLE purchase_order (
    id           TEXT    PRIMARY KEY,
    company_code TEXT    NOT NULL,
    supplier_id  TEXT    NOT NULL REFERENCES supplier (id),
    currency     TEXT    NOT NULL,
    total        INTEGER NOT NULL CHECK (total >= 0),
    -- The data model carries PO lines on the order itself (`lines`), unlike invoice lines, which are
    -- their own table. A JSON array of {line_no, description, qty, unit_price}.
    lines        TEXT    NOT NULL CHECK (json_valid(lines))
);
CREATE INDEX purchase_order_by_company ON purchase_order (company_code);

CREATE TABLE goods_receipt (
    id           TEXT    PRIMARY KEY,
    company_code TEXT    NOT NULL,
    po_id        TEXT    NOT NULL REFERENCES purchase_order (id),
    line_no      INTEGER NOT NULL CHECK (line_no >= 1),
    qty_received INTEGER NOT NULL CHECK (qty_received >= 0),
    received_at  TEXT    NOT NULL,
    UNIQUE (po_id, line_no)
);
CREATE INDEX goods_receipt_by_company ON goods_receipt (company_code);

-- ---------------------------------------------------------------------------------------------
-- Invoices
-- ---------------------------------------------------------------------------------------------

-- `state` is the spec's transition table, which `erp.py` -- and nothing else -- enforces. The CHECK
-- here pins the set of states; the accessor pins the moves between them. A later slice that invents
-- a state is refused by the database, not only by review.
CREATE TABLE invoice (
    id           TEXT    PRIMARY KEY,
    company_code TEXT    NOT NULL,
    supplier_id  TEXT    NOT NULL REFERENCES supplier (id),
    -- Nullable on purpose: an invoice with no PO reference is the "no PO" escalation criterion.
    po_ref       TEXT    REFERENCES purchase_order (id),
    number       TEXT    NOT NULL,
    date         TEXT    NOT NULL,
    currency     TEXT    NOT NULL,
    total        INTEGER NOT NULL CHECK (total >= 0),
    state        TEXT    NOT NULL CHECK (
                     state IN ('received', 'extracted', 'matched', 'staged', 'posted', 'held')
                 )
);
CREATE INDEX invoice_by_company_state ON invoice (company_code, state);
CREATE INDEX invoice_by_company_number ON invoice (company_code, supplier_id, number);

CREATE TABLE invoice_line (
    invoice_id  TEXT    NOT NULL REFERENCES invoice (id),
    line_no     INTEGER NOT NULL CHECK (line_no >= 1),
    description TEXT    NOT NULL,
    qty         INTEGER NOT NULL,
    unit_price  INTEGER NOT NULL,
    tax         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (invoice_id, line_no)
);

-- ---------------------------------------------------------------------------------------------
-- The append-only audit trail (statement 4.4)
-- ---------------------------------------------------------------------------------------------

-- `attempts BETWEEN 1 AND 2` is the spec's cap written into the schema: one initial attempt plus at
-- most one repair. A slice that tries a second repair cannot record it.
CREATE TABLE extraction (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_code TEXT    NOT NULL,
    invoice_id   TEXT    NOT NULL REFERENCES invoice (id),
    attempts     INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 2),
    repaired     INTEGER NOT NULL CHECK (repaired IN (0, 1)),
    -- NULL when the attempt ended in `needs_human`: there is no calibrated confidence to record.
    confidence   REAL    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    fields_json  TEXT    NOT NULL CHECK (json_valid(fields_json)),
    model_task   TEXT    NOT NULL,
    recorded_at  TEXT    NOT NULL
);
CREATE INDEX extraction_by_invoice ON extraction (invoice_id, id);

CREATE TRIGGER extraction_is_append_only_update BEFORE UPDATE ON extraction
BEGIN
    SELECT RAISE(ABORT, 'extraction is append-only');
END;
CREATE TRIGGER extraction_is_append_only_delete BEFORE DELETE ON extraction
BEGIN
    SELECT RAISE(ABORT, 'extraction is append-only');
END;

-- ---------------------------------------------------------------------------------------------
-- The three staged-change tables
-- ---------------------------------------------------------------------------------------------
--
-- These seed EMPTY: every row in them is written by a run.
--
-- `id` is an opaque prefixed key -- `pst_`, `crd_`, `hld_` -- because a bare rowid is ambiguous
-- across the three tables: each is its own id space, so the same integer names a row in all three.
-- The prefix is what `apply_change` resolves on, so one token names one change with no `kind`
-- argument to get wrong. `seq` is the per-table counter the key's digits come from; the key is
-- derived rather than random so that `p2p demo` and `scripts/smoke.sh`, which build a fresh database
-- per run, print the same keys every time.
--
-- THE APPLY-ONCE GUARD is the `*_decided_once` trigger on each table, and it is in the database, not
-- in a caller: `apply_change` issues an UPDATE with no "WHERE decision IS NULL", so a second
-- decision is refused by SQLite and surfaces as the stated no-op. The spec asks for "a unique index
-- on (id, applied_at)-style guard"; in SQLite a unique index cannot express this, for two reasons
-- that are worth writing down rather than discovering twice: a unique index treats NULLs as
-- distinct, so it cannot stop a row moving from undecided to decided, and a CHECK constraint cannot
-- see OLD values. A BEFORE UPDATE trigger is the mechanism SQLite gives for exactly this, and it
-- fails closed. The CHECK constraints alongside keep the three decision columns in step, so a
-- half-written decision cannot exist.

CREATE TABLE posting (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE CHECK (id LIKE 'pst\_%' ESCAPE '\'),
    company_code TEXT    NOT NULL,
    invoice_id   TEXT    NOT NULL REFERENCES invoice (id),
    payload_json TEXT    NOT NULL CHECK (json_valid(payload_json)),
    staged_by    TEXT    NOT NULL,
    staged_at    TEXT    NOT NULL,
    decided_by   TEXT,
    -- 'applied', or 'rejected: <reason>'. The spec puts the rejection reason on this column; `erp.py`
    -- parses it back into a typed `Decision` so no caller ever reads the string.
    decision     TEXT,
    applied_at   TEXT,
    CHECK ((decision IS NULL) = (decided_by IS NULL)),
    CHECK ((decision IS NULL) = (applied_at IS NULL))
);
CREATE INDEX posting_undecided ON posting (company_code, decision);
CREATE TRIGGER posting_decided_once BEFORE UPDATE OF decision, decided_by, applied_at ON posting
WHEN OLD.decision IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'this posting has already been decided');
END;

CREATE TABLE credit_memo (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE CHECK (id LIKE 'crd\_%' ESCAPE '\'),
    company_code TEXT    NOT NULL,
    supplier_id  TEXT    NOT NULL REFERENCES supplier (id),
    -- Nullable: a credit can be raised against a supplier without naming one invoice.
    invoice_id   TEXT    REFERENCES invoice (id),
    amount       INTEGER NOT NULL CHECK (amount > 0),
    -- Carried because the 2,500 escalation criterion is "in any currency": a bare amount could not
    -- be judged against it.
    currency     TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    staged_by    TEXT    NOT NULL,
    staged_at    TEXT    NOT NULL,
    decided_by   TEXT,
    decision     TEXT,
    applied_at   TEXT,
    CHECK ((decision IS NULL) = (decided_by IS NULL)),
    CHECK ((decision IS NULL) = (applied_at IS NULL))
);
CREATE INDEX credit_memo_undecided ON credit_memo (company_code, decision);
CREATE TRIGGER credit_memo_decided_once
BEFORE UPDATE OF decision, decided_by, applied_at ON credit_memo
WHEN OLD.decision IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'this credit memo has already been decided');
END;

CREATE TABLE payment_hold (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE CHECK (id LIKE 'hld\_%' ESCAPE '\'),
    company_code TEXT    NOT NULL,
    invoice_id   TEXT    NOT NULL REFERENCES invoice (id),
    -- A hold outside a dispute leaves `case_id` null.
    case_id      TEXT    REFERENCES dispute_case (id),
    reason       TEXT    NOT NULL,
    staged_by    TEXT    NOT NULL,
    staged_at    TEXT    NOT NULL,
    decided_by   TEXT,
    decision     TEXT,
    applied_at   TEXT,
    CHECK ((decision IS NULL) = (decided_by IS NULL)),
    CHECK ((decision IS NULL) = (applied_at IS NULL))
);
CREATE INDEX payment_hold_undecided ON payment_hold (company_code, decision);
CREATE TRIGGER payment_hold_decided_once
BEFORE UPDATE OF decision, decided_by, applied_at ON payment_hold
WHEN OLD.decision IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'this payment hold has already been decided');
END;

-- ---------------------------------------------------------------------------------------------
-- The supplier disputes desk (statements 5.1, 5.2)
-- ---------------------------------------------------------------------------------------------

-- `summary` holds slice 18.6's rolling summary, and `invoice_id` the invoice a case is about. Both
-- are here so 18.6 needs no schema change: the spec gives it the state machine, not the schema.
CREATE TABLE dispute_case (
    id               TEXT NOT NULL PRIMARY KEY,
    company_code     TEXT NOT NULL,
    supplier_id      TEXT NOT NULL REFERENCES supplier (id),
    invoice_id       TEXT REFERENCES invoice (id),
    state            TEXT NOT NULL CHECK (
                         state IN ('open', 'investigating', 'credit_proposed', 'escalated', 'resolved')
                     ),
    opened_by        TEXT NOT NULL,
    -- The owner column that scopes a case to one person: a `disputes` principal sees only its own.
    assigned_to      TEXT NOT NULL,
    escalated_reason TEXT,
    opened_at        TEXT NOT NULL,
    summary          TEXT
);
CREATE INDEX dispute_case_by_company_assignee ON dispute_case (company_code, assigned_to);

CREATE TABLE case_event (
    case_id      TEXT    NOT NULL REFERENCES dispute_case (id),
    seq          INTEGER NOT NULL CHECK (seq >= 1),
    actor        TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    payload_json TEXT    NOT NULL CHECK (json_valid(payload_json)),
    recorded_at  TEXT    NOT NULL,
    PRIMARY KEY (case_id, seq)
);

CREATE TRIGGER case_event_is_append_only_update BEFORE UPDATE ON case_event
BEGIN
    SELECT RAISE(ABORT, 'case_event is append-only');
END;
CREATE TRIGGER case_event_is_append_only_delete BEFORE DELETE ON case_event
BEGIN
    SELECT RAISE(ABORT, 'case_event is append-only');
END;

-- ---------------------------------------------------------------------------------------------
-- Batch runs (statement 4.5)
-- ---------------------------------------------------------------------------------------------

-- The CHECK is the spec's invariant: every document found lands in exactly one of the three
-- outcomes, so `items = ok + failed + skipped` always, including for a run the budget stopped early.
-- `stopped_early` and the per-item rows live in `summary_json`, not in columns.
CREATE TABLE batch_run (
    id           TEXT    PRIMARY KEY,
    company_code TEXT    NOT NULL,
    started_by   TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    items        INTEGER NOT NULL CHECK (items >= 0),
    ok           INTEGER NOT NULL CHECK (ok >= 0),
    failed       INTEGER NOT NULL CHECK (failed >= 0),
    skipped      INTEGER NOT NULL CHECK (skipped >= 0),
    summary_json TEXT    NOT NULL CHECK (json_valid(summary_json)),
    CHECK (items = ok + failed + skipped)
);
CREATE INDEX batch_run_by_starter ON batch_run (started_by, started_at);
