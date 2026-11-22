"""The permission gate in front of a staged ERP write.

App 17's `PermissionGate` with policy `staged_write` records the call and does not run the
tool. These tools *are* the recording: `stage_posting`, `stage_credit_memo` and
`stage_payment_hold` insert the staged row and return it, and the row's key is the only
handle an approver has. `StagedChange` has no id. Not running the tool would hand the
caller a staged change from which the key cannot be recovered, which the spec forbids,
and applying that outer change later would only stage another one.

So this gate is app 17's `PermissionGate` -- it advertises `gates_writes`, it is constructed
with `staged_write`, and a loop that is handed the tool passes the construction check --
and its `run` performs the denial check and then runs the tool. Running the tool records
the row. It does not apply it. `apply()` on the returned `StagedErpChange` is the apply
path, and it refuses the principal that staged the change.

`requires`, when set, is a permission the acting identity must hold. The session does not
set it: the tool is bound to one principal, and `assert_can` inside the accessor is the
authorization. The context's identity does not choose who the tool acts as.
"""

from __future__ import annotations

from agent_container.core.gate import PermissionGate, Policy
from agent_container.core.tool import ErrorCategory, Tool, ToolContext, ToolResult
from pydantic import BaseModel

from p2p.mcp.contract import NOT_PERMITTED


class StagedWriteGate[A: BaseModel](PermissionGate[A]):
    """`PermissionGate` over a tool whose own result is the staged ERP change."""

    def __init__(self, inner: Tool[A], *, requires: str | None = None) -> None:
        super().__init__(inner, Policy.STAGED_WRITE, requires=requires)

    async def run(self, args: A, ctx: ToolContext) -> ToolResult:
        if self.policy == Policy.DENY or (
            self.requires is not None and self.requires not in ctx.identity.permissions
        ):
            return ToolResult.error(ErrorCategory.PERMISSION, NOT_PERMITTED)
        return await self._inner.run(args, ctx)
