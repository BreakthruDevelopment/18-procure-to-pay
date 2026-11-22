"""The MCP gateway over the simulated ERP (slice 18.3).

`p2p-erp` is a stdio MCP server. It publishes the six read tools and the three staged
write tools from the spec's gateway table. Reads are company-code scoped inside `p2p/core`
and come back capped and paged. Writes record a staged change through app 17's
`PermissionGate` and return its `change_key`; they do not apply it.

This package adds files only. It calls `p2p/core` and does not edit it.
"""

from p2p.mcp.contract import (
    ERROR_FIELDS,
    READ_TOOLS,
    SERVER_NAME,
    TOOL_NAMES,
    WRITE_FIELDS,
    WRITE_TOOLS,
)
from p2p.mcp.manifest import manifest_for, tool_choice_for
from p2p.mcp.session import GatewaySession

__all__ = (
    "ERROR_FIELDS",
    "READ_TOOLS",
    "SERVER_NAME",
    "TOOL_NAMES",
    "WRITE_FIELDS",
    "WRITE_TOOLS",
    "GatewaySession",
    "manifest_for",
    "tool_choice_for",
)
