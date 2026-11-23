"""Where a model reply comes from: committed recordings by default, Bedrock on `--live`.

Replay files are the `dump_replies` / `RecordingProvider` format from app 17 slice
17.3. A live run wraps `BedrockProvider` in `RateLimitedProvider` so a burst of
demo steps cannot trip throttling. boto3 is imported only on that path.
"""

from __future__ import annotations

from pathlib import Path

from agent_container.core.errors import ReplayFormatError
from agent_container.core.provider import ModelProvider
from agent_container.core.recorder import RecordingProvider
from agent_container.core.replay import ReplayProvider

from p2p.core.errors import ValidationFailed
from p2p.demo.config import Config
from p2p.demo.redact import redact_recorded


def replay_from(path: Path) -> ReplayProvider:
    """Load a committed recording. A missing or malformed file is a validation failure."""
    if not path.is_file():
        raise ValidationFailed("the recording could not be read")
    try:
        return ReplayProvider.from_file(path)
    except ReplayFormatError as exc:
        raise ValidationFailed("the recording could not be read") from exc


def recording(inner: ModelProvider) -> RecordingProvider:
    """Wrap a provider so its replies can be saved as a replay fixture."""
    return RecordingProvider(inner, redact=redact_recorded)


def live_provider(config: Config) -> ModelProvider:
    """Bedrock behind the in-process limiter. Imported only when `--live` is set."""
    from agent_container.core.bedrock import BedrockProvider
    from agent_container.core.limiter import RateLimitedProvider

    inner = BedrockProvider(
        model_id=config.live_model_reference(),
        region_name=config.region,
    )
    return RateLimitedProvider(inner)
