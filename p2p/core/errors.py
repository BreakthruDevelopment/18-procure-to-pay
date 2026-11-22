"""The failures this app reports, each already carrying an app-17 `ErrorCategory`.

One exception class per category, and the category is a class attribute rather than a constructor
argument, so a caller cannot raise a `permission` failure as `validation` by accident. The adapter
(`p2p/core/adapter.py`) is the only place that turns one of these into a `ToolResult`, and the MCP
gateway (slice 18.3) is the only place that turns it into an MCP error payload; both read
`category` and `is_retryable` from here rather than deciding again.

**Messages are built from fixed strings and schema-derived names only, never from the caller's
input.** An id, a company code, a change key or a document's text in a message is that value
copied into somewhere it may be read by a different principal, logged, or shown to a model as
"content". Where the caller needs to know which field was wrong, name the field, not its value.
"""

from __future__ import annotations

from agent_container.core.tool import ErrorCategory


class P2PError(Exception):
    """Base for every failure this app reports. Never raised directly."""

    category: ErrorCategory
    is_retryable: bool = False


class AccessDenied(P2PError):
    """The acting principal may not do this, now or ever. Carries no row data."""

    category = ErrorCategory.PERMISSION


class ValidationFailed(P2PError):
    """The input was wrong and the caller can fix it: a malformed key, an impossible transition."""

    category = ErrorCategory.VALIDATION


class BusinessRule(P2PError):
    """A rule said no. Explain it; do not retry. Not for repeated validation failure."""

    category = ErrorCategory.BUSINESS


class InternalFailure(P2PError):
    """The app broke. Nothing the caller did, and no rule to explain."""

    category = ErrorCategory.INTERNAL


class TransientFailure(P2PError):
    """Might work if tried again. The only category whose default is retryable."""

    category = ErrorCategory.TRANSIENT
    is_retryable = True
