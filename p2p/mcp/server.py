"""The `p2p-erp` stdio server.

One process is one session: one database, one principal, and optionally one step. The
principal is not a credential. It is the subject, role and company code the process acts
as, the same three a command invocation carries.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Sequence
from typing import TextIO

from p2p.core.access import Principal, Role
from p2p.core.erp import Erp
from p2p.core.errors import P2PError
from p2p.mcp.contract import SERVER_NAME, SERVER_VERSION, STEP_NAMES
from p2p.mcp.log import configure_stderr_logging
from p2p.mcp.protocol import ServerState, handle_line, internal_error_line
from p2p.mcp.session import GatewaySession

_log = logging.getLogger("p2p.mcp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description="MCP gateway over the simulated ERP, on stdio.",
    )
    parser.add_argument("--version", action="version", version=f"{SERVER_NAME} {SERVER_VERSION}")
    parser.add_argument("--database", required=True, help="Path to the ERP sqlite file.")
    parser.add_argument("--subject", required=True, help="Who the session acts as.")
    parser.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in Role],
        help="The role the session acts as.",
    )
    parser.add_argument(
        "--company-code",
        required=True,
        help="The company code the session is scoped to.",
    )
    parser.add_argument(
        "--step",
        choices=STEP_NAMES,
        default=None,
        help="Limit the session to one step's tool manifest.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    inbound: TextIO | None = None,
    outbound: TextIO | None = None,
) -> int:
    """Serve MCP on stdio until the client closes its stream. Logs go to stderr."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_stderr_logging()
    try:
        principal = Principal(args.subject, Role(args.role), args.company_code)
    except ValueError:
        print("a principal needs a subject and a company code", file=sys.stderr)
        return 2
    try:
        erp_context = Erp.open(args.database)
    except (OSError, sqlite3.Error):
        print("the database could not be opened", file=sys.stderr)
        return 1
    except P2PError as failure:
        print(f"{failure.category.value}: {failure}", file=sys.stderr)
        return 1
    reader = sys.stdin if inbound is None else inbound
    writer = sys.stdout if outbound is None else outbound
    with erp_context as erp:
        session = GatewaySession(erp, principal, step=args.step)
        state = ServerState(session)
        for line in reader:
            try:
                response = handle_line(state, line)
            except Exception as failure:
                # The line and the exception message can both carry a tool argument.
                # The type is enough, and the client gets a protocol error with no detail.
                _log.error(
                    "gateway call failed",
                    extra={
                        "tool": "",
                        "error_category": "internal",
                        "is_retryable": False,
                        "is_error": True,
                        "staged": False,
                        "failure": type(failure).__name__,
                    },
                )
                response = internal_error_line()
            if response is None:
                continue
            writer.write(response)
            writer.write("\n")
            writer.flush()
    return 0
