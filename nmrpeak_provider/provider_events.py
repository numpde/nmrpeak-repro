"""Closed operational facts emitted by the interpreter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterEndpointFailed:
    """One configured endpoint produced no usable interpretation."""

    configuration_id: str
    failure_kind: str
    failure_reason: str
    failure_state: str | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_code: str | None = None
    request_id: str | None = None
