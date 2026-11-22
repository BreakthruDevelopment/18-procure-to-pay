"""The `invoice-match` output schema (statements 1.4, 1.6, 5.5).

`MatchOutcome` is what the model must call. The confidence band is not a field of it:
the band is computed in code from the extraction's per-field confidences and the
variance set, and a model-supplied number is not the one a posting is staged on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

TASK_NAME: Final = "invoice-match"
SCHEMA_TOOL: Final = "match_outcome"
#: The task cap: 8,000 characters of a tool result before it is fenced for the model.
RESULT_CAP: Final = 8_000
HIGH_AT: Final = 0.9
LOW_AT: Final = 0.6
VARIANCE_PERCENT: Final = 5

#: The fixed workflow. Code runs these in order; the model does not plan them.
STEPS: Final[tuple[str, ...]] = ("extract", "fetch_po", "fetch_receipt", "decide")

# Reasons are fixed sentences. A why built for a person or a payload is one of these,
# or several of them joined with "; ". Document text never becomes a reason.
NO_PO: Final = "the invoice has no PO"
VARIANCE_OVER: Final = "the variance exceeds 5%"
FLAGGED: Final = "the supplier is flagged"
DUPLICATE: Final = "the invoice number is duplicated"
CURRENCY: Final = "the invoice currency does not match the purchase order"
DRAFT: Final = "the extracted draft does not match the invoice"
LOW_CONFIDENCE: Final = "confidence is below 0.6"
REVIEW_BAND: Final = "confidence is in the review band; check the listed fields"
CLEAN: Final = "the invoice matches its purchase order and goods receipt"
BAD_ID: Final = "a model answer named a record the ERP does not confirm"
STEP_MISSED: Final = "the model did not call the step's tool"
NOT_CONFIRMED: Final = "the model answer did not confirm the match"

KNOWN_WHYS: Final[frozenset[str]] = frozenset(
    {
        NO_PO,
        VARIANCE_OVER,
        FLAGGED,
        DUPLICATE,
        CURRENCY,
        DRAFT,
        LOW_CONFIDENCE,
        REVIEW_BAND,
        CLEAN,
        BAD_ID,
        STEP_MISSED,
        NOT_CONFIRMED,
    }
)
CHECKABLE: Final[frozenset[str]] = frozenset(
    {
        "supplier_name",
        "po_ref",
        "number",
        "date",
        "currency",
        "total",
        "lines",
        "quantity",
        "price",
    }
)


class Verdict(StrEnum):
    MATCH = "match"
    VARIANCE = "variance"
    NO_PO = "no_po"


class VarianceKind(StrEnum):
    QUANTITY = "quantity"
    PRICE = "price"


class Band(StrEnum):
    """At least 0.9 with no variance, 0.6 to 0.9 (or a variance), and below 0.6."""

    HIGH = "high"
    REVIEW = "review"
    LOW = "low"


class VarianceItem(BaseModel):
    """One difference between the invoice and the purchase order or the receipt."""

    model_config = ConfigDict(extra="forbid")

    kind: VarianceKind
    line_no: int = Field(ge=1, le=100)
    expected: int = Field(ge=-100_000_000, le=100_000_000)
    actual: int = Field(ge=-100_000_000, le=100_000_000)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, value: object) -> object:
        if isinstance(value, VarianceKind):
            return value
        if isinstance(value, str):
            try:
                return VarianceKind(value)
            except ValueError:
                raise ValueError("variance kind is not quantity or price") from None
        return value


class MatchOutcome(BaseModel):
    """The decide step's forced tool. Ids here are checked against the ERP before use."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    variances: list[VarianceItem] = Field(default_factory=list, max_length=40)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human: bool
    why: str = Field(min_length=1, max_length=500)
    po_id: str | None = Field(default=None, max_length=64)
    supplier_id: str | None = Field(default=None, max_length=64)

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, value: object) -> object:
        if isinstance(value, Verdict):
            return value
        if isinstance(value, str):
            try:
                return Verdict(value)
            except ValueError:
                raise ValueError("verdict is not match, variance or no_po") from None
        return value

    @field_validator("po_id", "supplier_id", mode="before")
    @classmethod
    def _blank_id(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return value


class PostingFacts(BaseModel):
    """The posting payload `p2p review` knows how to show. Anything else is ignored.

    A posting staged through the gateway directly may carry a different shape. Review
    then shows the row's summary and no band, rather than printing payload text.
    """

    model_config = ConfigDict(extra="ignore")

    verdict: Verdict | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    band: Band | None = None
    needs_human: bool | None = None
    why: str | None = None
    fields_to_check: list[str] = Field(default_factory=list, max_length=20)
    variances: list[VarianceItem] = Field(default_factory=list, max_length=40)

    @field_validator("band", mode="before")
    @classmethod
    def _band(cls, value: object) -> object:
        if value is None or isinstance(value, Band):
            return value
        if isinstance(value, str):
            try:
                return Band(value)
            except ValueError:
                return None
        return None

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, value: object) -> object:
        if value is None or isinstance(value, Verdict):
            return value
        if isinstance(value, str):
            try:
                return Verdict(value)
            except ValueError:
                return None
        return None
