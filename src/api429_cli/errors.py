from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class CLIError(Exception):
    """Base error that can be presented to a CLI user without a traceback."""

    exit_code = 1


class UsageError(CLIError):
    exit_code = 2


class ConfigurationError(CLIError):
    exit_code = 2


class TransportError(ConfigurationError):
    """A read-only request failed before a usable response was received."""


class AmbiguousRequestError(CLIError):
    """The request may have reached the server, so retrying could charge twice."""

    exit_code = 3

    def __init__(self, message: str, *, idempotency_key: str | None = None) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key


@dataclass(slots=True)
class APIError(CLIError):
    status_code: int
    message: str
    code: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    retry_after: str | None = None
    payload: Any = None
    exit_code: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        if self.status_code in {401, 403}:
            self.exit_code = 4
        elif self.status_code == 402:
            self.exit_code = 5
        elif self.status_code == 429:
            self.exit_code = 6
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        parts = [f"API error {self.status_code}: {self.message}"]
        if self.code:
            parts.append(f"code={self.code}")
        if self.trace_id:
            parts.append(f"trace_id={self.trace_id}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        if self.retry_after:
            parts.append(f"retry_after={self.retry_after}s")
        return (
            " (".join([parts[0], ", ".join(parts[1:]) + ")"])
            if len(parts) > 1
            else parts[0]
        )
