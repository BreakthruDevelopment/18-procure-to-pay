"""The simulated ERP: the only module in this repo that opens the database.

Everything the rest of the app knows about stored state comes through an `Erp` instance. That is not
a style preference -- it is where two of the threat model's controls live:

* **The `company_code` filter is inside every query here**, not in a caller. A caller cannot forget
  it, because it has no SQL of its own to forget it in.
* **Every accessor takes the acting `Principal` and calls `assert_can`** before it reads or writes.
  The check and the filter are independent: a query that lost its WHERE clause is still refused by
  `assert_can`, and a row fetched by a correct query is still judged against the principal.

Slice 18.2 owns this file, and it owns the accessors for every table a later slice touches, so that
no later slice has to edit it -- `p2p/core/` is a foundation path they may not change, and a slice
that needed a new write path would have to stop and comment instead. The accessors written here for
later slices: `record_extraction` and `advance_invoice` (18.4), `stage_posting` (18.5),
`open_case`, `advance_case`, `set_case_summary`, `append_case_event`, `stage_credit_memo` and
`stage_payment_hold` (18.6), `record_batch_run` (18.7), the read `list_staged` (rendered by
`p2p staged`, whose visibility rule is therefore here and not in that command), and `apply_change`,
the one path that applies or rejects a staged change of any of the three kinds.

Money is INTEGER minor units everywhere; see the header of `erp/schema.sql`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Final, Protocol

from p2p.core.access import Action, Principal, Role, assert_can
from p2p.core.errors import AccessDenied, InternalFailure, ValidationFailed

# ------------------------------------------------------------------------------------------------
# Where the SQL lives
# ------------------------------------------------------------------------------------------------
#
# The source of truth is `erp/schema.sql` and `erp/seed.sql` at the repo root, as the spec names
# them. `pyproject.toml` force-includes both into the wheel at `p2p/_sql/`, because `erp/` is not a
# Python package and an installed wheel has no repo root to look in: without that, `p2p` would
# import cleanly from a wheel and then fail to open a database, which is the kind of break a
# checkout-root test never catches. The packaged copy is preferred, and the repo copy is the
# fallback for a source checkout; a test asserts that when both exist they are byte-identical.

#: Anchored on the `p2p` package rather than on a `p2p._sql` package: the force-included directory
#: carries no `__init__.py`, and asking `importlib.resources` for a package that does not exist is a
#: different (and version-dependent) story from asking a package that does for a path inside it.
_PACKAGED_SQL_DIR: Final = "_sql"
_REPO_SQL_DIR: Final = Path(__file__).resolve().parent.parent.parent / "erp"


def packaged_sql_path(name: str) -> Path | None:
    """The copy inside the installed package, if this installation has one."""
    candidate = resources.files("p2p").joinpath(_PACKAGED_SQL_DIR, name)
    try:
        if not candidate.is_file():
            return None
    except (OSError, FileNotFoundError):  # pragma: no cover - a zipped or exotic loader
        return None
    return Path(str(candidate))


def repo_sql_path(name: str) -> Path | None:
    """The copy at `erp/` in a source checkout, if this is one."""
    candidate = _REPO_SQL_DIR / name
    return candidate if candidate.is_file() else None


def _read_sql(name: str) -> str:
    for candidate in (packaged_sql_path(name), repo_sql_path(name)):
        if candidate is not None:
            return candidate.read_text(encoding="utf-8")
    raise InternalFailure(f"the ERP SQL file {name!r} is missing from this installation")


def sql_sources() -> tuple[str, str]:
    """The schema and the seed, as text. Exposed so a test can compare the two copies."""
    return _read_sql("schema.sql"), _read_sql("seed.sql")


#: Bumped only if the schema ever changes. `PRAGMA user_version` is 0 on a database SQLite has just
#: created, which is how `Erp.open` tells a first open from every one after it, without a table of
#: its own to keep that fact in.
SCHEMA_VERSION: Final = 1

# ------------------------------------------------------------------------------------------------
# Value types
# ------------------------------------------------------------------------------------------------


class InvoiceState(StrEnum):
    RECEIVED = "received"
    EXTRACTED = "extracted"
    MATCHED = "matched"
    STAGED = "staged"
    POSTED = "posted"
    HELD = "held"


class CaseState(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    CREDIT_PROPOSED = "credit_proposed"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


#: The spec's case chain. A case moves forward along it, or stays where it is; it never goes back.
#: The *steps* are slice 18.6's state machine -- this order is only the floor under it, so that no
#: slice can move a resolved case back to open.
_CASE_ORDER: Final[tuple[CaseState, ...]] = (
    CaseState.OPEN,
    CaseState.INVESTIGATING,
    CaseState.CREDIT_PROPOSED,
    CaseState.ESCALATED,
    CaseState.RESOLVED,
)


class ChangeKind(StrEnum):
    """The three staged-change tables. The value is the table name."""

    POSTING = "posting"
    CREDIT_MEMO = "credit_memo"
    PAYMENT_HOLD = "payment_hold"


#: The opaque key prefix per kind. One token names one change, with no `kind` argument to get wrong.
KEY_PREFIX: Final[Mapping[ChangeKind, str]] = {
    ChangeKind.POSTING: "pst_",
    ChangeKind.CREDIT_MEMO: "crd_",
    ChangeKind.PAYMENT_HOLD: "hld_",
}
_KIND_BY_PREFIX: Final[Mapping[str, ChangeKind]] = {v: k for k, v in KEY_PREFIX.items()}
#: A well-formed key: a known prefix and six digits. Checked before any lookup, so a typo is a
#: `validation` failure and only a well-formed key can produce a `permission` one.
_KEY_PATTERN: Final = re.compile(r"\A(pst_|crd_|hld_)[0-9]{6}\Z")


class DecisionKind(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Decision:
    """An approver's verdict on a staged change, and the reason when it is a rejection.

    The schema keeps this in one column, because the spec puts a rejection's reason on
    `posting.decision`. The encoding is confined to this class, so no caller reads the string.
    """

    kind: DecisionKind
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind is DecisionKind.REJECTED and not self.reason:
            raise ValueError("a rejection needs a reason")
        if self.kind is DecisionKind.APPLIED and self.reason is not None:
            raise ValueError("an applied decision carries no reason")

    @classmethod
    def applied(cls) -> Decision:
        return cls(DecisionKind.APPLIED)

    @classmethod
    def rejected(cls, reason: str) -> Decision:
        text = reason.strip()
        if not text:
            raise ValidationFailed("a rejection needs a reason")
        if "\x00" in text:
            raise ValidationFailed("a rejection reason may not contain a NUL byte")
        return cls(DecisionKind.REJECTED, text[:500])

    def encode(self) -> str:
        if self.kind is DecisionKind.APPLIED:
            return DecisionKind.APPLIED.value
        return f"{DecisionKind.REJECTED.value}: {self.reason}"

    @classmethod
    def decode(cls, stored: str) -> Decision:
        if stored == DecisionKind.APPLIED.value:
            return cls.applied()
        marker = f"{DecisionKind.REJECTED.value}: "
        if stored.startswith(marker):
            return cls(DecisionKind.REJECTED, stored[len(marker) :])
        raise InternalFailure("a stored decision is not in the recorded format")


@dataclass(frozen=True, slots=True)
class Supplier:
    id: str
    company_code: str
    name: str
    terms: str
    risk_flag: bool


@dataclass(frozen=True, slots=True)
class PurchaseOrderLine:
    line_no: int
    description: str
    qty: int
    unit_price: int


@dataclass(frozen=True, slots=True)
class PurchaseOrder:
    id: str
    company_code: str
    supplier_id: str
    currency: str
    total: int
    lines: tuple[PurchaseOrderLine, ...]


@dataclass(frozen=True, slots=True)
class GoodsReceipt:
    id: str
    company_code: str
    po_id: str
    line_no: int
    qty_received: int
    received_at: str


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    line_no: int
    description: str
    qty: int
    unit_price: int
    tax: int


@dataclass(frozen=True, slots=True)
class Invoice:
    id: str
    company_code: str
    supplier_id: str
    po_ref: str | None
    number: str
    date: str
    currency: str
    total: int
    state: InvoiceState
    lines: tuple[InvoiceLine, ...]


@dataclass(frozen=True, slots=True)
class Extraction:
    id: int
    company_code: str
    invoice_id: str
    attempts: int
    repaired: bool
    confidence: float | None
    fields: Mapping[str, object]
    model_task: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class StagedChangeRow:
    """One row of one of the three staged-change tables, in a single shape.

    `p2p staged`, `p2p review` and `apply_change` are generic over the three kinds, so one type for
    all three is what keeps them from growing a `kind` switch each. The fields that belong to only
    one kind are optional and documented by `kind`.
    """

    change_key: str
    kind: ChangeKind
    company_code: str
    staged_by: str
    staged_at: str
    summary: str
    decided_by: str | None = None
    decision: Decision | None = None
    applied_at: str | None = None
    invoice_id: str | None = None
    supplier_id: str | None = None
    case_id: str | None = None
    reason: str | None = None
    amount: int | None = None
    currency: str | None = None
    payload: Mapping[str, object] | None = None

    @property
    def is_decided(self) -> bool:
        return self.decision is not None


@dataclass(frozen=True, slots=True)
class DisputeCase:
    id: str
    company_code: str
    supplier_id: str
    invoice_id: str | None
    state: CaseState
    opened_by: str
    assigned_to: str
    escalated_reason: str | None
    opened_at: str
    summary: str | None


@dataclass(frozen=True, slots=True)
class CaseEvent:
    case_id: str
    seq: int
    actor: str
    kind: str
    payload: Mapping[str, object]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class BatchRun:
    id: str
    company_code: str
    started_by: str
    started_at: str
    items: int
    ok: int
    failed: int
    skipped: int
    summary: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PaymentStatus:
    """What `get_payment_status` reports: the invoice's state plus what has been applied to it."""

    invoice_id: str
    state: InvoiceState
    currency: str
    total: int
    applied_posting: str | None
    applied_holds: tuple[str, ...]
    applied_credit_total: int


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """The result of `apply_change`, including the case where the change was already decided.

    A repeated apply is not an error: the spec makes it the stated no-op the apply-once guard
    produces. `already_decided` is how a caller tells the two apart without reading `detail`.
    """

    change_key: str
    kind: ChangeKind
    decision: Decision
    already_decided: bool
    detail: str
    invoice_state: InvoiceState | None = None


# ------------------------------------------------------------------------------------------------
# The invoice transition table
# ------------------------------------------------------------------------------------------------
#
# The spec states it as a set, because two of the moves are not sequential. Split three ways by what
# causes each move, so that holding one accessor does not hand a caller the others:
#
#   * `_CALLER_TRANSITIONS` -- what `advance_invoice` accepts: the two a clerk's own work causes.
#   * `_STAGING_TRANSITION` -- made inside `stage_posting`, never on its own. Its stated cause is "a
#     posting staged", so the row and the state cannot come apart; a public accessor for it would
#     let a clerk mark an invoice `staged` with no posting to approve.
#   * `_APPROVAL_TRANSITIONS` -- made inside `apply_change`, by a second principal. A public
#     accessor for these would be a second apply path with no separation of duties, which is exactly
#     the duplication the spec forbids.

_CALLER_TRANSITIONS: Final[frozenset[tuple[InvoiceState, InvoiceState]]] = frozenset(
    {
        (InvoiceState.RECEIVED, InvoiceState.EXTRACTED),  # 18.4, a successful extraction
        (InvoiceState.EXTRACTED, InvoiceState.MATCHED),  # 18.5, the four-step match
    }
)
_STAGING_TRANSITION: Final = (InvoiceState.MATCHED, InvoiceState.STAGED)
_APPROVAL_TRANSITIONS: Final[frozenset[tuple[InvoiceState, InvoiceState]]] = frozenset(
    {
        (InvoiceState.STAGED, InvoiceState.POSTED),  # an approver applies the staged posting
        # an approver rejects it; the invoice returns to `matched` so it can be staged again
        (InvoiceState.STAGED, InvoiceState.MATCHED),
        (InvoiceState.POSTED, InvoiceState.HELD),  # an approver applies a staged hold
    }
)
#: The whole table. `held` is terminal: no member leaves it, and no slice may add one.
TRANSITIONS: Final[frozenset[tuple[InvoiceState, InvoiceState]]] = (
    _CALLER_TRANSITIONS | _APPROVAL_TRANSITIONS | {_STAGING_TRANSITION}
)

#: Reads are capped and paged (statement 2.1). A caller may ask for less, never for more.
MAX_PAGE: Final = 100
DEFAULT_PAGE: Final = 50
#: The spec's context window for a case: the rolling summary plus the last 10 events.
DEFAULT_CASE_EVENTS: Final = 10


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------------
# Row readers: sqlite3.Row values are untyped, and mypy --strict will not take them on trust
# ------------------------------------------------------------------------------------------------


#: The only columns `assert_can` reads. A `sqlite3.Row` is not a `Mapping` -- it has no `.get()`,
#: and indexing a column it does not carry raises -- so every row handed to the access check goes
#: through `_owner` first. That narrowing is the contract, not a workaround: the check is entitled
#: to the owner columns and to nothing else.
_OWNER_COLUMNS: Final = ("company_code", "staged_by", "assigned_to")


def _owner(row: sqlite3.Row) -> dict[str, object]:
    present = set(row.keys())
    return {column: row[column] for column in _OWNER_COLUMNS if column in present}


def _text(row: sqlite3.Row, column: str) -> str:
    value = row[column]
    if not isinstance(value, str):
        raise InternalFailure(f"column {column!r} should hold text")
    return value


def _opt_text(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, str):
        raise InternalFailure(f"column {column!r} should hold text or nothing")
    return value


def _number(row: sqlite3.Row, column: str) -> int:
    value = row[column]
    if not isinstance(value, int):
        raise InternalFailure(f"column {column!r} should hold an integer")
    return value


def _opt_number(row: sqlite3.Row, column: str) -> int | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, int):
        raise InternalFailure(f"column {column!r} should hold an integer or nothing")
    return value


def _opt_real(row: sqlite3.Row, column: str) -> float | None:
    value = row[column]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InternalFailure(f"column {column!r} should hold a number or nothing")
    return float(value)


def _object_json(row: sqlite3.Row, column: str) -> Mapping[str, object]:
    parsed = json.loads(_text(row, column))
    if not isinstance(parsed, dict):
        raise InternalFailure(f"column {column!r} should hold a JSON object")
    return parsed


def _array_json(row: sqlite3.Row, column: str) -> list[object]:
    parsed = json.loads(_text(row, column))
    if not isinstance(parsed, list):
        raise InternalFailure(f"column {column!r} should hold a JSON array")
    return parsed


def _dump_object(value: Mapping[str, object], what: str) -> str:
    """Serialize a caller's payload, refusing what the database cannot hold."""
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"the {what} payload is not JSON-serializable") from exc
    if "\\u0000" in text:
        # Postgres cannot store a NUL byte and SQLite will, silently; either way a NUL in an audit
        # row is corrupt data. Refused at the boundary, as the track's rules require.
        raise ValidationFailed(f"the {what} payload may not contain a NUL byte")
    return text


def _clean_text(value: str, field: str, *, cap: int) -> str:
    text = value.strip()
    if not text:
        raise ValidationFailed(f"{field} may not be empty")
    if "\x00" in text:
        raise ValidationFailed(f"{field} may not contain a NUL byte")
    return text[:cap]


def _page(limit: int | None, offset: int) -> tuple[int, int]:
    if offset < 0:
        raise ValidationFailed("offset may not be negative")
    if limit is None:
        return DEFAULT_PAGE, offset
    if limit < 1:
        raise ValidationFailed("limit must be at least 1")
    return min(limit, MAX_PAGE), offset


# ------------------------------------------------------------------------------------------------
# Per-kind SQL. Written out per table rather than interpolated: a table name spliced into a
# statement is how an injection gets in, and ruff's S608 is right to object to the shortcut.
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StagedSql:
    by_key: str
    undecided: str
    decide: str
    next_seq: str
    insert: str


_STAGED_SQL: Final[Mapping[ChangeKind, _StagedSql]] = {
    ChangeKind.POSTING: _StagedSql(
        by_key="SELECT * FROM posting WHERE id = ?",
        undecided="SELECT * FROM posting WHERE company_code = ? AND decision IS NULL ORDER BY seq",
        decide="UPDATE posting SET decision = ?, decided_by = ?, applied_at = ? WHERE id = ?",
        next_seq="SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM posting",
        insert=(
            "INSERT INTO posting (seq, id, company_code, invoice_id, payload_json, staged_by,"
            " staged_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        ),
    ),
    ChangeKind.CREDIT_MEMO: _StagedSql(
        by_key="SELECT * FROM credit_memo WHERE id = ?",
        undecided=(
            "SELECT * FROM credit_memo WHERE company_code = ? AND decision IS NULL ORDER BY seq"
        ),
        decide="UPDATE credit_memo SET decision = ?, decided_by = ?, applied_at = ? WHERE id = ?",
        next_seq="SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM credit_memo",
        insert=(
            "INSERT INTO credit_memo (seq, id, company_code, supplier_id, invoice_id, amount,"
            " currency, reason, staged_by, staged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ),
    ),
    ChangeKind.PAYMENT_HOLD: _StagedSql(
        by_key="SELECT * FROM payment_hold WHERE id = ?",
        undecided=(
            "SELECT * FROM payment_hold WHERE company_code = ? AND decision IS NULL ORDER BY seq"
        ),
        decide="UPDATE payment_hold SET decision = ?, decided_by = ?, applied_at = ? WHERE id = ?",
        next_seq="SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM payment_hold",
        insert=(
            "INSERT INTO payment_hold (seq, id, company_code, invoice_id, case_id, reason,"
            " staged_by, staged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ),
    ),
}


def _money(amount: int, currency: str) -> str:
    """Minor units as a readable amount, for a staged change's one-line summary."""
    sign = "-" if amount < 0 else ""
    whole, minor = divmod(abs(amount), 100)
    return f"{sign}{whole:,}.{minor:02d} {currency}"


# ------------------------------------------------------------------------------------------------
# The ERP
# ------------------------------------------------------------------------------------------------


class Erp:
    """A connection to one simulated ERP database, and every accessor over it.

    Open it with `Erp.open(path)`. The first open of a path applies `erp/schema.sql` and
    `erp/seed.sql` in one transaction and records `PRAGMA user_version`; every open after that
    leaves the file alone for ever, which is what makes a person's own database persist across
    commands -- and therefore what makes staging and approving two separate acts.

    Nothing here deletes a database, truncates a table or re-seeds one. `p2p demo` and
    `scripts/smoke.sh` (slice 18.8) get a fresh file in a `mkdtemp()` directory of their own
    instead, because the path is a parameter: a demo that rebuilt the configured path would, one
    day, rebuild somebody's real database.
    """

    def __init__(self, connection: sqlite3.Connection, *, clock: Clock = _utc_now) -> None:
        self._conn = connection
        self._clock = clock

    # -- lifecycle ------------------------------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, clock: Clock = _utc_now) -> Erp:
        target = Path(path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        # `isolation_level=None` turns off the driver's implicit transactions, so the only
        # transactions are the explicit `BEGIN IMMEDIATE` blocks below. Without that, a rollback
        # after the apply-once trigger fires can leave the driver holding a transaction nobody
        # opened.
        connection = sqlite3.connect(target, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # Connection-scoped, so it cannot live in schema.sql: without it the schema's REFERENCES
        # clauses are documentation rather than constraints.
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        erp = cls(connection, clock=clock)
        erp._initialize()
        return erp

    def _initialize(self) -> None:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        version = _number(row, "user_version") if row is not None else 0
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise InternalFailure(
                "this database was built by a different schema version of the simulated ERP"
            )
        schema, seed = sql_sources()
        # The transaction is written INSIDE the script, not around it: `executescript` commits any
        # transaction already open before it runs, so a `BEGIN` issued from Python here would be
        # thrown away and a failure halfway through the seed would leave a half-built database that
        # the `user_version` check would then take for a finished one. `PRAGMA user_version` lives
        # in the file header and is transactional, so it lands or does not land with the rest.
        # The version is a module constant, not caller input: PRAGMA takes no parameter.
        script = "\n".join(
            (
                "BEGIN IMMEDIATE;",
                schema,
                seed,
                f"PRAGMA user_version = {SCHEMA_VERSION:d};",
                "COMMIT;",
            )
        )
        try:
            self._conn.executescript(script)
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Erp:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _now(self) -> str:
        return _stamp(self._clock())

    # -- reads ----------------------------------------------------------------------------------

    def find_supplier(
        self, principal: Principal, name: str, *, limit: int | None = None, offset: int = 0
    ) -> tuple[Supplier, ...]:
        """Suppliers in the principal's company code matching `name`, capped and paged."""
        assert_can(principal, Action.READ_SUPPLIER)
        needle = _clean_text(name, "name", cap=200)
        size, skip = _page(limit, offset)
        rows = self._conn.execute(
            "SELECT * FROM supplier WHERE company_code = ? AND name LIKE ? ESCAPE '\\'"
            " ORDER BY name LIMIT ? OFFSET ?",
            (principal.company_code, f"%{_like_escape(needle)}%", size, skip),
        ).fetchall()
        return tuple(self._supplier(principal, row) for row in rows)

    def get_supplier(self, principal: Principal, supplier_id: str) -> Supplier:
        row = self._one("SELECT * FROM supplier WHERE id = ?", supplier_id, "supplier")
        return self._supplier(principal, row)

    def _supplier(self, principal: Principal, row: sqlite3.Row) -> Supplier:
        assert_can(principal, Action.READ_SUPPLIER, _owner(row))
        return Supplier(
            id=_text(row, "id"),
            company_code=_text(row, "company_code"),
            name=_text(row, "name"),
            terms=_text(row, "terms"),
            risk_flag=bool(_number(row, "risk_flag")),
        )

    def get_purchase_order(self, principal: Principal, po_id: str) -> PurchaseOrder:
        row = self._one("SELECT * FROM purchase_order WHERE id = ?", po_id, "purchase order")
        assert_can(principal, Action.READ_PURCHASE_ORDER, _owner(row))
        lines = []
        for raw in _array_json(row, "lines"):
            if not isinstance(raw, dict):
                raise InternalFailure("a purchase order line should be a JSON object")
            lines.append(
                PurchaseOrderLine(
                    line_no=_int_field(raw, "line_no"),
                    description=_str_field(raw, "description"),
                    qty=_int_field(raw, "qty"),
                    unit_price=_int_field(raw, "unit_price"),
                )
            )
        return PurchaseOrder(
            id=_text(row, "id"),
            company_code=_text(row, "company_code"),
            supplier_id=_text(row, "supplier_id"),
            currency=_text(row, "currency"),
            total=_number(row, "total"),
            lines=tuple(sorted(lines, key=lambda line: line.line_no)),
        )

    def get_goods_receipt(
        self, principal: Principal, po_id: str, line_no: int | None = None
    ) -> tuple[GoodsReceipt, ...]:
        """The receipts recorded against a purchase order, or against one of its lines."""
        assert_can(principal, Action.READ_GOODS_RECEIPT)
        if line_no is None:
            rows = self._conn.execute(
                "SELECT * FROM goods_receipt WHERE company_code = ? AND po_id = ? ORDER BY line_no",
                (principal.company_code, po_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM goods_receipt WHERE company_code = ? AND po_id = ? AND line_no = ?",
                (principal.company_code, po_id, line_no),
            ).fetchall()
        receipts = []
        for row in rows:
            assert_can(principal, Action.READ_GOODS_RECEIPT, _owner(row))
            receipts.append(
                GoodsReceipt(
                    id=_text(row, "id"),
                    company_code=_text(row, "company_code"),
                    po_id=_text(row, "po_id"),
                    line_no=_number(row, "line_no"),
                    qty_received=_number(row, "qty_received"),
                    received_at=_text(row, "received_at"),
                )
            )
        return tuple(receipts)

    def get_invoice(self, principal: Principal, invoice_id: str) -> Invoice:
        row = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
        return self._invoice(principal, row)

    def list_open_invoices(
        self, principal: Principal, *, limit: int | None = None, offset: int = 0
    ) -> tuple[Invoice, ...]:
        """Invoices in the principal's company code that are not yet posted or held."""
        assert_can(principal, Action.READ_OPEN_INVOICES)
        size, skip = _page(limit, offset)
        rows = self._conn.execute(
            "SELECT * FROM invoice WHERE company_code = ? AND state NOT IN ('posted', 'held')"
            " ORDER BY id LIMIT ? OFFSET ?",
            (principal.company_code, size, skip),
        ).fetchall()
        return tuple(
            self._invoice(principal, row, action=Action.READ_OPEN_INVOICES) for row in rows
        )

    def _invoice(
        self, principal: Principal, row: sqlite3.Row, *, action: Action = Action.READ_INVOICE
    ) -> Invoice:
        assert_can(principal, action, _owner(row))
        invoice_id = _text(row, "id")
        lines = self._conn.execute(
            "SELECT * FROM invoice_line WHERE invoice_id = ? ORDER BY line_no", (invoice_id,)
        ).fetchall()
        return Invoice(
            id=invoice_id,
            company_code=_text(row, "company_code"),
            supplier_id=_text(row, "supplier_id"),
            po_ref=_opt_text(row, "po_ref"),
            number=_text(row, "number"),
            date=_text(row, "date"),
            currency=_text(row, "currency"),
            total=_number(row, "total"),
            state=InvoiceState(_text(row, "state")),
            lines=tuple(
                InvoiceLine(
                    line_no=_number(line, "line_no"),
                    description=_text(line, "description"),
                    qty=_number(line, "qty"),
                    unit_price=_number(line, "unit_price"),
                    tax=_number(line, "tax"),
                )
                for line in lines
            ),
        )

    def get_payment_status(self, principal: Principal, invoice_id: str) -> PaymentStatus:
        """The invoice's state plus what has actually been applied to it.

        `held` is terminal in this build: there is no `release_payment` tool and no transition out
        of it, so a held invoice reports `held` for ever.
        """
        row = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
        assert_can(principal, Action.READ_PAYMENT_STATUS, _owner(row))
        applied_posting = self._conn.execute(
            "SELECT id FROM posting WHERE invoice_id = ? AND decision = ? ORDER BY seq LIMIT 1",
            (invoice_id, DecisionKind.APPLIED.value),
        ).fetchone()
        holds = self._conn.execute(
            "SELECT id FROM payment_hold WHERE invoice_id = ? AND decision = ? ORDER BY seq",
            (invoice_id, DecisionKind.APPLIED.value),
        ).fetchall()
        credits = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM credit_memo"
            " WHERE invoice_id = ? AND decision = ?",
            (invoice_id, DecisionKind.APPLIED.value),
        ).fetchone()
        return PaymentStatus(
            invoice_id=invoice_id,
            state=InvoiceState(_text(row, "state")),
            currency=_text(row, "currency"),
            total=_number(row, "total"),
            applied_posting=None if applied_posting is None else _text(applied_posting, "id"),
            applied_holds=tuple(_text(hold, "id") for hold in holds),
            applied_credit_total=0 if credits is None else _number(credits, "total"),
        )

    def list_extractions(self, principal: Principal, invoice_id: str) -> tuple[Extraction, ...]:
        """The append-only extraction trail for one invoice (statement 4.4)."""
        assert_can(principal, Action.READ_EXTRACTION)
        rows = self._conn.execute(
            "SELECT * FROM extraction WHERE company_code = ? AND invoice_id = ? ORDER BY id",
            (principal.company_code, invoice_id),
        ).fetchall()
        trail = []
        for row in rows:
            assert_can(principal, Action.READ_EXTRACTION, _owner(row))
            trail.append(
                Extraction(
                    id=_number(row, "id"),
                    company_code=_text(row, "company_code"),
                    invoice_id=_text(row, "invoice_id"),
                    attempts=_number(row, "attempts"),
                    repaired=bool(_number(row, "repaired")),
                    confidence=_opt_real(row, "confidence"),
                    fields=_object_json(row, "fields_json"),
                    model_task=_text(row, "model_task"),
                    recorded_at=_text(row, "recorded_at"),
                )
            )
        return tuple(trail)

    def list_staged(self, principal: Principal) -> tuple[StagedChangeRow, ...]:
        """The undecided staged changes this principal may **see**, not what it may decide.

        This is the visibility rule the spec gives to 18.2 rather than to the `p2p staged` command,
        so that it is an accessor and not a filter in a caller:

        * a `clerk` or `disputes` principal sees the changes **it staged**, to follow what it has
          handed on;
        * an `approver` sees every undecided change in its company code **minus any it staged
          itself**, which is separation of duties applied to a list: a change it could not decide
          has no business on the list it decides from;
        * a `service` principal sees none, and is refused: its reads are the gateway's six, and
          `p2p staged` is not one of them.
        """
        assert_can(principal, Action.READ_STAGED)
        found: list[StagedChangeRow] = []
        for kind, sql in _STAGED_SQL.items():
            for row in self._conn.execute(sql.undecided, (principal.company_code,)).fetchall():
                staged_by = _text(row, "staged_by")
                mine = staged_by == principal.subject
                if principal.role is Role.APPROVER and mine:
                    continue
                if principal.role is not Role.APPROVER and not mine:
                    continue
                found.append(self._staged_row(kind, row))
        return tuple(sorted(found, key=lambda change: (change.staged_at, change.change_key)))

    def get_staged(self, principal: Principal, change_key: str) -> StagedChangeRow:
        """One staged change by its key, for `p2p review`. Visibility as in `list_staged`."""
        assert_can(principal, Action.READ_STAGED)
        kind, row = self._resolve_change(change_key)
        change = self._staged_row(kind, row)
        if change.company_code != principal.company_code:
            raise _not_visible()
        if principal.role is Role.APPROVER:
            if change.staged_by == principal.subject:
                raise AccessDenied(
                    "separation of duties: an approver does not review a change it staged"
                )
        elif change.staged_by != principal.subject:
            raise _not_visible()
        return change

    def get_case(self, principal: Principal, case_id: str) -> DisputeCase:
        row = self._one("SELECT * FROM dispute_case WHERE id = ?", case_id, "dispute case")
        assert_can(principal, Action.READ_CASE, _owner(row))
        return _case(row)

    def list_cases(
        self, principal: Principal, *, limit: int | None = None, offset: int = 0
    ) -> tuple[DisputeCase, ...]:
        """Cases the principal may see: its own for `disputes`, all of them for an approver."""
        assert_can(principal, Action.READ_CASE)
        size, skip = _page(limit, offset)
        rows = self._conn.execute(
            "SELECT * FROM dispute_case WHERE company_code = ? ORDER BY id LIMIT ? OFFSET ?",
            (principal.company_code, size, skip),
        ).fetchall()
        visible = []
        for row in rows:
            if principal.role is Role.DISPUTES and _text(row, "assigned_to") != principal.subject:
                continue
            assert_can(principal, Action.READ_CASE, _owner(row))
            visible.append(_case(row))
        return tuple(visible)

    def list_case_events(
        self, principal: Principal, case_id: str, *, limit: int | None = None
    ) -> tuple[CaseEvent, ...]:
        """The last `limit` events of a case, oldest first. Default 10, the spec's window (5.1)."""
        case_row = self._one("SELECT * FROM dispute_case WHERE id = ?", case_id, "dispute case")
        assert_can(principal, Action.READ_CASE, _owner(case_row))
        size = DEFAULT_CASE_EVENTS if limit is None else min(max(limit, 1), MAX_PAGE)
        rows = self._conn.execute(
            "SELECT * FROM case_event WHERE case_id = ? ORDER BY seq DESC LIMIT ?",
            (case_id, size),
        ).fetchall()
        return tuple(
            CaseEvent(
                case_id=_text(row, "case_id"),
                seq=_number(row, "seq"),
                actor=_text(row, "actor"),
                kind=_text(row, "kind"),
                payload=_object_json(row, "payload_json"),
                recorded_at=_text(row, "recorded_at"),
            )
            for row in reversed(rows)
        )

    def get_batch_run(self, principal: Principal, run_id: str) -> BatchRun:
        """One batch run. `started_by` is an owner column, so a clerk sees only its own runs.

        An approver sees every run in its company code, as it sees everything else there.
        """
        row = self._one("SELECT * FROM batch_run WHERE id = ?", run_id, "batch run")
        assert_can(principal, Action.READ_BATCH_RUN, _owner(row))
        if principal.role is Role.CLERK and _text(row, "started_by") != principal.subject:
            raise AccessDenied("no batch run visible to this principal")
        return BatchRun(
            id=_text(row, "id"),
            company_code=_text(row, "company_code"),
            started_by=_text(row, "started_by"),
            started_at=_text(row, "started_at"),
            items=_number(row, "items"),
            ok=_number(row, "ok"),
            failed=_number(row, "failed"),
            skipped=_number(row, "skipped"),
            summary=_object_json(row, "summary_json"),
        )

    # -- writes ---------------------------------------------------------------------------------

    def record_extraction(
        self,
        principal: Principal,
        invoice_id: str,
        *,
        attempts: int,
        repaired: bool,
        confidence: float | None,
        fields: Mapping[str, object],
        model_task: str,
    ) -> Extraction:
        """Append one row to the extraction trail (slice 18.4).

        It records only. It does **not** move the invoice: the spec is explicit that a re-ingest of
        an already-advanced invoice records an extraction and writes no state, so the two cannot be
        the same call. 18.4 calls `advance_invoice` itself when the extraction succeeded and the
        invoice was still `received`.
        """
        row = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
        assert_can(principal, Action.RECORD_EXTRACTION, _owner(row))
        if attempts not in (1, 2):
            raise ValidationFailed("attempts must be 1 or 2: one attempt plus at most one repair")
        if repaired and attempts != 2:
            raise ValidationFailed("a repaired extraction took two attempts")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValidationFailed("confidence must be between 0 and 1")
        payload = _dump_object(fields, "extraction")
        task = _clean_text(model_task, "model_task", cap=64)
        recorded_at = self._now()
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO extraction (company_code, invoice_id, attempts, repaired, confidence,"
                " fields_json, model_task, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    principal.company_code,
                    invoice_id,
                    attempts,
                    int(repaired),
                    confidence,
                    payload,
                    task,
                    recorded_at,
                ),
            )
            new_id = cursor.lastrowid
        if new_id is None:
            raise InternalFailure("the extraction row was not recorded")
        return Extraction(
            id=new_id,
            company_code=principal.company_code,
            invoice_id=invoice_id,
            attempts=attempts,
            repaired=repaired,
            confidence=confidence,
            fields=dict(fields),
            model_task=task,
            recorded_at=recorded_at,
        )

    def advance_invoice(
        self, principal: Principal, invoice_id: str, to_state: InvoiceState
    ) -> Invoice:
        """Move an invoice to `to_state`, accepting only a row of the spec's transition table.

        Two moves are reachable here -- `received -> extracted` and `extracted -> matched` -- plus
        the table's "any -> itself", which is a no-op. The other four are caused by staging a
        posting or by an approver's decision, and are made inside `stage_posting` and
        `apply_change`; asking for one here is refused with a reason that says which path makes it.
        """
        row = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
        assert_can(principal, Action.ADVANCE_INVOICE, _owner(row))
        current = InvoiceState(_text(row, "state"))
        if current is to_state:
            return self._invoice(principal, row)
        self._check_caller_transition(current, to_state)
        with self._write() as conn:
            conn.execute("UPDATE invoice SET state = ? WHERE id = ?", (to_state, invoice_id))
        return self.get_invoice(principal, invoice_id)

    @staticmethod
    def _check_caller_transition(current: InvoiceState, to_state: InvoiceState) -> None:
        move = (current, to_state)
        if move in _CALLER_TRANSITIONS:
            return
        if move == _STAGING_TRANSITION:
            raise ValidationFailed(
                f"{current} -> {to_state} is made only by staging a posting, not on its own"
            )
        if move in _APPROVAL_TRANSITIONS:
            raise ValidationFailed(
                f"{current} -> {to_state} is made only by an approver deciding a staged change"
            )
        raise ValidationFailed(f"{current} -> {to_state} is not a transition of this workflow")

    def stage_posting(
        self, principal: Principal, invoice_id: str, payload: Mapping[str, object]
    ) -> StagedChangeRow:
        """Record a posting for approval, and move the invoice `matched -> staged` with it.

        The row and the state change are one transaction because the transition's stated cause is
        "a posting staged": were they two calls, an invoice could sit in `staged` with nothing to
        approve, or a posting could wait against an invoice that still reads `matched`.

        Nothing is posted. An approver applies it later through `apply_change`, and the key this
        returns is the only handle on it once the output of this run is gone.
        """
        row = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
        assert_can(principal, Action.STAGE_POSTING, _owner(row))
        current = InvoiceState(_text(row, "state"))
        if current is not InvoiceState.MATCHED:
            raise ValidationFailed(
                f"a posting is staged against a matched invoice; this one is {current}"
            )
        body = _dump_object(payload, "posting")
        staged_at = self._now()
        sql = _STAGED_SQL[ChangeKind.POSTING]
        with self._write() as conn:
            key, seq = self._next_key(conn, ChangeKind.POSTING)
            conn.execute(
                sql.insert,
                (seq, key, principal.company_code, invoice_id, body, principal.subject, staged_at),
            )
            conn.execute(
                "UPDATE invoice SET state = ? WHERE id = ?", (InvoiceState.STAGED, invoice_id)
            )
        return self._staged_row(ChangeKind.POSTING, self._one(sql.by_key, key, "staged change"))

    def stage_credit_memo(
        self,
        principal: Principal,
        *,
        supplier_id: str,
        amount: int,
        currency: str,
        reason: str,
        invoice_id: str | None = None,
    ) -> StagedChangeRow:
        """Record a credit memo for approval (slice 18.6). Changes no invoice state, ever.

        A credit is a separate document against the supplier, which is why `credit_memo` has no
        state column and why applying one moves nothing.
        """
        supplier = self._one("SELECT * FROM supplier WHERE id = ?", supplier_id, "supplier")
        assert_can(principal, Action.STAGE_CREDIT_MEMO, _owner(supplier))
        if amount <= 0:
            raise ValidationFailed("a credit memo's amount must be positive")
        money = _clean_text(currency, "currency", cap=3).upper()
        why = _clean_text(reason, "reason", cap=500)
        if invoice_id is not None:
            invoice = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
            assert_can(principal, Action.STAGE_CREDIT_MEMO, _owner(invoice))
        staged_at = self._now()
        sql = _STAGED_SQL[ChangeKind.CREDIT_MEMO]
        with self._write() as conn:
            key, seq = self._next_key(conn, ChangeKind.CREDIT_MEMO)
            conn.execute(
                sql.insert,
                (
                    seq,
                    key,
                    principal.company_code,
                    supplier_id,
                    invoice_id,
                    amount,
                    money,
                    why,
                    principal.subject,
                    staged_at,
                ),
            )
        return self._staged_row(ChangeKind.CREDIT_MEMO, self._one(sql.by_key, key, "staged change"))

    def stage_payment_hold(
        self,
        principal: Principal,
        *,
        invoice_id: str,
        reason: str,
        case_id: str | None = None,
    ) -> StagedChangeRow:
        """Record a payment hold for approval (slice 18.6). A hold outside a dispute has no case.

        Applying it is the only path to `invoice.state = held`, and that transition starts at
        `posted`: a hold is a payment-side act on an invoice already in the ledger. Whether staging
        one against an invoice that is not yet posted is refused at stage time is 18.6's rule; what
        is guaranteed here is that applying it cannot move an invoice that is not `posted`.
        """
        row = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
        assert_can(principal, Action.STAGE_PAYMENT_HOLD, _owner(row))
        why = _clean_text(reason, "reason", cap=500)
        if case_id is not None:
            case_row = self._one("SELECT * FROM dispute_case WHERE id = ?", case_id, "dispute case")
            assert_can(principal, Action.STAGE_PAYMENT_HOLD, _owner(case_row))
        staged_at = self._now()
        sql = _STAGED_SQL[ChangeKind.PAYMENT_HOLD]
        with self._write() as conn:
            key, seq = self._next_key(conn, ChangeKind.PAYMENT_HOLD)
            conn.execute(
                sql.insert,
                (
                    seq,
                    key,
                    principal.company_code,
                    invoice_id,
                    case_id,
                    why,
                    principal.subject,
                    staged_at,
                ),
            )
        return self._staged_row(
            ChangeKind.PAYMENT_HOLD, self._one(sql.by_key, key, "staged change")
        )

    def open_case(
        self, principal: Principal, *, supplier_id: str, invoice_id: str | None = None
    ) -> DisputeCase:
        """Open a dispute case, assigned to the principal that opened it (slice 18.6)."""
        supplier = self._one("SELECT * FROM supplier WHERE id = ?", supplier_id, "supplier")
        assert_can(principal, Action.OPEN_CASE, _owner(supplier))
        if invoice_id is not None:
            invoice = self._one("SELECT * FROM invoice WHERE id = ?", invoice_id, "invoice")
            assert_can(principal, Action.OPEN_CASE, _owner(invoice))
        opened_at = self._now()
        with self._write() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM dispute_case").fetchone()
            case_id = f"DC-{_number(row, 'n') + 1:04d}"
            conn.execute(
                "INSERT INTO dispute_case (id, company_code, supplier_id, invoice_id, state,"
                " opened_by, assigned_to, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    principal.company_code,
                    supplier_id,
                    invoice_id,
                    CaseState.OPEN,
                    principal.subject,
                    principal.subject,
                    opened_at,
                ),
            )
        return self.get_case(principal, case_id)

    def advance_case(
        self,
        principal: Principal,
        case_id: str,
        to_state: CaseState,
        *,
        escalated_reason: str | None = None,
    ) -> DisputeCase:
        """Move a case forward along the spec's chain. Never backwards; `resolved` is the end.

        Which step follows which is slice 18.6's state machine. This accessor only refuses to go
        back, so that no slice can reopen a resolved case or un-escalate one.
        """
        row = self._one("SELECT * FROM dispute_case WHERE id = ?", case_id, "dispute case")
        assert_can(principal, Action.ADVANCE_CASE, _owner(row))
        current = CaseState(_text(row, "state"))
        if _CASE_ORDER.index(to_state) < _CASE_ORDER.index(current):
            raise ValidationFailed(f"a case does not move back from {current} to {to_state}")
        if to_state is CaseState.ESCALATED and not escalated_reason:
            raise ValidationFailed("escalating a case needs the criterion it matched")
        why = (
            None
            if escalated_reason is None
            else _clean_text(escalated_reason, "escalated_reason", cap=500)
        )
        with self._write() as conn:
            if why is None:
                conn.execute("UPDATE dispute_case SET state = ? WHERE id = ?", (to_state, case_id))
            else:
                conn.execute(
                    "UPDATE dispute_case SET state = ?, escalated_reason = ? WHERE id = ?",
                    (to_state, why, case_id),
                )
        return self.get_case(principal, case_id)

    def set_case_summary(self, principal: Principal, case_id: str, summary: str) -> DisputeCase:
        """Replace a case's rolling summary (statement 5.1).

        The summary is the half of a case's context that is not the last ten events, and it is
        replaced rather than appended: that is what makes it rolling. The events themselves stay
        append-only.
        """
        row = self._one("SELECT * FROM dispute_case WHERE id = ?", case_id, "dispute case")
        assert_can(principal, Action.ADVANCE_CASE, _owner(row))
        text = _clean_text(summary, "summary", cap=4000)
        with self._write() as conn:
            conn.execute("UPDATE dispute_case SET summary = ? WHERE id = ?", (text, case_id))
        return self.get_case(principal, case_id)

    def append_case_event(
        self, principal: Principal, case_id: str, *, kind: str, payload: Mapping[str, object]
    ) -> CaseEvent:
        """Append one event to a case's transcript. The table refuses updates and deletes."""
        row = self._one("SELECT * FROM dispute_case WHERE id = ?", case_id, "dispute case")
        assert_can(principal, Action.APPEND_CASE_EVENT, _owner(row))
        event_kind = _clean_text(kind, "kind", cap=64)
        body = _dump_object(payload, "case event")
        recorded_at = self._now()
        with self._write() as conn:
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM case_event WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            seq = _number(seq_row, "next")
            conn.execute(
                "INSERT INTO case_event (case_id, seq, actor, kind, payload_json, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, seq, principal.subject, event_kind, body, recorded_at),
            )
        return CaseEvent(
            case_id=case_id,
            seq=seq,
            actor=principal.subject,
            kind=event_kind,
            payload=dict(payload),
            recorded_at=recorded_at,
        )

    def record_batch_run(
        self,
        principal: Principal,
        *,
        items: int,
        ok: int,
        failed: int,
        skipped: int,
        summary: Mapping[str, object],
    ) -> BatchRun:
        """Record one batch run (slice 18.7).

        The totals are checked here and again by the schema: `items = ok + failed + skipped` always,
        because every document found lands in exactly one of the three outcomes -- which is what
        makes a run the budget stopped early still add up.
        """
        assert_can(principal, Action.RECORD_BATCH_RUN)
        for name, value in (("items", items), ("ok", ok), ("failed", failed), ("skipped", skipped)):
            if value < 0:
                raise ValidationFailed(f"{name} may not be negative")
        if items != ok + failed + skipped:
            raise ValidationFailed("items must equal ok + failed + skipped")
        body = _dump_object(summary, "batch summary")
        started_at = self._now()
        with self._write() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM batch_run").fetchone()
            run_id = f"BR-{_number(row, 'n') + 1:06d}"
            conn.execute(
                "INSERT INTO batch_run (id, company_code, started_by, started_at, items, ok,"
                " failed, skipped, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    principal.company_code,
                    principal.subject,
                    started_at,
                    items,
                    ok,
                    failed,
                    skipped,
                    body,
                ),
            )
        return self.get_batch_run(principal, run_id)

    # -- the one apply path ---------------------------------------------------------------------

    def apply_change(
        self, principal: Principal, change_key: str, decision: Decision
    ) -> ApplyOutcome:
        """Apply or reject a staged change of any of the three kinds. The only path that does.

        The key's prefix picks the table, so one token names one change and there is no `kind`
        argument to get wrong. The call enforces separation of duties through `assert_can`, carries
        the apply-once guard, and makes each kind's own state effect:

        * a **posting** applied moves its invoice `staged -> posted`; rejected, `staged -> matched`,
          so it can be staged again;
        * a **payment hold** applied moves its invoice `posted -> held`, the only path to `held`;
        * a **credit memo** moves nothing.

        A second decision on the same change is the stated no-op, not an error: the `*_decided_once`
        trigger in the schema refuses the UPDATE, and this returns the decision already recorded
        with `already_decided` set. The guard is in the database, so a retry cannot slip past it by
        taking a different route -- which is why the UPDATE below deliberately carries no
        "WHERE decision IS NULL".
        """
        kind, row = self._resolve_change(change_key)
        assert_can(principal, Action.DECIDE_CHANGE, _owner(row))
        existing = self._staged_row(kind, row)
        if existing.decision is not None:
            return ApplyOutcome(
                change_key=existing.change_key,
                kind=kind,
                decision=existing.decision,
                already_decided=True,
                detail="this change was already decided; nothing was changed",
                invoice_state=self._state_of(existing.invoice_id),
            )
        sql = _STAGED_SQL[kind]
        decided_at = self._now()
        try:
            with self._write() as conn:
                conn.execute(
                    sql.decide,
                    (decision.encode(), principal.subject, decided_at, existing.change_key),
                )
                self._apply_state_effect(conn, kind, existing, decision)
        except sqlite3.IntegrityError:
            # The apply-once trigger. Another decision landed between the read above and this
            # write; the database refused ours and rolled it back, so report what is recorded.
            settled = self._staged_row(kind, self._one(sql.by_key, existing.change_key, "change"))
            if settled.decision is None:  # pragma: no cover - a constraint we do not know about
                raise
            return ApplyOutcome(
                change_key=settled.change_key,
                kind=kind,
                decision=settled.decision,
                already_decided=True,
                detail="this change was already decided; nothing was changed",
                invoice_state=self._state_of(settled.invoice_id),
            )
        return ApplyOutcome(
            change_key=existing.change_key,
            kind=kind,
            decision=decision,
            already_decided=False,
            detail=f"{decision.kind} {existing.summary}",
            invoice_state=self._state_of(existing.invoice_id),
        )

    def _apply_state_effect(
        self,
        conn: sqlite3.Connection,
        kind: ChangeKind,
        change: StagedChangeRow,
        decision: Decision,
    ) -> None:
        """The invoice move each kind's decision causes, inside the deciding transaction."""
        if kind is ChangeKind.CREDIT_MEMO or change.invoice_id is None:
            return
        if kind is ChangeKind.POSTING:
            target = (
                InvoiceState.POSTED
                if decision.kind is DecisionKind.APPLIED
                else InvoiceState.MATCHED
            )
        elif decision.kind is DecisionKind.APPLIED:
            target = InvoiceState.HELD
        else:
            return  # a rejected hold moves nothing
        row = conn.execute(
            "SELECT state FROM invoice WHERE id = ?", (change.invoice_id,)
        ).fetchone()
        if row is None:
            raise InternalFailure("the invoice this change names is missing")
        current = InvoiceState(_text(row, "state"))
        if current is target:
            return
        if (current, target) not in _APPROVAL_TRANSITIONS:
            raise ValidationFailed(
                f"this change would move its invoice {current} -> {target},"
                " which is not a transition of this workflow"
            )
        conn.execute("UPDATE invoice SET state = ? WHERE id = ?", (target, change.invoice_id))

    def _state_of(self, invoice_id: str | None) -> InvoiceState | None:
        if invoice_id is None:
            return None
        row = self._conn.execute("SELECT state FROM invoice WHERE id = ?", (invoice_id,)).fetchone()
        return None if row is None else InvoiceState(_text(row, "state"))

    # -- internals ------------------------------------------------------------------------------

    def _one(self, sql: str, key: str | int, what: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self._conn.execute(sql, (key,)).fetchone()
        if row is None:
            # One message and one category whether the row is absent or in another company code: a
            # different answer for each would tell a caller that somebody else's row exists. The
            # spec's eval wants `permission` and no row data for a cross-company read, and this is
            # what gives it that without leaking the easy case.
            raise AccessDenied(f"no {what} visible to this principal")
        return row

    def _resolve_change(self, change_key: str) -> tuple[ChangeKind, sqlite3.Row]:
        if not _KEY_PATTERN.match(change_key):
            # Shape only -- no value echoed. A malformed key is the caller's own typo, and saying
            # which prefixes exist is a schema fact, not somebody's data.
            raise ValidationFailed(
                "a change key is 'pst_', 'crd_' or 'hld_' followed by six digits"
            )
        kind = _KIND_BY_PREFIX[change_key[:4]]
        return kind, self._one(_STAGED_SQL[kind].by_key, change_key, "staged change")

    def _next_key(self, conn: sqlite3.Connection, kind: ChangeKind) -> tuple[str, int]:
        row = conn.execute(_STAGED_SQL[kind].next_seq).fetchone()
        seq = _number(row, "next")
        if seq > 999_999:  # pragma: no cover - a demo database does not reach a million changes
            raise InternalFailure("this table has run out of change keys")
        return f"{KEY_PREFIX[kind]}{seq:06d}", seq

    def _staged_row(self, kind: ChangeKind, row: sqlite3.Row) -> StagedChangeRow:
        """One row of one staged-change table, in the single shape all three are read through."""
        stored = _opt_text(row, "decision")
        change_key = _text(row, "id")
        company_code = _text(row, "company_code")
        staged_by = _text(row, "staged_by")
        staged_at = _text(row, "staged_at")
        decided_by = _opt_text(row, "decided_by")
        decision = None if stored is None else Decision.decode(stored)
        applied_at = _opt_text(row, "applied_at")

        if kind is ChangeKind.POSTING:
            invoice_id = _text(row, "invoice_id")
            return StagedChangeRow(
                change_key=change_key,
                kind=kind,
                company_code=company_code,
                staged_by=staged_by,
                staged_at=staged_at,
                summary=f"post invoice {invoice_id}",
                decided_by=decided_by,
                decision=decision,
                applied_at=applied_at,
                invoice_id=invoice_id,
                payload=_object_json(row, "payload_json"),
            )
        if kind is ChangeKind.CREDIT_MEMO:
            amount = _number(row, "amount")
            currency = _text(row, "currency")
            supplier_id = _text(row, "supplier_id")
            return StagedChangeRow(
                change_key=change_key,
                kind=kind,
                company_code=company_code,
                staged_by=staged_by,
                staged_at=staged_at,
                summary=f"credit {_money(amount, currency)} to supplier {supplier_id}",
                decided_by=decided_by,
                decision=decision,
                applied_at=applied_at,
                invoice_id=_opt_text(row, "invoice_id"),
                supplier_id=supplier_id,
                reason=_text(row, "reason"),
                amount=amount,
                currency=currency,
            )
        held_invoice = _text(row, "invoice_id")
        return StagedChangeRow(
            change_key=change_key,
            kind=kind,
            company_code=company_code,
            staged_by=staged_by,
            staged_at=staged_at,
            summary=f"hold payment on invoice {held_invoice}",
            decided_by=decided_by,
            decision=decision,
            applied_at=applied_at,
            invoice_id=held_invoice,
            case_id=_opt_text(row, "case_id"),
            reason=_text(row, "reason"),
        )


def _case(row: sqlite3.Row) -> DisputeCase:
    return DisputeCase(
        id=_text(row, "id"),
        company_code=_text(row, "company_code"),
        supplier_id=_text(row, "supplier_id"),
        invoice_id=_opt_text(row, "invoice_id"),
        state=CaseState(_text(row, "state")),
        opened_by=_text(row, "opened_by"),
        assigned_to=_text(row, "assigned_to"),
        escalated_reason=_opt_text(row, "escalated_reason"),
        opened_at=_text(row, "opened_at"),
        summary=_opt_text(row, "summary"),
    )


def _not_visible() -> AccessDenied:
    return AccessDenied("no staged change visible to this principal")


def _like_escape(value: str) -> str:
    """Escape LIKE's wildcards so a search for '%' finds a literal '%'."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _int_field(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InternalFailure(f"a purchase order line's {key!r} should be an integer")
    return value


def _str_field(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise InternalFailure(f"a purchase order line's {key!r} should be text")
    return value


__all__: Sequence[str] = (
    "DEFAULT_CASE_EVENTS",
    "KEY_PREFIX",
    "MAX_PAGE",
    "SCHEMA_VERSION",
    "TRANSITIONS",
    "ApplyOutcome",
    "BatchRun",
    "CaseEvent",
    "CaseState",
    "ChangeKind",
    "Decision",
    "DecisionKind",
    "DisputeCase",
    "Erp",
    "Extraction",
    "GoodsReceipt",
    "Invoice",
    "InvoiceLine",
    "InvoiceState",
    "PaymentStatus",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "StagedChangeRow",
    "Supplier",
    "packaged_sql_path",
    "repo_sql_path",
    "sql_sources",
)
