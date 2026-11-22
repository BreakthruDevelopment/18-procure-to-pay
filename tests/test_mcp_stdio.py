"""Newline-delimited JSON-RPC, and the `p2p-erp` process boundary."""

from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path

import pytest
from pydantic import JsonValue

from p2p.core.access import Principal
from p2p.core.erp import Erp
from p2p.mcp.contract import ERROR_FIELDS, PREFERRED_PROTOCOL, SERVER_NAME, SERVER_VERSION
from p2p.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    ServerState,
    handle_line,
    internal_error_line,
    serve_lines,
)
from p2p.mcp.server import main
from p2p.mcp.session import GatewaySession


def _request(
    method: str,
    params: dict[str, JsonValue] | None = None,
    *,
    request_id: int | None = 1,
) -> str:
    message: dict[str, JsonValue] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def _object(line: str) -> dict[str, object]:
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def _reset_logging() -> None:
    import p2p.mcp.log as gateway_log

    gateway = logging.getLogger("p2p.mcp")
    for handler in list(gateway.handlers):
        gateway.removeHandler(handler)
        handler.close()
    gateway_log._installed = False


def test_initialize_echoes_a_supported_version_and_a_notification_is_silent(
    erp: Erp, clerk: Principal
) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    lines = serve_lines(
        state,
        [
            _request("initialize", {"protocolVersion": "2024-11-05"}),
            _request("notifications/initialized", request_id=None),
        ],
    )
    assert len(lines) == 1
    body = _object(lines[0])
    result = body["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == "2024-11-05"
    assert state.initialized is True
    info = result["serverInfo"]
    assert isinstance(info, dict)
    assert info["name"] == SERVER_NAME
    assert info["version"] == SERVER_VERSION


def test_an_unsupported_version_still_initializes_at_the_preferred_one(
    erp: Erp, clerk: Principal
) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    line = serve_lines(state, [_request("initialize", {"protocolVersion": "1999-01-01"})])[0]
    result = _object(line)["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == PREFERRED_PROTOCOL
    assert state.initialized is True


def test_a_non_string_version_is_invalid_and_does_not_initialize(
    erp: Erp, clerk: Principal
) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    line = serve_lines(state, [_request("initialize", {"protocolVersion": 1})])[0]
    error = _object(line)["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32602
    assert state.initialized is False


def test_initialize_without_an_id_changes_nothing(erp: Erp, clerk: Principal) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    assert serve_lines(state, ['{"jsonrpc":"2.0","method":"initialize"}']) == []
    assert state.initialized is False


def test_tools_call_before_initialize_is_an_rpc_error_and_echoes_nothing(
    erp: Erp, clerk: Principal
) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    line = handle_line(
        state,
        _request(
            "tools/call",
            {"name": "get_invoice", "arguments": {"invoice_id": "INV-2001"}},
        ),
    )
    assert line is not None
    error = _object(line)["error"]
    assert isinstance(error, dict)
    assert error["code"] == INVALID_REQUEST
    assert error["message"] == "server not initialized"
    assert "INV-2001" not in line


def test_a_parse_error_does_not_echo_the_body(erp: Erp, clerk: Principal) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    marker = "leak-this-body"
    line = handle_line(state, marker)
    assert line is not None
    error = _object(line)["error"]
    assert isinstance(error, dict)
    assert error["code"] == PARSE_ERROR
    assert _object(line)["id"] is None
    assert marker not in line


def test_an_unknown_method_and_a_bool_id_are_protocol_errors(erp: Erp, clerk: Principal) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    serve_lines(state, [_request("initialize", {"protocolVersion": PREFERRED_PROTOCOL})])
    unknown = handle_line(state, _request("tools/nope"))
    assert unknown is not None
    error = _object(unknown)["error"]
    assert isinstance(error, dict)
    assert error["code"] == METHOD_NOT_FOUND
    bad_id = handle_line(state, '{"jsonrpc":"2.0","id":true,"method":"ping"}')
    assert bad_id is not None
    assert _object(bad_id)["error"] == {"code": INVALID_REQUEST, "message": "invalid request"}
    assert "true" not in bad_id


def test_tool_success_and_tool_failure_are_both_json_rpc_results(
    erp: Erp, clerk: Principal
) -> None:
    state = ServerState(GatewaySession(erp, clerk))
    lines = serve_lines(
        state,
        [
            _request("initialize", {"protocolVersion": PREFERRED_PROTOCOL}, request_id=1),
            _request(
                "tools/call",
                {"name": "get_invoice", "arguments": {"invoice_id": "INV-1001"}},
                request_id=2,
            ),
            _request(
                "tools/call",
                {"name": "get_invoice", "arguments": {"invoice_id": "INV-2001"}},
                request_id=3,
            ),
        ],
    )
    success = _object(lines[1])
    failure = _object(lines[2])
    assert "error" not in success
    assert "error" not in failure
    for message, is_error in ((success, False), (failure, True)):
        result = message["result"]
        assert isinstance(result, dict)
        assert result["isError"] is is_error
        content = result["content"]
        assert isinstance(content, list)
        block = content[0]
        assert isinstance(block, dict)
        text = block["text"]
        assert isinstance(text, str)
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        assert json.loads(text) == structured
    ok_body = success["result"]
    assert isinstance(ok_body, dict)
    ok_structured = ok_body["structuredContent"]
    assert isinstance(ok_structured, dict)
    assert ok_structured["id"] == "INV-1001"
    failed_body = failure["result"]
    assert isinstance(failed_body, dict)
    failed_structured = failed_body["structuredContent"]
    assert isinstance(failed_structured, dict)
    assert tuple(sorted(failed_structured)) == ERROR_FIELDS
    assert failed_structured["error_category"] == "permission"
    assert failed_structured["is_retryable"] is False
    assert "INV-2001" not in lines[2]


def test_internal_error_line_carries_no_request() -> None:
    body = _object(internal_error_line())
    assert body["id"] is None
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == INTERNAL_ERROR
    assert error["message"] == "internal error"


def test_version_flag_prints_the_server_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"{SERVER_NAME} {SERVER_VERSION}"


def test_missing_arguments_exit_2() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_an_empty_subject_exits_2_with_a_fixed_sentence(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--database",
            str(db_path),
            "--subject",
            "",
            "--role",
            "clerk",
            "--company-code",
            "1000",
        ],
        inbound=StringIO(""),
        outbound=StringIO(),
    )
    assert code == 2
    assert capsys.readouterr().err.strip() == "a principal needs a subject and a company code"


def test_a_database_that_cannot_be_opened_names_neither_the_path_nor_the_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("x", encoding="utf-8")
    target = parent / "erp.sqlite3"
    outbound = StringIO()
    code = main(
        [
            "--database",
            str(target),
            "--subject",
            "clerk@demo",
            "--role",
            "clerk",
            "--company-code",
            "1000",
        ],
        inbound=StringIO(""),
        outbound=outbound,
    )
    assert code == 1
    assert outbound.getvalue() == ""
    err = capsys.readouterr().err
    assert "the database could not be opened" in err
    assert str(target) not in err
    assert "Traceback" not in err


def test_main_writes_only_json_rpc_and_logs_on_stderr(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _reset_logging()
    inbound = StringIO(
        "\n".join(
            (
                _request("initialize", {"protocolVersion": PREFERRED_PROTOCOL}, request_id=1),
                _request("notifications/initialized", request_id=None),
                _request(
                    "tools/call",
                    {"name": "get_invoice", "arguments": {"invoice_id": "INV-1001"}},
                    request_id=2,
                ),
            )
        )
        + "\n"
    )
    outbound = StringIO()
    code = main(
        [
            "--database",
            str(db_path),
            "--subject",
            "clerk@demo",
            "--role",
            "clerk",
            "--company-code",
            "1000",
        ],
        inbound=inbound,
        outbound=outbound,
    )
    assert code == 0
    lines = [line for line in outbound.getvalue().splitlines() if line]
    assert len(lines) == 2
    for line in lines:
        assert "gateway call" not in line
        assert _object(line)["jsonrpc"] == "2.0"
    err = capsys.readouterr().err
    assert "gateway call" in err
    assert "INV-1001" not in err
    assert "1001-A" not in err


def test_an_unexpected_failure_is_a_protocol_error_without_the_exception_text(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(state: ServerState, line: str) -> str:
        del state, line
        raise RuntimeError("secret-invoice-text")

    monkeypatch.setattr("p2p.mcp.server.handle_line", explode)
    _reset_logging()
    outbound = StringIO()
    code = main(
        [
            "--database",
            str(db_path),
            "--subject",
            "clerk@demo",
            "--role",
            "clerk",
            "--company-code",
            "1000",
        ],
        inbound=StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
        outbound=outbound,
    )
    assert code == 0
    body = _object(outbound.getvalue().strip())
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == INTERNAL_ERROR
    assert error["message"] == "internal error"
    assert "secret-invoice-text" not in outbound.getvalue()
    err = capsys.readouterr().err
    assert "secret-invoice-text" not in err
    assert "RuntimeError" in err
