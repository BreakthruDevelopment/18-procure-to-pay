"""Environment configuration for the demo and the live Bedrock path.

Every value is read once, here. Nothing else in this package reads `os.environ`, so
what a run depends on is in one place. The Bedrock profile id is never a literal at
a call site.

The profile id has no default. The project records the model (Claude Sonnet 4.6),
the region (us-east-2) and that the Global Anthropic inference profile is the route;
the identifier itself is configuration. A live run without it is a validation
failure, and the replay path never asks.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from p2p.core.errors import ValidationFailed

#: Recorded in the track spec: the second AWS account, us-east-2.
DEFAULT_REGION = "us-east-2"

DATABASE_VAR = "P2P_DATABASE"
PROFILE_VAR = "P2P_BEDROCK_PROFILE_ID"
REGION_VAR = "P2P_REGION"
VERSION_VAR = "APP_VERSION"


@dataclass(frozen=True, slots=True)
class Config:
    """What a demo or health check reads from its environment."""

    database_path: Path | None
    region: str = DEFAULT_REGION
    bedrock_profile_id: str | None = None
    app_version: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        values = os.environ if env is None else env
        database = values.get(DATABASE_VAR, "").strip()
        profile = values.get(PROFILE_VAR, "").strip()
        region = values.get(REGION_VAR, "").strip() or DEFAULT_REGION
        version = values.get(VERSION_VAR, "").strip()
        return cls(
            database_path=Path(database) if database else None,
            region=region,
            bedrock_profile_id=profile or None,
            app_version=version or None,
        )

    def live_model_reference(self) -> str:
        """The model identifier for `--live`, or a validation failure if it is missing."""
        if self.bedrock_profile_id is None:
            raise ValidationFailed("P2P_BEDROCK_PROFILE_ID is required for a live run")
        return self.bedrock_profile_id
