"""One MCP session: one principal, one ERP, the tools that principal may call.

The tool stack for a write is `Intercepted(StagedWriteGate(leaf))`. The stack for a read
is `Intercepted(PermissionGate(leaf, read))`. The chain is shared, so the turn cap and
the step guard apply to every tool, and a model is never shown a tool its role does not
hold: those operations are left out of the stack rather than included and refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from agent_container.core.gate import PermissionGate, Policy
from agent_container.core.interceptors import (
    TURN_CAP_NOTICE,
    TURN_CAP_OMITTED,
    Intercepted,
    InterceptorChain,
    TurnOutputCap,
)
from agent_container.core.messages import ToolChoice, ToolSpec
from agent_container.core.tool import (
    TOOL_NAME_PATTERN,
    AnyTool,
    Effect,
    ErrorCategory,
    Identity,
    Tool,
    ToolContext,
    ToolResult,
)
from pydantic import BaseModel, JsonValue

from p2p.core.access import Principal, allowed_actions
from p2p.core.adapter import AnyErpOperation, ErpTool, StagedErpChange, build_tool_set
from p2p.core.erp import Erp
from p2p.core.errors import AccessDenied, InternalFailure, P2PError, ValidationFailed
from p2p.mcp.contract import (
    ERROR_FIELDS,
    FORCED_TOOL_INVALID,
    INTERNAL_MESSAGE,
    NO_SUCH_TOOL,
    NOT_PERMITTED,
    PAGE_TOO_LARGE,
    STEP_TOOLS,
    TOOL_NAMES,
    WRITE_KEY_MISSING,
)
from p2p.mcp.gate import StagedWriteGate
from p2p.mcp.interceptors import CallAudit, RefuseTruncation, StepGuard
from p2p.mcp.log import logger
from p2p.mcp.manifest import manifest_for, tool_choice_for
from p2p.mcp.operations import OPERATIONS, StagedWriteOut, parse_object

_log = logging.getLogger("p2p.mcp")


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """One call, in the shape the MCP result is built from.

    `text` is `body` as one JSON line. A client that only reads the text content still
    sees `error_category` or `change_key`. `staged` is the in-process change, present
    only when this call recorded one; it is not serialized.
    """

    is_error: bool
    body: Mapping[str, JsonValue]
    text: str
    staged: StagedErpChange | None = None

    @property
    def error_category(self) -> str | None:
        if not self.is_error:
            return None
        value = self.body["error_category"]
        if not isinstance(value, str):
            raise InternalFailure("an error payload's category was not text")
        return value

    @property
    def is_retryable(self) -> bool:
        if not self.is_error:
            return False
        value = self.body["is_retryable"]
        if not isinstance(value, bool):
            raise InternalFailure("an error payload's is_retryable was not a boolean")
        return value


def _error(
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool,
) -> GatewayResult:
    body: dict[str, JsonValue] = {
        "error_category": category.value,
        "is_retryable": retryable,
        "message": message,
    }
    if tuple(sorted(body)) != ERROR_FIELDS:
        raise InternalFailure("the error payload does not match the frozen fields")
    text = _canonical(body)
    return GatewayResult(is_error=True, body=body, text=text, staged=None)


def _canonical(body: Mapping[str, JsonValue]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _cut(content: str) -> bool:
    """Whether app 17's turn cap replaced or shortened this result.

    A shortened JSON document is not a smaller answer. The caller is told to ask for a
    smaller page, and the shortened text is not returned.
    """
    return content == TURN_CAP_OMITTED or content.endswith(TURN_CAP_NOTICE)


def from_tool_result(tool: AnyTool, result: ToolResult) -> GatewayResult:
    """The frozen payload for one tool result. Categories are read, never chosen here."""
    if isinstance(result.staged, StagedErpChange) and not result.is_error:
        payload = StagedWriteOut(
            change_key=result.staged.change_key,
            description=result.staged.description,
        )
        raw = payload.model_dump(mode="json")
        if not isinstance(raw, dict):
            raise InternalFailure("a staged write result was not an object")
        body = cast(dict[str, JsonValue], raw)
        return GatewayResult(is_error=False, body=body, text=_canonical(body), staged=result.staged)
    if tool.effect == Effect.WRITE and not result.is_error:
        # The row may already exist. Retrying would stage a second one, so this is not
        # retryable, and the message does not say that nothing was done.
        return _error(ErrorCategory.INTERNAL, WRITE_KEY_MISSING, retryable=False)
    if _cut(result.content):
        # A cut FAILURE keeps the category and retryability it already had. App 17's
        # `TurnOutputCap` preserves both when it shortens a result, and discarding them here
        # would publish a transient ERP lock as a permanent error about the caller's input:
        # app 19 would be told a retryable failure is a dead end, which is the whole of exam
        # statement 2.2, and a refused call would be blamed on the arguments. Only the message
        # is replaced, so the cut text is still never returned. A cut SUCCESS is a page the
        # caller should ask for again smaller, which is `validation`.
        if result.is_error and result.error_category is not None:
            return _error(result.error_category, PAGE_TOO_LARGE, retryable=result.is_retryable)
        return _error(ErrorCategory.VALIDATION, PAGE_TOO_LARGE, retryable=False)
    if result.is_error:
        category = result.error_category
        if category is None:
            return _error(ErrorCategory.INTERNAL, INTERNAL_MESSAGE, retryable=False)
        return _error(category, result.content, retryable=result.is_retryable)
    try:
        body = parse_object(result.content)
    except InternalFailure:
        return _error(ErrorCategory.INTERNAL, INTERNAL_MESSAGE, retryable=False)
    return GatewayResult(is_error=False, body=body, text=result.content, staged=None)


def _stack(
    operation: AnyErpOperation, leaf: ErpTool[BaseModel], chain: InterceptorChain
) -> AnyTool:
    """The leaf behind its gate and the shared interceptor chain."""
    gated: Tool[BaseModel]
    if operation.effect == Effect.WRITE:
        gated = StagedWriteGate(leaf)
    else:
        gated = PermissionGate(leaf, Policy.READ)
    return Intercepted(gated, chain)


class GatewaySession:
    """The tools one principal may call, and the step that further narrows them."""

    def __init__(
        self,
        erp: Erp,
        principal: Principal,
        *,
        step: str | None = None,
        max_turn_chars: int | None = None,
    ) -> None:
        logger()
        if step is not None:
            # Validates the name before any tool is built. Unknown steps do not echo it.
            manifest_for(step)
        self._erp = erp
        self._principal = principal
        self._step = step
        self._turn = 0
        self._run_id = secrets.token_hex(8)
        cap = TurnOutputCap() if max_turn_chars is None else TurnOutputCap(max_turn_chars)
        # `after` runs in reverse of this tuple: the cap may shorten a result, the
        # refusal replaces a shortened read, and the audit logs what the caller receives.
        self._chain = InterceptorChain(
            (CallAudit(), RefuseTruncation(), cap, StepGuard(self.current_step))
        )
        self._tools = self._build()

    def current_step(self) -> str | None:
        return self._step

    def set_step(self, step: str | None) -> None:
        """Narrow subsequent calls to one step's manifest. `None` restores the role's set."""
        if step is not None:
            manifest_for(step)
        self._step = step

    @property
    def principal(self) -> Principal:
        return self._principal

    @property
    def tools(self) -> Mapping[str, AnyTool]:
        """Every tool this principal's role holds, gated. Not narrowed by the active step.

        The step narrows `visible_names` and refuses a call. The stack still holds the
        role's tools, so the refusal is the step interceptor's and not a missing tool.
        """
        return self._tools

    def tool(self, name: str) -> AnyTool:
        found = self._tools.get(name)
        if found is not None:
            return found
        if name in TOOL_NAMES:
            raise AccessDenied(NOT_PERMITTED)
        raise ValidationFailed(NO_SUCH_TOOL)

    def visible_names(self, step: str | None = None) -> tuple[str, ...]:
        """Tool names this principal may call, in contract order, cut to a step when one is set.

        `step` defaults to the session's active step. Pass a name to ask about another
        step without changing the session, which is what a model request for the next
        step needs.
        """
        chosen = self._step if step is None else step
        if chosen is not None:
            manifest_for(chosen)
            order = STEP_TOOLS[chosen]
        else:
            order = TOOL_NAMES
        return tuple(name for name in order if name in self._tools)

    def tool_choice(self, step: str | None = None, *, force: str | None = None) -> ToolChoice:
        """The `ToolChoice` for a step, over the tools this principal actually holds."""
        chosen = self._step if step is None else step
        if chosen is None:
            if force is None:
                return ToolChoice(mode="auto")
            if TOOL_NAME_PATTERN.match(force) is None:
                raise ValidationFailed(FORCED_TOOL_INVALID)
            return ToolChoice(mode="tool", name=force)
        return tool_choice_for(chosen, self.visible_names(chosen), force=force)

    def model_tools(self, step: str | None = None) -> tuple[ToolSpec, ...]:
        """The tool specs a model request for this step should advertise."""
        specs: list[ToolSpec] = []
        for name in self.visible_names(step):
            tool = self._tools[name]
            specs.append(ToolSpec(tool.name, tool.description, tool.input_schema()))
        return tuple(specs)

    def call(
        self,
        name: str,
        arguments: Mapping[str, JsonValue] | None = None,
        *,
        turn: int | None = None,
        identity: Identity | None = None,
    ) -> GatewayResult:
        """Run one tool call on a fresh event loop.

        Use `acall` from a loop that is already running.
        """
        return asyncio.run(self.acall(name, arguments, turn=turn, identity=identity))

    async def acall(
        self,
        name: str,
        arguments: Mapping[str, JsonValue] | None = None,
        *,
        turn: int | None = None,
        identity: Identity | None = None,
    ) -> GatewayResult:
        tool = self._tools.get(name)
        if tool is None:
            if name not in TOOL_NAMES:
                return _error(ErrorCategory.VALIDATION, NO_SUCH_TOOL, retryable=False)
            return _error(ErrorCategory.PERMISSION, NOT_PERMITTED, retryable=False)
        if turn is None:
            self._turn += 1
            turn = self._turn
        ctx = ToolContext(
            run_id=self._run_id,
            turn=turn,
            tool_use_id=f"mcp-{turn}",
            identity=identity if identity is not None else self._identity(),
        )
        try:
            result = await tool.invoke(dict(arguments or {}), ctx)
        except P2PError as failure:
            return _error(failure.category, str(failure), retryable=failure.is_retryable)
        except Exception as failure:
            # The exception's message is unreviewed. The type is a fact about this code.
            _log.error(
                "gateway call failed",
                extra={
                    "tool": name,
                    "error_category": ErrorCategory.INTERNAL.value,
                    "is_retryable": False,
                    "is_error": True,
                    "staged": False,
                    "failure": type(failure).__name__,
                },
            )
            return _error(ErrorCategory.INTERNAL, INTERNAL_MESSAGE, retryable=False)
        return from_tool_result(tool, result)

    def _identity(self) -> Identity:
        permissions = frozenset(action.value for action in allowed_actions(self._principal.role))
        return Identity(self._principal.subject, permissions)

    def _build(self) -> dict[str, AnyTool]:
        """Bind the operations this role holds. `build_tool_set` is that decision, used once.

        The objects it returns are the leaves. The gates and the chain wrap those same
        objects, so there are not two copies of an operation to drift apart.
        """
        leaves = build_tool_set(OPERATIONS, self._erp, self._principal)
        by_name = {operation.name: operation for operation in OPERATIONS}
        stacked: dict[str, AnyTool] = {}
        for name, leaf in leaves.tools.items():
            operation = by_name[name]
            if not isinstance(leaf, ErpTool):
                raise InternalFailure("a gateway tool was not built from an ERP operation")
            stacked[name] = _stack(operation, cast(ErpTool[BaseModel], leaf), self._chain)
        return stacked
