"""Bound provider-owned interpretation inputs and the overall model budget."""

from __future__ import annotations

from dataclasses import dataclass
import math


MAX_INTERPRETER_CONFIG_BYTES = 64 * 1024
MAX_INTERPRETER_ENDPOINTS = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterPolicy:
    interpretation_timeout_seconds: float

    def __post_init__(self) -> None:
        value = self.interpretation_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("interpretation_timeout_seconds must be positive and finite")
        object.__setattr__(self, "interpretation_timeout_seconds", float(value))
