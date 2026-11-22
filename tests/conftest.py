"""Fixtures: one freshly built ERP per test, and the four principals of the role table."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from p2p.core.access import Principal, Role
from p2p.core.erp import Erp

COMPANY = "1000"
OTHER_COMPANY = "2000"


class Ticks:
    """A clock that advances a second per call, so staged_at values are distinct and ordered."""

    def __init__(self) -> None:
        self._at = datetime(2026, 11, 21, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._at += timedelta(seconds=1)
        return self._at


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "erp.sqlite3"


@pytest.fixture
def erp(db_path: Path) -> Iterator[Erp]:
    with Erp.open(db_path, clock=Ticks()) as opened:
        yield opened


@pytest.fixture
def raw(db_path: Path, erp: Erp) -> Iterator[sqlite3.Connection]:
    """A second, plain connection to the same file, for asserting what the DATABASE refuses.

    A guard proved only through `Erp` could be a check in Python wearing a constraint's clothes.
    """
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def clerk() -> Principal:
    return Principal("clerk@demo", Role.CLERK, COMPANY)


@pytest.fixture
def approver() -> Principal:
    return Principal("approver@demo", Role.APPROVER, COMPANY)


@pytest.fixture
def disputes() -> Principal:
    return Principal("disputes@demo", Role.DISPUTES, COMPANY)


@pytest.fixture
def service() -> Principal:
    return Principal("svc-app19", Role.SERVICE, COMPANY)


@pytest.fixture
def other_clerk() -> Principal:
    """A clerk in the other company code: the principal every cross-company rule is tested with."""
    return Principal("clerk@other", Role.CLERK, OTHER_COMPANY)
