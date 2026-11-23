"""`GET /health`: version and a replay round-trip. Opens no ERP connection.

A first `Erp.open` applies schema and seed, which is a write, and Lambda's
filesystem is read-only outside `/tmp`. This payload therefore talks only to
app 17's recorder and replayer.

**It answers, it never raises.** The round-trip's failures come from two
exception trees: this app's `P2PError`, and app 17's `AgentContainerError` —
`ReplayExhausted` and `ReplayFormatError` descend from `RuntimeError` and
`ValueError`, not from `P2PError`, so catching only the latter let a short or
malformed recording escape as an unhandled Lambda error instead of the
documented `status: error` body with a 500. The live smoke reads that body, so
a crash there is a deploy check that reports nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from agent_container.core.errors import AgentContainerError
from agent_container.core.messages import Message, ModelRequest
from agent_container.core.replay import ReplayProvider

from p2p import __version__
from p2p.core.errors import P2PError, ValidationFailed
from p2p.demo.config import Config
from p2p.demo.fixtures import demo_fixture
from p2p.demo.provider import recording, replay_from

_PING = ModelRequest(system="health-replay", messages=(Message.user_text("ping"),))


def reported_version(config: Config | None = None) -> str:
    """The image tag when `APP_VERSION` is set, otherwise the package version."""
    loaded = config if config is not None else Config.from_env()
    if loaded.app_version is not None:
        return loaded.app_version
    return __version__


def replay_round_trip() -> None:
    """Record the committed health reply and replay it. Raises if the two disagree."""
    asyncio.run(_round_trip())


async def _round_trip() -> None:
    inner = replay_from(demo_fixture("health.json"))
    recorder = recording(inner)
    first = await recorder.converse(_PING)
    dumped = recorder.to_dict()
    replayed = ReplayProvider.from_dict({str(key): value for key, value in dumped.items()})
    second = await replayed.converse(_PING)
    if first.stop_reason != second.stop_reason or first.text != second.text:
        raise ValidationFailed("the health replay round-trip did not pass")
    if replayed.remaining != 0:
        raise ValidationFailed("the health replay round-trip did not pass")


def health_payload(config: Config | None = None) -> dict[str, str]:
    """JSON object for `GET /health`. Never opens the ERP."""
    try:
        replay_round_trip()
    except (P2PError, AgentContainerError):
        return {
            "status": "error",
            "version": reported_version(config),
            "replay": "failed",
        }
    return {
        "status": "ok",
        "version": reported_version(config),
        "replay": "ok",
    }


def handler(event: Mapping[str, Any], context: object) -> dict[str, object]:
    """AWS Lambda entry. The function *is* `/health`; the event is not interpreted."""
    del event, context
    body = health_payload()
    status = 200 if body["status"] == "ok" else 500
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }
