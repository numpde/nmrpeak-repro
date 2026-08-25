"""Define dependency-free timeout policy for bounded interpretation."""

from __future__ import annotations

from dataclasses import dataclass
import math


_HTTP_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAIChatCallPolicy:
    request_timeout_seconds: float
    turn_timeout_seconds: float

    def __post_init__(self) -> None:
        request_timeout = _positive_timeout(
            self.request_timeout_seconds,
            "request_timeout_seconds",
        )
        turn_timeout = _positive_timeout(
            self.turn_timeout_seconds,
            "turn_timeout_seconds",
        )
        if turn_timeout < _HTTP_ATTEMPTS * request_timeout + _RETRY_DELAY_SECONDS:
            raise ValueError(
                "turn_timeout_seconds must cover both request attempts and their "
                "retry delay"
            )
        object.__setattr__(self, "request_timeout_seconds", request_timeout)
        object.__setattr__(self, "turn_timeout_seconds", turn_timeout)


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterPolicy:
    call_policy: OpenAIChatCallPolicy
    interpretation_timeout_seconds: float

    def __post_init__(self) -> None:
        if type(self.call_policy) is not OpenAIChatCallPolicy:
            raise TypeError("Interpreter policy requires admitted call policy")
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
