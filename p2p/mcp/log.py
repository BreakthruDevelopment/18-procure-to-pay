"""JSON-lines logging for the gateway. Fixed fields, and never a payload.

The server installs the handler on stderr. stdout is the MCP stream; a log line there
would desynchronize the client. The handler is not installed at import: a library user
decides where logs go, and a `NullHandler` keeps an unconfigured logger from writing to
stderr through the root logger's last resort.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Final, TextIO

LOG_FIELDS: Final[tuple[str, ...]] = (
    "tool",
    "error_category",
    "is_retryable",
    "is_error",
    "staged",
    "failure",
)

_LOGGER_NAME: Final = "p2p.mcp"
_installed = False


class JsonLineFormatter(logging.Formatter):
    """One JSON object per record, limited to the fixed fields. Nothing else on the record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {"level": record.levelname, "message": record.getMessage()}
        for name in LOG_FIELDS:
            if name in record.__dict__:
                payload[name] = record.__dict__[name]
        return json.dumps(payload, sort_keys=True)


def logger() -> logging.Logger:
    gateway = logging.getLogger(_LOGGER_NAME)
    if not any(isinstance(handler, logging.NullHandler) for handler in gateway.handlers):
        gateway.addHandler(logging.NullHandler())
    return gateway


def configure_stderr_logging(stream: TextIO | None = None) -> None:
    """Attach one JSON-lines handler to stderr. A second call does not add another."""
    global _installed
    gateway = logger()
    gateway.setLevel(logging.INFO)
    if _installed:
        return
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    gateway.addHandler(handler)
    _installed = True
