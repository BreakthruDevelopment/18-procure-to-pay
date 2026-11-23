"""Write a recording in app 17's fixture format, through `RecordingProvider`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from agent_container.core.messages import Message, ModelRequest, ModelResponse
from agent_container.core.replay import ReplayProvider

from p2p.demo.provider import recording


async def record_replies(replies: Sequence[ModelResponse]) -> dict[str, object]:
    """Run the replies through the recorder so the file is its output, not a hand copy."""
    inner = ReplayProvider(replies)
    recorder = recording(inner)
    dummy = ModelRequest(system="record", messages=(Message.user_text("record"),))
    for _ in replies:
        await recorder.converse(dummy)
    payload = recorder.to_dict()
    return {str(key): value for key, value in payload.items()}


def write_recording(path: Path, data: dict[str, object], *, description: str) -> None:
    body = {"description": description, **data}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
