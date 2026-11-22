"""The one summary `p2p batch` prints and stores on `batch_run.summary_json`.

Assembled here, in code. A model is never asked for these totals: `already_advanced`
is database state, `not_reached` is a fact about the budget, and a `change_key` is
the key of a posting staged during the run.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from agent_container.core.tool import ErrorCategory
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ItemOutcome(StrEnum):
    """The one place a document lands. The three totals count these."""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    """Why a document was not attempted. Neither is an `error_category`."""

    ALREADY_ADVANCED = "already_advanced"
    NOT_REACHED = "not_reached"


class BatchItem(BaseModel):
    """One document. The filename is the identity: a refused document has no invoice id."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1)
    outcome: ItemOutcome
    error_category: ErrorCategory | None = None
    skip_reason: SkipReason | None = None
    change_key: str | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        failed = self.outcome is ItemOutcome.FAILED
        skipped = self.outcome is ItemOutcome.SKIPPED
        if failed and self.error_category is None:
            raise ValueError("a failed item needs an error_category")
        if not failed and self.error_category is not None:
            raise ValueError("an error_category belongs only to a failed item")
        if skipped and self.skip_reason is None:
            raise ValueError("a skipped item needs a skip_reason")
        if not skipped and self.skip_reason is not None:
            raise ValueError("a skip_reason belongs only to a skipped item")
        if self.change_key is not None and self.outcome is not ItemOutcome.OK:
            raise ValueError("a change_key belongs to an item that staged a posting")
        if self.change_key is not None and not self.change_key:
            raise ValueError("a change_key may not be empty")
        return self


class BatchSummary(BaseModel):
    """Every document found, the three totals, and the budget's reason when it stopped the run."""

    model_config = ConfigDict(extra="forbid")

    items: int = Field(ge=0)
    ok: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    stopped_early: str | None = None
    documents: list[BatchItem]

    @model_validator(mode="after")
    def _adds_up(self) -> Self:
        if self.items != len(self.documents):
            raise ValueError("items must equal the number of documents")
        counts = {outcome: 0 for outcome in ItemOutcome}
        not_reached = False
        cut = False
        for item in self.documents:
            counts[item.outcome] += 1
            if item.skip_reason is SkipReason.NOT_REACHED:
                not_reached = True
            if item.error_category is ErrorCategory.TRANSIENT:
                cut = True
        if self.ok != counts[ItemOutcome.OK]:
            raise ValueError("ok must equal the documents with that outcome")
        if self.failed != counts[ItemOutcome.FAILED]:
            raise ValueError("failed must equal the documents with that outcome")
        if self.skipped != counts[ItemOutcome.SKIPPED]:
            raise ValueError("skipped must equal the documents with that outcome")
        if self.items != self.ok + self.failed + self.skipped:
            raise ValueError("items must equal ok + failed + skipped")
        # `not_reached` is a document the budget stopped the run before. The reason
        # is the budget's, carried once, not repeated on the item.
        if not_reached and not self.stopped_early:
            raise ValueError("a document not reached needs the budget's reason")
        if self.stopped_early and not not_reached and not cut:
            raise ValueError("stopped_early needs a document the budget did not finish")
        return self


def format_summary(summary: BatchSummary) -> str:
    """One JSON document. Absent fields are the ones that outcome does not carry."""
    return summary.model_dump_json(indent=2, exclude_none=True)
