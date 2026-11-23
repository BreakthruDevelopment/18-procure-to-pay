"""String redaction for app 17's `RecordingProvider`.

`Redacting` in app 17 is a tool decorator; the recorder takes a function. Fixtures
are synthetic, and this still strips the credential shapes a live recording might
copy into a committed file.
"""

from __future__ import annotations

import re

_AWS_ACCESS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(aws_secret_access_key|secret_access_key|secret-access-key)\s*[:=]\s*\S+"
)


def redact_recorded(text: str) -> str:
    """Replace credential-shaped strings before a reply is stored."""
    cleaned = _AWS_ACCESS_KEY.sub("AKIA****************", text)
    return _SECRET_ASSIGNMENT.sub(r"\1=[redacted]", cleaned)
