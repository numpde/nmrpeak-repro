"""Resolve durable Attempt records against one admitted two-lane generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .attempt_identity import derive_provider_attempt_key
from .attempt_journal import (
    ActiveAttempt,
    AttemptJournalRecord,
    StartPending,
    TerminalPending,
    validate_frozen_generation_id,
)
from .chf_runner_protocol import CHF_RUNNER_CODEC, CHF_RUNNER_CONTRACT_ID
from .hf_runner_protocol import HF_RUNNER_CODEC, HF_RUNNER_CONTRACT_ID
from .lifecycle_lane import (
    CHF_LIFECYCLE_LANE,
    HF_LIFECYCLE_LANE,
    LifecycleLane,
)
from .product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    ProviderResultFacts,
    ResultLaneIdentity,
)
from .run_generation import RunGenerationIdentity, run_generation_fingerprint
from .runner_protocol import RunnerFrameCodec


class GenerationRuntimeRejected(ValueError):
    """Admitted generation facts cannot resolve one durable obligation."""


@dataclass(frozen=True, slots=True)
class GenerationLane:
    """The exact lifecycle and runner composition for one admitted lane."""

    lane: LifecycleLane
    generation: RunGenerationIdentity
    result_facts: ProviderResultFacts
    runner_codec: RunnerFrameCodec[Any]


@dataclass(frozen=True, slots=True)
class GenerationRuntime:
    """Both fixed lane compositions named by one journal generation reference."""

    frozen_generation_id: str
    hf: GenerationLane
    chf: GenerationLane

    def __post_init__(self) -> None:
        validate_frozen_generation_id(self.frozen_generation_id)
        _require_lane(
            self.hf,
            lane=HF_LIFECYCLE_LANE,
            result_identity=HF_RESULT_IDENTITY,
            runner_contract_id=HF_RUNNER_CONTRACT_ID,
            runner_codec=HF_RUNNER_CODEC,
        )
        _require_lane(
            self.chf,
            lane=CHF_LIFECYCLE_LANE,
            result_identity=CHF_RESULT_IDENTITY,
            runner_contract_id=CHF_RUNNER_CONTRACT_ID,
            runner_codec=CHF_RUNNER_CODEC,
        )
        if self.hf.generation.provider_ref != self.chf.generation.provider_ref:
            raise GenerationRuntimeRejected(
                "Frozen generation lanes do not share one provider identity"
            )

    def resolve(self, record: AttemptJournalRecord) -> GenerationLane:
        """Return the sole admitted lane whose generation owns this Attempt key."""

        if type(record) not in {StartPending, ActiveAttempt, TerminalPending}:
            raise TypeError("Frozen generation resolution requires a journal record")
        if record.frozen_generation_id != self.frozen_generation_id:
            raise GenerationRuntimeRejected(
                "Journal record names a different frozen generation"
            )
        matches = tuple(
            candidate
            for candidate in (self.hf, self.chf)
            if _provider_attempt_key(candidate.generation, record)
            == record.provider_attempt_key
        )
        if len(matches) != 1:
            raise GenerationRuntimeRejected(
                "Journal record does not belong to exactly one admitted lane"
            )
        return matches[0]


def _require_lane(
    candidate: GenerationLane,
    *,
    lane: LifecycleLane,
    result_identity: ResultLaneIdentity,
    runner_contract_id: str,
    runner_codec: RunnerFrameCodec[Any],
) -> None:
    if type(candidate) is not GenerationLane:
        raise TypeError("Frozen generation lane composition is invalid")
    if candidate.lane is not lane:
        raise GenerationRuntimeRejected("Frozen generation lane identity is invalid")
    if (
        type(candidate.generation) is not RunGenerationIdentity
        or candidate.generation.analysis_kind_ref
        != lane.offering.analysis_kind_ref
    ):
        raise GenerationRuntimeRejected(
            "Frozen generation run policy belongs to another analysis kind"
        )
    if (
        type(candidate.result_facts) is not ProviderResultFacts
        or candidate.result_facts.identity is not result_identity
        or candidate.result_facts.runner_contract_id != runner_contract_id
    ):
        raise GenerationRuntimeRejected(
            "Frozen generation result facts belong to another runner"
        )
    if candidate.runner_codec is not runner_codec:
        raise GenerationRuntimeRejected(
            "Frozen generation input codec belongs to another runner"
        )


def _provider_attempt_key(
    generation: RunGenerationIdentity,
    record: AttemptJournalRecord,
) -> str:
    return derive_provider_attempt_key(
        provider_ref=generation.provider_ref,
        run_generation_fingerprint=run_generation_fingerprint(generation),
        job_ref=record.job_ref,
        input_fingerprint=record.input_fingerprint,
    )
