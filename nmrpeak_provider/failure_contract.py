"""Classify preparation failures before authorizing an exact API failure pair.

Adapted from Magnet's analysis_contract failure classes. The report field can
contain only provider-rendered diagnostics; model and runner prose stays at its
own boundary and cannot be forwarded through this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .failure_message import is_failure_message
from .text_provenance import ProviderDiagnosticText


_SAFE_CODE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", re.ASCII)


class FailureContractError(ValueError):
    """A classified failure or publication rule violates the local contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"preparation failure contract rejected: {reason}")


@dataclass(frozen=True, slots=True)
class ClassifiedPreparationFailure:
    """A determinate pre-execution cause, before public publication policy."""

    kind: str
    reported_message: ProviderDiagnosticText | None = None

    def __post_init__(self) -> None:
        if not _is_code(self.kind):
            raise FailureContractError("invalid_classified_failure_kind")
        if self.reported_message is not None and not is_failure_message(
            self.reported_message
        ):
            raise FailureContractError("invalid_classified_failure_message")


@dataclass(frozen=True, slots=True)
class PublishedPreparationFailure:
    """The exact public code and message authorized before journal retention."""

    failure_code: str
    failure_message: str

    def __post_init__(self) -> None:
        if not _is_code(self.failure_code) or len(self.failure_code) > 128:
            raise FailureContractError("invalid_failure_code")
        if not is_failure_message(self.failure_message):
            raise FailureContractError("invalid_failure_message")


@dataclass(frozen=True, slots=True)
class ReportedFailurePublication:
    """Authorize forwarding one reviewed, provider-rendered diagnostic."""

    failure_code: str

    def __post_init__(self) -> None:
        if not _is_code(self.failure_code) or len(self.failure_code) > 128:
            raise FailureContractError("invalid_reported_failure_code")


@dataclass(frozen=True, slots=True)
class PreparationFailureRule:
    """Map a classified cause to recovery, fixed text, or reviewed text."""

    kind: str
    publication: PublishedPreparationFailure | ReportedFailurePublication | None

    def __post_init__(self) -> None:
        if not _is_code(self.kind):
            raise FailureContractError("invalid_failure_rule_kind")
        if self.publication is not None and type(self.publication) not in {
            PublishedPreparationFailure,
            ReportedFailurePublication,
        }:
            raise FailureContractError("invalid_failure_publication")


@dataclass(frozen=True, slots=True)
class PreparationFailurePolicy:
    """The complete immutable disposition table for one NMRPeak lane."""

    rules: tuple[PreparationFailureRule, ...]

    def __post_init__(self) -> None:
        if type(self.rules) is not tuple or not self.rules or any(
            type(rule) is not PreparationFailureRule for rule in self.rules
        ):
            raise FailureContractError("invalid_failure_policy_rules")
        kinds = [rule.kind for rule in self.rules]
        if len(kinds) != len(set(kinds)):
            raise FailureContractError("duplicate_failure_policy_kind")

    def resolve(
        self, failure: ClassifiedPreparationFailure, /
    ) -> PublishedPreparationFailure | None:
        """Select the reviewed API pair, or `None` for local recovery."""

        if type(failure) is not ClassifiedPreparationFailure:
            raise FailureContractError("invalid_classified_failure")
        for rule in self.rules:
            if rule.kind != failure.kind:
                continue
            match rule.publication:
                case None:
                    return None
                case PublishedPreparationFailure():
                    return rule.publication
                case ReportedFailurePublication(failure_code=failure_code):
                    if failure.reported_message is None:
                        raise FailureContractError("reported_failure_message_missing")
                    return PublishedPreparationFailure(
                        failure_code, failure.reported_message
                    )
        raise FailureContractError("unknown_classified_failure_kind")


def _is_code(value: object) -> bool:
    return type(value) is str and _SAFE_CODE.fullmatch(value) is not None
