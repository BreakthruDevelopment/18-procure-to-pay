"""Interceptors on every gateway call (statement 1.5, exercised here).

* `StepGuard` answers a call the active step's manifest does not name, and the tool
  never runs. That is the "a tool outside the step's manifest cannot be called" rule,
  enforced in code rather than written into a prompt.
* `RefuseTruncation` replaces a page the turn cap cut, so a partial document is not
  returned. A staged write is left intact: its key is read off the staged change.
* `CallAudit` writes one JSON log line of fixed fields after the call. It does not log
  the arguments or the result text. A posting payload and an invoice line are data.

`TurnOutputCap` from app 17 sits in the same chain. It is the library's interceptor,
which is the 1.5 exercise: the gateway's calls pass through it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent_container.core.interceptors import TURN_CAP_NOTICE, TURN_CAP_OMITTED, Interceptor
from agent_container.core.tool import ErrorCategory, ToolContext, ToolResult
from pydantic import BaseModel

from p2p.core.adapter import StagedErpChange
from p2p.mcp.contract import NOT_IN_MANIFEST, PAGE_TOO_LARGE, STEP_TOOLS

_log = logging.getLogger("p2p.mcp")


class StepGuard(Interceptor):
    """Refuse a tool the active step does not list. No step means the session's full set."""

    def __init__(self, step: Callable[[], str | None]) -> None:
        self._step = step

    async def before(self, tool: str, args: BaseModel, ctx: ToolContext) -> ToolResult | None:
        # Arguments are the caller's data. The decision uses the tool's name only.
        del args, ctx
        current = self._step()
        if current is None:
            return None
        allowed = STEP_TOOLS.get(current)
        if allowed is not None and tool in allowed:
            return None
        return ToolResult.error(ErrorCategory.PERMISSION, NOT_IN_MANIFEST)


class RefuseTruncation(Interceptor):
    """Turn a cut page into an error, and do not return the cut text.

    A write keeps its staged change: the key is read from that object, and the cut text
    is discarded by the session. Replacing the result would drop the key of a row that
    had already been recorded.
    """

    async def after(
        self, tool: str, args: BaseModel, ctx: ToolContext, result: ToolResult
    ) -> ToolResult:
        del tool, args, ctx
        cut = result.content == TURN_CAP_OMITTED or result.content.endswith(TURN_CAP_NOTICE)
        if not cut or isinstance(result.staged, StagedErpChange):
            return result
        if result.is_error and result.error_category is not None:
            # Keep the failure's own category and retryability; replace only its text. See the
            # same rule in `session.from_tool_result`: a cut transient failure that came back
            # as `validation`/not-retryable would stop a caller retrying something it should.
            return ToolResult.error(
                result.error_category, PAGE_TOO_LARGE, retryable=result.is_retryable
            )
        return ToolResult.error(ErrorCategory.VALIDATION, PAGE_TOO_LARGE)


class CallAudit(Interceptor):
    """One log line per call: which tool, whether it failed, whether it staged. No payload."""

    async def after(
        self, tool: str, args: BaseModel, ctx: ToolContext, result: ToolResult
    ) -> ToolResult:
        del args, ctx
        category = None if result.error_category is None else result.error_category.value
        _log.info(
            "gateway call",
            extra={
                "tool": tool,
                "error_category": category,
                "is_retryable": result.is_retryable,
                "is_error": result.is_error,
                "staged": result.staged is not None,
            },
        )
        return result
