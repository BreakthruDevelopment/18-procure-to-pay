"""The Adapter: one ERP call, presented as an app-17 `Tool`.

The MCP gateway (slice 18.3) and the agent loop (18.5, 18.6) must not be two implementations of the
same nine operations, or they will disagree -- about an `effect`, about which role may call
something, about what an error means. So an operation is written once, as an `ErpOperation`, and
`erp_tool()` wraps it into a `Tool` bound to one `Erp` and one `Principal`. Both callers get the
same object; neither can set a different `effect` for it.

What this file is responsible for, and nothing else:

* **The `effect`.** It comes from the operation's declaration and is passed through unchanged. App
  17 refuses to build a loop containing an `Effect.WRITE` tool with no `PermissionGate` anywhere in
  its stack, and `writes()` reads the whole stack, so declaring the truth here is what makes that
  check work at all. Declaring a write as a read would defeat it.
* **Turning a failure into a result, not an exception.** App 17's contract is that a tool returns
  failures as `ToolResult`s so the model can react to them. Every failure this app reports is a
  `P2PError` subclass that already carries its `error_category` and `is_retryable`, so the mapping
  here is a lookup, not a judgement: nothing in this file decides a category, which is what keeps
  "a rule said no" (`business`) from being used for repeated validation failure.
* **Carrying the staged change AND its key.** A staged write records a row and acts on nothing. It
  returns app 17's `StagedChange` -- for the gateway's contract -- and the `change_key` of the row
  it recorded, because `StagedChange` has no id and the key is the only thing an approver can name.
  Without the key in the success result, a clerk gets a change it can never refer to again.

Nothing is asked of app 17: `StagedErpChange` implements its `StagedChange` contract (`description`
and `apply`), and this spec adds no field to it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from agent_container.core.tool import (
    AnyTool,
    Effect,
    StagedChange,
    Tool,
    ToolContext,
    ToolResult,
)
from pydantic import BaseModel

from p2p.core.access import READ_ACTIONS, Action, Principal, assert_can
from p2p.core.erp import Decision, Erp, StagedChangeRow
from p2p.core.errors import InternalFailure, P2PError, ValidationFailed

#: A tool result's own size cap, matching the spec's 8,000 characters per tool result. Reads are
#: already paged by `erp.py`; this is the backstop for a page that is small in rows and large in
#: text. It refuses rather than truncating: half a JSON document is not a smaller answer, it is a
#: broken one, and a model handed one would guess at the rest.
MAX_RESULT_CHARS: Final = 8_000


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """What an operation's body hands back: the content, and a staged row if it recorded one."""

    content: str
    staged: StagedChangeRow | None = None


@dataclass(frozen=True, slots=True)
class ErpOperation[A: BaseModel]:
    """One ERP call, declared once: its name, its typed input, its `effect` and its body.

    `action` is the access action the operation needs. The accessors in `erp.py` each call
    `assert_can` themselves, so this is a second, earlier check: it lets the gateway refuse a tool
    to a role before the call is made -- which is how a `service` principal is told it may not use a
    write tool, rather than being told a row is not visible.
    """

    name: str
    description: str
    effect: Effect
    input_model: type[A]
    action: Action
    run: Callable[[Erp, Principal, A], OperationOutcome]

    def __post_init__(self) -> None:
        if self.effect is Effect.READ and self.action not in READ_ACTIONS:
            raise ValueError(f"operation {self.name!r} declares effect 'read' for a write action")
        if self.effect is Effect.WRITE and self.action in READ_ACTIONS:
            raise ValueError(f"operation {self.name!r} declares effect 'write' for a read action")

    def bind(self, erp: Erp, principal: Principal) -> ErpTool[A]:
        """This operation as a tool acting for one principal against one ERP."""
        return ErpTool(self, erp, principal)


class AnyErpOperation(Protocol):
    """An operation with its input type erased: the view a registry holds.

    The same shape, and the same reason, as app 17's `AnyTool`. `ErpOperation[A]` has to be precise
    in `A` for the author of an operation, but a registry holds operations of many argument types
    and `ErpOperation[A]` is invariant in `A`, so a list of them is not a list of
    `ErpOperation[BaseModel]`. Every `ErpOperation[A]` satisfies this.
    """

    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def effect(self) -> Effect: ...
    @property
    def action(self) -> Action: ...
    def bind(self, erp: Erp, principal: Principal) -> AnyTool: ...


class StagedErpChange(StagedChange):
    """App 17's `StagedChange` over a row this app recorded in one of its staged-change tables.

    `apply()` goes through `Erp.apply_change`, the single apply path, so everything that path
    enforces holds here too: the acting principal must be an approver in the right company code,
    must not be the one that staged the change, and a second apply is the stated no-op rather than
    a second write.

    It therefore matters **which principal this object carries**. A staged write hands one back to
    the principal that staged it, and that principal cannot apply it: calling `apply()` raises
    `AccessDenied` for separation of duties. That is the intended behaviour, not an oversight -- a
    clerk holding a `StagedChange` must not be one `await` away from posting it.

    `apply()` lets a `P2PError` out rather than converting it, so an approver sees why. The error
    already carries its category.
    """

    def __init__(
        self,
        erp: Erp,
        principal: Principal,
        change: StagedChangeRow,
        *,
        decision: Decision | None = None,
    ) -> None:
        self._erp = erp
        self._principal = principal
        self._change = change
        self._decision = decision if decision is not None else Decision.applied()

    @property
    def change_key(self) -> str:
        """The key of the row this change is recorded as. What an approver names."""
        return self._change.change_key

    @property
    def description(self) -> str:
        return f"{self._change.summary} (staged change {self._change.change_key})"

    async def apply(self) -> None:
        self._erp.apply_change(self._principal, self._change.change_key, self._decision)


class ErpTool[A: BaseModel](Tool[A]):
    """An `ErpOperation` bound to one `Erp` and one `Principal`, as an app-17 `Tool`.

    A leaf tool: `wrapped` stays `None`, and slice 18.3 is what puts a `PermissionGate` and the
    other decorators around it. Binding the principal at construction is deliberate -- a command
    invocation and an MCP session each act as exactly one principal, so a tool that took the
    principal per call would be a way to act as somebody else inside one session.
    """

    def __init__(self, operation: ErpOperation[A], erp: Erp, principal: Principal) -> None:
        self._operation = operation
        self._erp = erp
        self._principal = principal

    @property
    def name(self) -> str:
        return self._operation.name

    @property
    def description(self) -> str:
        return self._operation.description

    @property
    def effect(self) -> Effect:
        return self._operation.effect

    @property
    def input_model(self) -> type[A]:
        return self._operation.input_model

    @property
    def operation(self) -> ErpOperation[A]:
        """The declaration this tool was built from, for the gateway's per-step manifests."""
        return self._operation

    async def run(self, args: A, ctx: ToolContext) -> ToolResult:
        try:
            assert_can(self._principal, self._operation.action)
            outcome = self._operation.run(self._erp, self._principal, args)
            if len(outcome.content) > MAX_RESULT_CHARS:
                raise ValidationFailed(
                    "this result is larger than a tool result may be; ask for a smaller page"
                )
        except P2PError as failure:
            return as_error(failure)
        if outcome.staged is None:
            return ToolResult.ok(outcome.content)
        return ToolResult.ok(
            outcome.content,
            staged=StagedErpChange(self._erp, self._principal, outcome.staged),
        )


def as_error(failure: P2PError) -> ToolResult:
    """A `P2PError` as an app-17 error result, carrying the category the error already declares."""
    return ToolResult.error(failure.category, str(failure), retryable=failure.is_retryable)


@dataclass(frozen=True, slots=True)
class ToolSet:
    """The tools one principal may call, built from a registry of operations.

    Slice 18.3 registers the gateway's nine operations and asks for a set per session; the per-step
    manifests it builds are subsets of `names`. Operations the principal's role does not hold are
    left out rather than included and refused, so a model is never shown a tool it cannot call --
    which is the "tool distribution" half of statement 2.3.
    """

    tools: Mapping[str, AnyTool] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.tools))

    def get(self, name: str) -> AnyTool:
        tool = self.tools.get(name)
        if tool is None:
            raise ValidationFailed("no such tool is available to this principal")
        return tool

    def subset(self, names: Sequence[str]) -> ToolSet:
        """A manifest: the named tools, refusing a name this principal does not hold."""
        return ToolSet({name: self.get(name) for name in names})


def build_tool_set(
    operations: Sequence[AnyErpOperation], erp: Erp, principal: Principal
) -> ToolSet:
    """Every operation this principal's role holds, bound and keyed by name."""
    allowed: dict[str, AnyTool] = {}
    seen: set[str] = set()
    for operation in operations:
        if operation.name in seen:
            raise InternalFailure("two operations were registered under one name")
        seen.add(operation.name)
        try:
            assert_can(principal, operation.action)
        except P2PError:
            continue
        allowed[operation.name] = operation.bind(erp, principal)
    return ToolSet(allowed)


__all__ = (
    "MAX_RESULT_CHARS",
    "AnyErpOperation",
    "ErpOperation",
    "ErpTool",
    "OperationOutcome",
    "StagedErpChange",
    "ToolSet",
    "as_error",
    "build_tool_set",
)
