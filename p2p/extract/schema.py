"""The `InvoiceDraft` output schema, sent as a forced tool (statements 4.3, 4.4).

Amounts are integer minor currency units: 125000 with currency USD is 1,250.00. A
credit line is a negative `unit_price` (or quantity) that still has to reconcile with
`total`. Bounds are closed so a model cannot pad a draft with unbounded strings or
an unbounded line list.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TASK_NAME: Final = "invoice-extract"
SCHEMA_TOOL: Final = "invoice_draft"
DOCUMENT_CAP: Final = 20_000
MAX_LINES: Final = 20
MAX_UNREADABLE: Final = 20

_DATE: Final = "%Y-%m-%d"
_CURRENCY: Final = re.compile(r"\A[A-Za-z]{3}\Z")


class InvoiceDraftLine(BaseModel):
    """One line of an extracted invoice. Money fields are integer minor units."""

    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(ge=1, le=MAX_LINES)
    description: str = Field(min_length=1, max_length=200)
    qty: int = Field(ge=-1_000_000, le=1_000_000)
    unit_price: int = Field(ge=-100_000_000, le=100_000_000)
    tax: int = Field(default=0, ge=-100_000_000, le=100_000_000)

    @property
    def extension(self) -> int:
        return self.qty * self.unit_price + self.tax


class FieldConfidence(BaseModel):
    """Per-field confidence, each a probability in [0, 1]."""

    model_config = ConfigDict(extra="forbid")

    supplier_name: float = Field(ge=0.0, le=1.0)
    po_ref: float = Field(ge=0.0, le=1.0)
    number: float = Field(ge=0.0, le=1.0)
    date: float = Field(ge=0.0, le=1.0)
    currency: float = Field(ge=0.0, le=1.0)
    total: float = Field(ge=0.0, le=1.0)
    lines: float = Field(ge=0.0, le=1.0)

    def overall(self) -> float:
        """The extraction row stores one number; 18.5 recomputes bands from the fields."""
        return min(
            self.supplier_name,
            self.po_ref,
            self.number,
            self.date,
            self.currency,
            self.total,
            self.lines,
        )


class InvoiceDraft(BaseModel):
    """Extracted invoice fields. A free-text answer is never parsed into this."""

    model_config = ConfigDict(extra="forbid")

    supplier_name: str = Field(min_length=1, max_length=200)
    po_ref: str | None = Field(default=None, max_length=64)
    number: str = Field(min_length=1, max_length=64)
    date: str = Field(min_length=10, max_length=10)
    currency: str = Field(min_length=3, max_length=3)
    total: int = Field(ge=-100_000_000, le=100_000_000)
    lines: list[InvoiceDraftLine] = Field(min_length=1, max_length=MAX_LINES)
    confidence: FieldConfidence
    unreadable: list[str] = Field(default_factory=list, max_length=MAX_UNREADABLE)

    @field_validator("po_ref", mode="before")
    @classmethod
    def _empty_po_is_missing(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("supplier_name", "number", "po_ref")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        text = value.strip().upper()
        if _CURRENCY.match(text) is None:
            raise ValueError("currency must be a three-letter code")
        return text

    @field_validator("date")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        text = value.strip()
        try:
            datetime.strptime(text, _DATE)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD") from None
        return text

    @field_validator("unreadable")
    @classmethod
    def _cap_unreadable(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if text:
                cleaned.append(text[:200])
        return cleaned

    @model_validator(mode="after")
    def totals_must_reconcile(self) -> Self:
        line_total = sum(line.extension for line in self.lines)
        if line_total != self.total:
            raise ValueError("lines do not sum to the stated total")
        return self
