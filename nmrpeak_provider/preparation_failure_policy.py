"""Parse each lane's complete reviewed preparation-failure policy.

The strict TOML shape and exhaustive kind table follow Magnet's E02 failure
policy parser; the kinds, public messages, and trust decisions belong to
NMRPeak's HF and CHF lanes.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
import tomllib

from .failure_contract import (
    PreparationFailurePolicy,
    PreparationFailureRule,
    PublishedPreparationFailure,
    ReportedFailurePublication,
)


class FailureKind(StrEnum):
    DIRECT_SOURCE_ISSUE = "direct_source_issue"
    DIRECT_RUNNER_REJECTED = "direct_runner_rejected"
    MODEL_REPORTED_PROBLEM = "model_reported_problem"
    CANDIDATE_ISSUE = "candidate_issue"
    CANDIDATE_RUNNER_REJECTED = "candidate_runner_rejected"


def parse_preparation_failure_policy(raw: bytes) -> PreparationFailurePolicy:
    """Admit a complete exact policy, rejecting ambiguous or missing rules."""

    if type(raw) is not bytes:
        raise ValueError("preparation failure policy must be bytes")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("preparation failure policy is not valid TOML") from None
    if type(document) is not dict or set(document) != {"failures"}:
        raise ValueError("preparation failure policy must contain only [failures]")
    failures = document["failures"]
    if type(failures) is not dict:
        raise ValueError("preparation failure policy [failures] must be a table")
    if set(failures) != {kind.value for kind in FailureKind}:
        raise ValueError("preparation failure policy must cover every failure kind")
    return PreparationFailurePolicy(
        tuple(_parse_rule(kind, failures[kind.value]) for kind in FailureKind)
    )


def load_preparation_failure_policy(lane_name: str) -> PreparationFailurePolicy:
    """Load one packaged HF or CHF policy during lane construction."""

    if lane_name not in {"hf", "chf"}:
        raise ValueError("unknown NMRPeak preparation failure policy lane")
    path = Path(__file__).with_name("policies") / f"{lane_name}_preparation_failures.toml"
    return parse_preparation_failure_policy(path.read_bytes())


def _parse_rule(kind: FailureKind, value: object) -> PreparationFailureRule:
    if type(value) is not dict or type(value.get("submit_failure_to_api")) is not bool:
        raise ValueError(f"preparation failure policy [{kind.value}] is invalid")
    if value["submit_failure_to_api"] is False:
        # Interpreter unavailability already has its own retryable outcome.
        # Every classified kind in this table is terminal by construction.
        raise ValueError(f"determinate failure [{kind.value}] must publish")

    fixed = {"submit_failure_to_api", "failure_code", "failure_message"}
    reported = {
        "submit_failure_to_api",
        "failure_code",
        "forward_failure_message",
    }
    if set(value) == fixed:
        return PreparationFailureRule(
            kind.value,
            PublishedPreparationFailure(value["failure_code"], value["failure_message"]),
        )
    if set(value) == reported and value["forward_failure_message"] is True:
        if kind not in {
            FailureKind.DIRECT_SOURCE_ISSUE,
            FailureKind.DIRECT_RUNNER_REJECTED,
            FailureKind.CANDIDATE_ISSUE,
        }:
            raise ValueError(f"unreviewed forwarding for [{kind.value}]")
        return PreparationFailureRule(
            kind.value, ReportedFailurePublication(value["failure_code"])
        )
    raise ValueError(f"terminal failure [{kind.value}] is invalid")
