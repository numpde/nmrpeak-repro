"""Bind each fixed API offering to its private NMRPeak input projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .chf_binding import ChfRunnerInput, bind_chf_runner_input
from .hf_binding import HfRunnerInput, bind_hf_runner_input
from .product import AnalysisOffering, CHF_OFFERING, HF_OFFERING
from .product_input import ChfModelInput, HfModelInput
from .runner_protocol import RunnerModelInput


ParsedInput = TypeVar("ParsedInput")
BoundInput = TypeVar("BoundInput", bound=RunnerModelInput)


@dataclass(frozen=True, slots=True)
class LifecycleLane(Generic[ParsedInput, BoundInput]):
    """The scientific input boundary needed by one shared Attempt lifecycle."""

    offering: AnalysisOffering
    bind_runner_input: Callable[[ParsedInput], BoundInput]


HF_LIFECYCLE_LANE = LifecycleLane[HfModelInput, HfRunnerInput](
    offering=HF_OFFERING,
    bind_runner_input=bind_hf_runner_input,
)
CHF_LIFECYCLE_LANE = LifecycleLane[ChfModelInput, ChfRunnerInput](
    offering=CHF_OFFERING,
    bind_runner_input=bind_chf_runner_input,
)
