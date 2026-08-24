"""Prove NMRPeak success bytes are bounded, canonical, and provider-owned."""

from __future__ import annotations

import json
import unittest

from nmrpeak_provider.product_decode import CHF_DECODE_POLICY, HF_DECODE_POLICY
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    MAX_RESULT_BYTES,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
    ResultLaneIdentity,
    RunnerResultRejected,
    canonical_result_bytes,
)


CHECKPOINT_REF = "sha256:" + "1" * 64
IMAGE_INPUT_REF = "sha256:" + "2" * 64


def hf_facts() -> ProviderResultFacts:
    return ProviderResultFacts(
        identity=HF_RESULT_IDENTITY,
        runner_contract_id="nmrpeak.runner_session.test.v1",
        checkpoint_ref=CHECKPOINT_REF,
        image_input_ref=IMAGE_INPUT_REF,
    )


class ProductResultTests(unittest.TestCase):
    def test_decode_policies_freeze_the_reviewed_generation_choices(self) -> None:
        for policy in (HF_DECODE_POLICY, CHF_DECODE_POLICY):
            with self.subTest(policy=policy.decode_policy_id):
                self.assertEqual(10, policy.beam_size)
                self.assertEqual(160, policy.maximum_generated_tokens)
                self.assertEqual("3.0", str(policy.temperature))
                self.assertEqual(1, policy.seed)

    def test_success_preserves_candidate_order_and_duplicates(self) -> None:
        encoded = canonical_result_bytes(["CCO", "N#N", "CCO"], hf_facts())

        self.assertEqual(
            {
                "candidates": [
                    {"generated_smiles": "CCO"},
                    {"generated_smiles": "N#N"},
                    {"generated_smiles": "CCO"},
                ],
                "provenance": {
                    "checkpoint_sha256": CHECKPOINT_REF,
                    "decode_policy_id": "nmrpeak_hf_decode_v1",
                    "device": "cpu",
                    "image_input_id": IMAGE_INPUT_REF,
                    "runner_contract_id": "nmrpeak.runner_session.test.v1",
                    "runner_ref": "nmrpeak_hf_v1",
                    "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
                    "target": "cpu-x86_64",
                },
                "schema_id": "nmrpeak.structure_candidates.result.v1",
            },
            json.loads(encoded),
        )
        self.assertEqual(
            encoded,
            json.dumps(
                json.loads(encoded),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )

    def test_both_fixed_runner_identities_are_admitted(self) -> None:
        facts = ProviderResultFacts(
            identity=CHF_RESULT_IDENTITY,
            runner_contract_id="nmrpeak.runner_session.test.v1",
            checkpoint_ref=CHECKPOINT_REF,
            image_input_ref=IMAGE_INPUT_REF,
        )
        result = json.loads(canonical_result_bytes(["C"], facts))
        self.assertEqual("nmrpeak_chf_v1", result["provenance"]["runner_ref"])

    def test_candidate_collection_is_closed_and_bounded(self) -> None:
        cases = (
            ((), "JSON array"),
            ([], "between one and 10"),
            (["C"] * 11, "between one and 10"),
            ([1], "non-empty strings"),
            ([""], "non-empty strings"),
            (["C C"], "decoder vocabulary"),
            (["C\nC"], "decoder vocabulary"),
            (["C" * 3_501], "size limit"),
        )
        for candidates, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(RunnerResultRejected, message):
                    canonical_result_bytes(candidates, hf_facts())

    def test_largest_admitted_result_stays_below_the_provider_cap(self) -> None:
        encoded = canonical_result_bytes(["C" * 3_498] * 10, hf_facts())
        self.assertLess(len(encoded), MAX_RESULT_BYTES)

    def test_final_canonical_byte_cap_rejects_json_escape_expansion(self) -> None:
        with self.assertRaisesRegex(RunnerResultRejected, "canonical byte limit"):
            canonical_result_bytes(["\\" * 3_498] * 10, hf_facts())

    def test_provenance_rejects_unowned_or_malformed_identities(self) -> None:
        forged_identity = ResultLaneIdentity(
            runner_ref="nmrpeak_hf_v1",
            decode_policy=HF_RESULT_IDENTITY.decode_policy,
        )
        with self.assertRaisesRegex(AssertionError, "unowned runner identity"):
            ProviderResultFacts(
                forged_identity,
                "nmrpeak.runner_session.test.v1",
                CHECKPOINT_REF,
                IMAGE_INPUT_REF,
            )

        malformed_cases = (
            (
                HF_RESULT_IDENTITY,
                "invalid contract",
                CHECKPOINT_REF,
                IMAGE_INPUT_REF,
                "runner contract identity",
            ),
            (
                HF_RESULT_IDENTITY,
                "nmrpeak.runner_session.test.v1",
                "sha256:short",
                IMAGE_INPUT_REF,
                "exact SHA-256 identities",
            ),
            (
                HF_RESULT_IDENTITY,
                "nmrpeak.runner_session.test.v1",
                CHECKPOINT_REF,
                "2" * 64,
                "exact SHA-256 identities",
            ),
        )
        for *values, message in malformed_cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, message):
                    ProviderResultFacts(*values)

        with self.assertRaisesRegex(TypeError, "provider-owned facts"):
            canonical_result_bytes(["C"], object())


if __name__ == "__main__":
    unittest.main()
