"""Newline-delimited JSON-RPC for the MCP stdio transport.

One message is one JSON object on one line. A notification (no `id`) gets no response.
A tool failure is a successful JSON-RPC response whose result has `isError` true and the
frozen error object in both `structuredContent` and the text content. A protocol failure
is a JSON-RPC error and carries no ERP data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from agent_container.core.tool import AnyTool, Effect
from pydantic import JsonValue

from p2p.mcp.contract import (
    PREFERRED_PROTOCOL,
    PROTOCOL_VERSIONS,
    SERVER_NAME,
    SERVER_VERSION,
)
from p2p.mcp.operations import OUTPUT_MODELS
from p2p.mcp.session import GatewayResult, GatewaySession

#: A single line longer than this is refused without being parsed. Tool arguments are a
#: page of ids, not a document; a document belongs to a later slice's own boundary.
MAX_MESSAGE_CHARS: Final = 1_000_000

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603


@dataclass
class ServerState:
    session: GatewaySession
    initialized: bool = False
    protocol: str = PREFERRED_PROTOCOL


def serve_lines(state: ServerState, lines: list[str]) -> list[str]:
    """Handle already-split lines. Tests use this; the server reads a stream."""
    responses: list[str] = []
    for line in lines:
        response = handle_line(state, line)
        if response is not None:
            responses.append(response)
    return responses


def handle_line(state: ServerState, line: str) -> str | None:
    """One inbound line. `None` means the client is owed no response."""
    if line.strip() == "":
        return None
    if len(line) > MAX_MESSAGE_CHARS:
        return _encode(_rpc_error(None, PARSE_ERROR, "parse error"))
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return _encode(_rpc_error(None, PARSE_ERROR, "parse error"))
    response = handle_message(state, message)
    if response is None:
        return None
    return _encode(response)


def handle_message(state: ServerState, message: object) -> dict[str, JsonValue] | None:
    if not isinstance(message, dict):
        return _rpc_error(None, INVALID_REQUEST, "invalid request")
    request_id = _request_id(message)
    if request_id is _BAD_ID:
        return _rpc_error(None, INVALID_REQUEST, "invalid request")
    version = message.get("jsonrpc")
    method = message.get("method")
    if version != "2.0" or not isinstance(method, str) or method == "":
        if request_id is _NO_ID:
            return None
        return _rpc_error(_id_value(request_id), INVALID_REQUEST, "invalid request")
    if request_id is _NO_ID:
        # A notification gets no response, and it changes nothing. `initialize` is a
        # request: it carries an id, and that is what marks the session ready.
        return None
    response_id = _id_value(request_id)
    if method == "initialize":
        return _initialize(state, response_id, message.get("params"))
    if not state.initialized:
        return _rpc_error(response_id, INVALID_REQUEST, "server not initialized")
    if method == "ping":
        return _result(response_id, {})
    if method == "tools/list":
        return _tools_list(state, response_id, message.get("params"))
    if method == "tools/call":
        return _tools_call(state, response_id, message.get("params"))
    return _rpc_error(response_id, METHOD_NOT_FOUND, "method not found")


def _initialize(
    state: ServerState, request_id: str | int | None, params: object
) -> dict[str, JsonValue]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, INVALID_PARAMS, "invalid params")
    offered = PREFERRED_PROTOCOL
    version = params.get("protocolVersion")
    if isinstance(version, str) and version in PROTOCOL_VERSIONS:
        offered = version
    elif version is not None and not isinstance(version, str):
        return _rpc_error(request_id, INVALID_PARAMS, "invalid params")
    state.initialized = True
    state.protocol = offered
    return _result(
        request_id,
        {
            "protocolVersion": offered,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def _tools_list(
    state: ServerState, request_id: str | int | None, params: object
) -> dict[str, JsonValue]:
    if params is not None and not isinstance(params, dict):
        return _rpc_error(request_id, INVALID_PARAMS, "invalid params")
    if isinstance(params, dict) and "cursor" in params:
        cursor = params["cursor"]
        if not isinstance(cursor, str) or cursor != "":
            return _rpc_error(request_id, INVALID_PARAMS, "invalid params")
    tools: list[JsonValue] = [
        _descriptor(state.session.tools[name]) for name in state.session.visible_names()
    ]
    return _result(request_id, {"tools": tools})


def _tools_call(
    state: ServerState, request_id: str | int | None, params: object
) -> dict[str, JsonValue]:
    if not isinstance(params, dict):
        return _rpc_error(request_id, INVALID_PARAMS, "invalid params")
    name = params.get("name")
    if not isinstance(name, str):
        return _rpc_error(request_id, INVALID_PARAMS, "invalid params")
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    parsed = _json_object(arguments)
    if parsed is None:
        return _rpc_error(request_id, INVALID_PARAMS, "invalid params")
    outcome = state.session.call(name, parsed)
    return _result(request_id, _tool_result(outcome))


def _descriptor(tool: AnyTool) -> dict[str, JsonValue]:
    output = OUTPUT_MODELS[tool.name].model_json_schema()
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": cast(dict[str, JsonValue], dict(tool.input_schema())),
        "outputSchema": cast(dict[str, JsonValue], output),
        "annotations": {"readOnlyHint": tool.effect == Effect.READ},
    }


def _tool_result(outcome: GatewayResult) -> dict[str, JsonValue]:
    return {
        "content": [{"type": "text", "text": outcome.text}],
        "isError": outcome.is_error,
        "structuredContent": dict(outcome.body),
    }


def _json_object(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return None
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, JsonValue], parsed)


class _Marker:
    pass


_NO_ID = _Marker()
_BAD_ID = _Marker()


def _request_id(message: Mapping[str, object]) -> str | int | _Marker | None:
    if "id" not in message:
        return _NO_ID
    value = message["id"]
    if value is None or isinstance(value, str):
        return value
    # `bool` is an `int` subclass, and it is not a JSON-RPC id.
    if isinstance(value, bool) or not isinstance(value, int):
        return _BAD_ID
    return value


def _id_value(request_id: str | int | _Marker | None) -> str | int | None:
    if isinstance(request_id, _Marker):
        return None
    return request_id


def _result(request_id: str | int | None, result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: str | int | None, code: int, message: str) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _encode(payload: Mapping[str, JsonValue]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def internal_error_line() -> str:
    """A protocol error that names nothing about the request that caused it.

    The server uses this when a line handler raises. The request body and the exception
    text are both unreviewed, so neither is copied into the response or its id.
    """
    return _encode(_rpc_error(None, INTERNAL_ERROR, "internal error"))
