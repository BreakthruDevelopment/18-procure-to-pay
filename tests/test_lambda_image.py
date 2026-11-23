"""The Lambda image's `CMD` is the deployed entry point, so it is checked like code.

`30-apps-infra/stacks/aws` sets no `image_config`, which means the handler Lambda calls is
whatever `CMD` the image carries. Nothing else in this repo references that string: moving or
renaming `handler` would leave a green suite and a function that cannot start, and the break
would first appear as an `Unable to import module` in CloudWatch after a deploy. These tests
resolve the string the way the runtime interface client does, and then call it.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

Handler = Callable[[Mapping[str, object], object], dict[str, object]]


def _lambda_stage_cmd() -> str:
    """The single string in the `AS lambda` stage's `CMD`."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    parts = re.split(r"^FROM\s+\S+\s+AS\s+lambda\s*$", text, maxsplit=1, flags=re.MULTILINE)
    assert len(parts) == 2, "the Dockerfile declares no `AS lambda` stage"
    match = re.search(r'^CMD\s+\["([^"]+)"\]\s*$', parts[1], flags=re.MULTILINE)
    assert match is not None, "the `AS lambda` stage declares no CMD"
    return match.group(1)


def _resolve(target: str) -> Handler:
    module_name, _, attribute = target.rpartition(".")
    assert module_name, f"CMD {target!r} is not a module.attribute path"
    module = importlib.import_module(module_name)
    resolved = getattr(module, attribute, None)
    assert callable(resolved), f"CMD {target!r} does not name a callable"
    return cast("Handler", resolved)


def test_the_lambda_cmd_names_an_importable_callable() -> None:
    _resolve(_lambda_stage_cmd())


def test_the_lambda_cmd_handler_answers_with_the_health_payload() -> None:
    response = _resolve(_lambda_stage_cmd())({}, None)
    assert response["statusCode"] == 200
    body = json.loads(str(response["body"]))
    assert body["status"] == "ok"
    assert body["replay"] == "ok"
