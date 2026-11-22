"""The tool list and the error shape app 19 connects to, checked against the running server.

Comparing the constants to themselves would stay green if the server drifted. These tests
ask the server and read the payloads it actually returns.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from agent_container.core.interceptors import TURN_CAP_NOTICE, TURN_CAP_OMITTED
from agent_container.core.tool import ErrorCategory, ToolResult
from pydantic import JsonValue

from p2p.core.access import Principal
from p2p.core.erp import Erp, InvoiceState
from p2p.mcp.contract import (
    APPROVER_TOOLS,
    ARGUMENT_FIELDS,
    CLERK_TOOLS,
    DISPUTES_TOOLS,
    ERROR_CATEGORIES,
    ERROR_FIELDS,
    PAGE_TOO_LARGE,
    READ_TOOLS,
    REQUIRED_ARGUMENTS,
    SERVICE_TOOLS,
    TOOL_NAMES,
    WRITE_FIELDS,
    WRITE_TOOLS,
)
from p2p.mcp.operations import OPERATIONS, OUTPUT_MODELS
from p2p.mcp.protocol import ServerState, serve_lines
from p2p.mcp.session import GatewaySession, from_tool_result

_FIVE = ("transient", "validation", "permission", "business", "internal")


def _request(
    method: str,
    params: dict[str, JsonValue] | None = None,
    *,
    request_id: int = 1,
) -> str:
    message: dict[str, JsonValue] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def _object(line: str) -> dict[str, object]:
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return cast(dict[str, object], parsed)


def _listed(erp: Erp, principal: Principal) -> list[dict[str, object]]:
    state = ServerState(GatewaySession(erp, principal))
    lines = serve_lines(
        state,
        [
            _request("initialize", {"protocolVersion": "2025-03-26"}, request_id=1),
            _request("tools/list", request_id=2),
        ],
    )
    listed = _object(lines[1])
    result = listed["result"]
    assert isinstance(result, dict)
    tools = result["tools"]
    assert isinstance(tools, list)
    out: list[dict[str, object]] = []
    for tool in tools:
        assert isinstance(tool, dict)
        out.append(cast(dict[str, object], tool))
    return out


def _match(erp: Erp, clerk: Principal) -> None:
    erp.advance_invoice(clerk, "INV-1001", InvoiceState.EXTRACTED)
    erp.advance_invoice(clerk, "INV-1001", InvoiceState.MATCHED)


def test_error_categories_are_app_17s_five_and_this_module_adds_none() -> None:
    assert ERROR_CATEGORIES == _FIVE
    assert tuple(category.value for category in ErrorCategory) == ERROR_CATEGORIES


def test_operations_and_output_models_follow_the_published_tool_list() -> None:
    assert tuple(operation.name for operation in OPERATIONS) == TOOL_NAMES
    assert tuple(OUTPUT_MODELS) == TOOL_NAMES


def test_from_tool_result_keeps_the_category_and_whether_it_is_retryable(
    erp: Erp, clerk: Principal
) -> None:
    tool = GatewaySession(erp, clerk).tool("get_invoice")
    expectations = (
        (ErrorCategory.TRANSIENT, True),
        (ErrorCategory.VALIDATION, False),
        (ErrorCategory.PERMISSION, False),
        (ErrorCategory.BUSINESS, False),
        (ErrorCategory.INTERNAL, False),
    )
    for category, retryable in expectations:
        outcome = from_tool_result(
            tool,
            ToolResult.error(category, "a fixed sentence", retryable=retryable),
        )
        assert outcome.is_error is True
        assert outcome.error_category == category.value
        assert outcome.is_retryable is retryable
        assert tuple(sorted(outcome.body)) == ERROR_FIELDS
        assert outcome.body["message"] == "a fixed sentence"
        assert json.loads(outcome.text) == outcome.body


def test_a_result_that_is_not_one_json_object_becomes_internal_and_is_not_echoed(
    erp: Erp, clerk: Principal
) -> None:
    tool = GatewaySession(erp, clerk).tool("get_invoice")
    outcome = from_tool_result(tool, ToolResult.ok("not-json"))
    assert outcome.error_category == "internal"
    assert outcome.is_retryable is False
    assert "not-json" not in outcome.text


def test_a_cut_page_is_replaced_and_a_cut_write_keeps_the_staged_key(
    erp: Erp, clerk: Principal
) -> None:
    read = GatewaySession(erp, clerk).tool("get_invoice")
    leaked = "INV-2001 secret row"
    cut = from_tool_result(
        read,
        ToolResult.error(ErrorCategory.PERMISSION, leaked + TURN_CAP_NOTICE),
    )
    assert cut.error_category == "validation"
    assert cut.is_retryable is False
    assert cut.body["message"] == PAGE_TOO_LARGE
    assert leaked not in cut.text

    _match(erp, clerk)
    session = GatewaySession(erp, clerk)
    posted = session.call("post_invoice", {"invoice_id": "INV-1001", "payload": {}})
    assert posted.staged is not None
    rebuilt = from_tool_result(
        session.tool("post_invoice"),
        ToolResult.ok(TURN_CAP_OMITTED, staged=posted.staged),
    )
    assert rebuilt.is_error is False
    assert rebuilt.body["change_key"] == "pst_000001"
    assert tuple(sorted(rebuilt.body)) == WRITE_FIELDS
    assert TURN_CAP_OMITTED not in rebuilt.text
    assert json.loads(rebuilt.text) == rebuilt.body


def test_tools_list_is_the_role_contract_in_contract_order(
    erp: Erp,
    clerk: Principal,
    approver: Principal,
    disputes: Principal,
    service: Principal,
) -> None:
    expected = (
        (service, SERVICE_TOOLS),
        (approver, APPROVER_TOOLS),
        (clerk, CLERK_TOOLS),
        (disputes, DISPUTES_TOOLS),
    )
    for principal, names in expected:
        listed = _listed(erp, principal)
        assert tuple(tool["name"] for tool in listed) == names


def test_each_published_schema_matches_the_frozen_fields(
    erp: Erp, clerk: Principal, service: Principal
) -> None:
    by_role: Mapping[str, list[dict[str, object]]] = {
        "clerk": _listed(erp, clerk),
        "service": _listed(erp, service),
    }
    for listed in by_role.values():
        for tool in listed:
            name = tool["name"]
            assert isinstance(name, str)
            schema = tool["inputSchema"]
            assert isinstance(schema, dict)
            properties = schema["properties"]
            assert isinstance(properties, dict)
            assert tuple(sorted(str(key) for key in properties)) == ARGUMENT_FIELDS[name]
            required = schema.get("required", [])
            assert isinstance(required, list)
            assert tuple(required) == REQUIRED_ARGUMENTS[name]
            assert schema["additionalProperties"] is False
            output = tool["outputSchema"]
            assert isinstance(output, dict)
            annotations = tool["annotations"]
            assert isinstance(annotations, dict)
            assert annotations["readOnlyHint"] is (name in READ_TOOLS)
            if name in WRITE_TOOLS:
                assert tuple(output["required"]) == WRITE_FIELDS
