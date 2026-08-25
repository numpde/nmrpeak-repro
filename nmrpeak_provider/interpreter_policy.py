"""Define dependency-free limits and timeout policy for interpretation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math


MAX_INTERPRETER_CONFIG_BYTES = 64 * 1024
MAX_INTERPRETER_ENDPOINTS = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAIChatCallPolicy:
    request_timeout_seconds: float
    turn_timeout_seconds: float
    maximum_attempts: int = field(default=2, init=False)
    retry_delay_seconds: float = field(default=1.0, init=False)

    def __post_init__(self) -> None:
        request_timeout = _positive_timeout(
            self.request_timeout_seconds,
            "request_timeout_seconds",
        )
        turn_timeout = _positive_timeout(
            self.turn_timeout_seconds,
            "turn_timeout_seconds",
        )
        minimum_turn_timeout = (
            self.maximum_attempts * request_timeout
            + (self.maximum_attempts - 1) * self.retry_delay_seconds
        )
        if turn_timeout < minimum_turn_timeout:
            raise ValueError(
                "turn_timeout_seconds must cover every request attempt and retry delay"
            )
        object.__setattr__(self, "request_timeout_seconds", request_timeout)
        object.__setattr__(self, "turn_timeout_seconds", turn_timeout)


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterPolicy:
    call_policy: OpenAIChatCallPolicy
    interpretation_timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "interpretation_timeout_seconds",
            _positive_timeout(
                self.interpretation_timeout_seconds,
                "interpretation_timeout_seconds",
            ),
        )


def _positive_timeout(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be positive and finite")
    try:
        valid = math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be positive and finite")
    return float(value)
