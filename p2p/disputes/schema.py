"""The `dispute-triage` output schema: a closed set of next actions (statement 2.3).

Amounts are integer minor currency units: 250_000 with currency USD is 2,500.00. The
escalation criterion "the credit exceeds 2,500" is therefore `amount > 250_000` in any
currency, which is why a credit row carries its currency at all.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TASK_NAME: Final = "dispute-triage"
SCHEMA_TOOL: Final = "dispute_step"
TRANSCRIPT_CAP: Final = 12_000
CREDIT_LIMIT_MINOR: Final = 250_000  # 2,500.00 in any currency
VARIANCE_PERCENT: Final = 5


class NextAction(StrEnum):
    """Every next action the model may name. Anything else fails schema validation."""

    READ_SUPPLIER = "read_supplier"
    READ_INVOICE = "read_invoice"
    READ_PAYMENT = "read_payment"
    PROPOSE_CREDIT = "propose_credit"
    HOLD_PAYMENT = "hold_payment"
    ASK_QUESTION = "ask_question"
    ESCALATE = "escalate"
    RESOLVE = "resolve"


class Criterion(StrEnum):
    """The five written escalation criteria (statement 5.2), verbatim."""

    NO_PO = "the invoice has no PO"
    VARIANCE = "the variance exceeds 5%"
    CREDIT_AMOUNT = "the credit exceeds 2,500"
    FLAGGED_SUPPLIER = "the supplier is flagged"
    LOOPED = "the case has looped twice without new facts"


#: The ERP tool each action names, or None when the action is not a tool call.
ACTION_TOOLS: Final[dict[NextAction, str | None]] = {
    NextAction.READ_SUPPLIER: "find_supplier",
    NextAction.READ_INVOICE: "get_invoice",
    NextAction.READ_PAYMENT: "get_payment_status",
    NextAction.PROPOSE_CREDIT: "issue_credit_memo",
    NextAction.HOLD_PAYMENT: "hold_payment",
    NextAction.ASK_QUESTION: None,
    NextAction.ESCALATE: None,
    NextAction.RESOLVE: None,
}

CRITERIA: Final[tuple[Criterion, ...]] = tuple(Criterion)


class DisputeStep(BaseModel):
    """One step of the disputes desk. Sent as a forced schema tool; never parsed from prose."""

    model_config = ConfigDict(extra="forbid", strict=True)

    next_action: NextAction
    tool_choice: str | None = None
    draft_reply: str = Field(min_length=1, max_length=2000)
    escalate: Criterion | None = None
    question: str | None = Field(default=None, min_length=1, max_length=500)
    supplier_name: str | None = Field(default=None, min_length=1, max_length=200)
    invoice_id: str | None = Field(default=None, min_length=1, max_length=64)
    amount: int | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("next_action", mode="before")
    @classmethod
    def _closed_action(cls, value: object) -> object:
        # Strict mode would refuse the JSON string a model puts in a tool call; the closed set
        # is still enforced by constructing the enum, which raises on anything else.
        if isinstance(value, NextAction):
            return value
        if isinstance(value, str):
            try:
                return NextAction(value)
            except ValueError:
                raise ValueError("next_action is not in the closed set") from None
        return value

    @field_validator("escalate", mode="before")
    @classmethod
    def _closed_criterion(cls, value: object) -> object:
        if value is None or isinstance(value, Criterion):
            return value
        if isinstance(value, str):
            try:
                return Criterion(value)
            except ValueError:
                raise ValueError("escalate is not a written criterion") from None
        return value

    @model_validator(mode="after")
    def tool_choice_matches_the_action(self) -> Self:
        expected = ACTION_TOOLS[self.next_action]
        if self.tool_choice != expected:
            raise ValueError("tool_choice must name the tool for next_action")
        if self.next_action is NextAction.PROPOSE_CREDIT and (
            self.amount is None or self.currency is None or self.reason is None
        ):
            raise ValueError("propose_credit needs amount, currency and reason")
        if self.next_action is NextAction.HOLD_PAYMENT and self.reason is None:
            raise ValueError("hold_payment needs a reason")
        if self.next_action is NextAction.ASK_QUESTION and self.question is None:
            raise ValueError("ask_question needs a question")
        return self
