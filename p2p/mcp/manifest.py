"""Per-step tool manifests and `tool_choice` (statement 2.3).

A step names the gateway tools a model may call during it, and the `ToolChoice` the model
request carries. 18.4, 18.5 and 18.6 own the steps' work. They do not own this list: a
tool outside the active step's manifest cannot be called, and that refusal is an
interceptor in `p2p.mcp.interceptors`, not a line in each later slice.

`force` is how a later slice names one tool the model must call -- 18.4's schema tool,
which is not an ERP tool and so is not in any manifest here. The name is checked for
shape and is not required to be one of ours.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agent_container.core.messages import ToolChoice
from agent_container.core.tool import TOOL_NAME_PATTERN

from p2p.core.errors import ValidationFailed
from p2p.mcp.contract import (
    FORCED_TOOL_INVALID,
    NO_SUCH_STEP,
    STEP_NAMES,
    STEP_TOOL_CHOICE,
    STEP_TOOLS,
)

STEP_SUMMARY: Final[dict[str, str]] = {
    "extract": "Read the invoice document. No ERP tool is available at this step.",
    "fetch_po": "Fetch the purchase order the invoice names.",
    "fetch_receipt": "Fetch the goods receipt for that purchase order.",
    "decide": (
        "Compare the invoice, the order and the receipt, and stage a posting only when "
        "the step's rules say to."
    ),
    "dispute": (
        "Read supplier, invoice and payment state, and stage a credit or a hold when "
        "the case calls for one."
    ),
}


@dataclass(frozen=True, slots=True)
class StepManifest:
    """One step: the tools it may call, and the choice it sends when nobody overrides it."""

    name: str
    summary: str
    tools: tuple[str, ...]
    tool_choice: ToolChoice


def _choice(step: str) -> ToolChoice:
    mode, name = STEP_TOOL_CHOICE[step]
    if mode == "tool":
        return ToolChoice(mode="tool", name=name)
    if mode == "any":
        return ToolChoice(mode="any")
    return ToolChoice(mode="auto")


MANIFESTS: Final[dict[str, StepManifest]] = {
    step: StepManifest(
        name=step,
        summary=STEP_SUMMARY[step],
        tools=STEP_TOOLS[step],
        tool_choice=_choice(step),
    )
    for step in STEP_NAMES
}


def manifest_for(step: str) -> StepManifest:
    """The manifest named `step`. An unknown step is a validation failure, with no echo."""
    found = MANIFESTS.get(step)
    if found is None:
        raise ValidationFailed(NO_SUCH_STEP)
    return found


def tool_choice_for(
    step: str,
    available: Sequence[str],
    *,
    force: str | None = None,
) -> ToolChoice:
    """The `ToolChoice` a model request for `step` should carry.

    `force` names one tool the model must call. It is not looked up in the manifest: the
    schema tool 18.4 forces is not an ERP tool. Its shape is checked, and the value is
    not repeated when the shape is wrong.

    A step whose own choice names a tool this caller does not hold falls back to `auto`
    over the tools the caller does hold, rather than advertising a call that cannot be made.
    """
    manifest = manifest_for(step)
    if force is not None:
        if TOOL_NAME_PATTERN.match(force) is None:
            raise ValidationFailed(FORCED_TOOL_INVALID)
        return ToolChoice(mode="tool", name=force)
    choice = manifest.tool_choice
    if choice.mode == "tool" and choice.name not in available:
        return ToolChoice(mode="auto")
    return choice
