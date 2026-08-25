"""Prove journal records resolve only through their admitted lane generation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest

from nmrpeak_provider.attempt_identity import derive_provider_attempt_key
from nmrpeak_provider.attempt_journal import StartPending
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CODEC,
    CHF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.generation_runtime import (
    GenerationRuntime,
    GenerationRuntimeRejected,
    GenerationLane,
)
from nmrpeak_provider.hf_runner_protocol import (
    HF_RUNNER_CODEC,
    HF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.lifecycle_lane import (
    CHF_LIFECYCLE_LANE,
    HF_LIFECYCLE_LANE,
)
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    ProviderResultFacts,
)
from nmrpeak_provider.run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    run_generation_fingerprint,
)


FROZEN_GENERATION_ID = "sha256:" + "1" * 64
HF_FACTS = ProviderResultFacts(
    identity=HF_RESULT_IDENTITY,
    runner_contract_id=HF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "2" * 64,
    image_input_ref="sha256:" + "3" * 64,
)
CHF_FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "4" * 64,
    image_input_ref="sha256:" + "5" * 64,
)


class GenerationRuntimeTests(unittest.TestCase):
    def test_each_record_resolves_from_its_attempt_key_without_a_lane_marker(self) -> None:
        runtime = generation_runtime()

        hf_record = record_for(runtime.hf)
        chf_record = record_for(runtime.chf)

        self.assertIs(runtime.resolve(hf_record), runtime.hf)
        self.assertIs(runtime.resolve(chf_record), runtime.chf)

    def test_wrong_generation_or_unowned_key_fails_closed(self) -> None:
        runtime = generation_runtime()
        hf_record = record_for(runtime.hf)

        with self.assertRaisesRegex(
            GenerationRuntimeRejected,
            "different frozen generation",
        ):
            runtime.resolve(
                replace(hf_record, frozen_generation_id="sha256:" + "6" * 64)
            )
        with self.assertRaisesRegex(
            GenerationRuntimeRejected,
            "exactly one admitted lane",
        ):
            runtime.resolve(
                replace(
                    hf_record,
                    provider_attempt_key="nmrpeak-provider.v1:" + "7" * 64,
                )
            )

    def test_generation_rejects_cross_lane_and_cross_provider_composition(self) -> None:
        runtime = generation_runtime()
        invalid_hf = (
            replace(runtime.hf, lane=CHF_LIFECYCLE_LANE),
            replace(runtime.hf, generation=runtime.chf.generation),
            replace(runtime.hf, result_facts=CHF_FACTS),
            replace(runtime.hf, runner_codec=CHF_RUNNER_CODEC),
        )
        for lane in invalid_hf:
            with self.subTest(lane=lane), self.assertRaises(GenerationRuntimeRejected):
                replace(runtime, hf=lane)

        foreign_chf = replace(
            runtime.chf,
            generation=replace(
                runtime.chf.generation,
                provider_ref="provider:other",
            ),
        )
        with self.assertRaisesRegex(GenerationRuntimeRejected, "provider identity"):
            replace(runtime, chf=foreign_chf)


def generation_runtime() -> GenerationRuntime:
    hf = GenerationLane(
        lane=HF_LIFECYCLE_LANE,
        generation=generation("mol_from_1h_peaks", "hf-generation"),
        result_facts=HF_FACTS,
        runner_codec=HF_RUNNER_CODEC,
    )
    chf = GenerationLane(
        lane=CHF_LIFECYCLE_LANE,
        generation=generation("mol_from_1h_13c_formula", "chf-generation"),
        result_facts=CHF_FACTS,
        runner_codec=CHF_RUNNER_CODEC,
    )
    return GenerationRuntime(FROZEN_GENERATION_ID, hf, chf)


def generation(analysis_kind_ref: str, generation_id: str) -> RunGenerationIdentity:
    return RunGenerationIdentity(
        provider_ref="provider:nmrpeak",
        analysis_kind_ref=analysis_kind_ref,
        generation_id=generation_id,
        scope=CreatedAtWindow(datetime(2026, 8, 24, tzinfo=UTC)),
    )


def record_for(runtime: GenerationLane) -> StartPending:
    input_fingerprint = "sha256:" + "8" * 64
    return StartPending(
        job_ref="job:selected",
        provider_attempt_key=derive_provider_attempt_key(
            provider_ref=runtime.generation.provider_ref,
            run_generation_fingerprint=run_generation_fingerprint(
                runtime.generation
            ),
            job_ref="job:selected",
            input_fingerprint=input_fingerprint,
        ),
        input_fingerprint=input_fingerprint,
        frozen_generation_id=FROZEN_GENERATION_ID,
    )


if __name__ == "__main__":
    unittest.main()
