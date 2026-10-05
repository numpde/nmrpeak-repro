"""Prove reviewed preparation-failure publication and complete lane policies."""

from __future__ import annotations

from dataclasses import replace
import unittest

from nmrpeak_provider.failure_contract import (
    ClassifiedPreparationFailure,
    FailureContractError,
    PreparationFailurePolicy,
    PreparationFailureRule,
    PublishedPreparationFailure,
    ReportedFailurePublication,
)
from nmrpeak_provider.preparation_failure_policy import (
    FailureKind,
    load_preparation_failure_policy,
    parse_preparation_failure_policy,
)
from nmrpeak_provider.text_provenance import ProviderDiagnosticText


class PreparationFailurePolicyTests(unittest.TestCase):
    def test_each_lane_has_complete_reviewed_publication(self) -> None:
        for lane in ("hf", "chf"):
            with self.subTest(lane=lane):
                policy = load_preparation_failure_policy(lane)
                self.assertEqual(
                    {rule.kind for rule in policy.rules},
                    {kind.value for kind in FailureKind},
                )
                source = policy.resolve(ClassifiedPreparationFailure(
                    FailureKind.DIRECT_SOURCE_ISSUE.value,
                    ProviderDiagnosticText("Input rejected at /model_input/formula: invalid syntax."),
                ))
                self.assertEqual(source, PublishedPreparationFailure(
                    "input_rejected",
                    "Input rejected at /model_input/formula: invalid syntax.",
                ))
                candidate = policy.resolve(ClassifiedPreparationFailure(
                    FailureKind.CANDIDATE_ISSUE.value,
                    ProviderDiagnosticText("Interpreted candidate rejected at /model_input/formula."),
                ))
                self.assertEqual(candidate.failure_code, "interpretation_failed")
                self.assertIn("candidate", candidate.failure_message)
                report = policy.resolve(ClassifiedPreparationFailure(
                    FailureKind.MODEL_REPORTED_PROBLEM.value,
                ))
                self.assertEqual(report.failure_code, "interpretation_failed")
                self.assertIn("exact cause has not been independently verified", report.failure_message)
                runner = policy.resolve(ClassifiedPreparationFailure(
                    FailureKind.DIRECT_RUNNER_REJECTED.value,
                    ProviderDiagnosticText("The model accepts at most 511 input tokens."),
                ))
                self.assertEqual(
                    runner.failure_message,
                    "The model accepts at most 511 input tokens.",
                )
                candidate_runner = policy.resolve(ClassifiedPreparationFailure(
                    FailureKind.CANDIDATE_RUNNER_REJECTED.value,
                    ProviderDiagnosticText(
                        "The last interpreted candidate produced 640 tokenizer tokens."
                    ),
                ))
                self.assertEqual(candidate_runner, PublishedPreparationFailure(
                    "interpretation_failed",
                    "The last interpreted candidate produced 640 tokenizer tokens.",
                ))

    def test_exhaustive_parse_rejects_missing_and_extra_kinds(self) -> None:
        from pathlib import Path

        raw = (Path(__file__).parents[2] / "nmrpeak_provider" / "policies" /
               "hf_preparation_failures.toml").read_bytes()
        with self.assertRaisesRegex(ValueError, "cover every failure kind"):
            parse_preparation_failure_policy(
                raw.replace(b"[failures.candidate_issue]", b"[failures.unreviewed_issue]")
            )
        with self.assertRaisesRegex(ValueError, "cover every failure kind"):
            parse_preparation_failure_policy(raw.split(b"[failures.candidate_issue]")[0])

    def test_unreviewed_forwarding_and_ambiguous_fields_are_rejected(self) -> None:
        from pathlib import Path

        raw = (Path(__file__).parents[2] / "nmrpeak_provider" / "policies" /
               "hf_preparation_failures.toml").read_bytes()
        unsafe = raw.replace(
            b'failure_message = "The interpreter route reported a problem with the submitted description, but the exact cause has not been independently verified. Generation did not start. Review the description before submitting a new Job."',
            b"forward_failure_message = true",
        )
        with self.assertRaisesRegex(ValueError, "unreviewed forwarding"):
            parse_preparation_failure_policy(unsafe)
        ambiguous = raw.replace(
            b"forward_failure_message = true",
            b'forward_failure_message = true\nfailure_message = "another authority"',
            1,
        )
        with self.assertRaisesRegex(ValueError, "terminal failure"):
            parse_preparation_failure_policy(ambiguous)
        with self.assertRaisesRegex(ValueError, "must be bytes"):
            parse_preparation_failure_policy("not bytes")  # type: ignore[arg-type]
        local = raw.replace(
            b"submit_failure_to_api = true",
            b"submit_failure_to_api = false",
            1,
        )
        with self.assertRaisesRegex(ValueError, "must publish"):
            parse_preparation_failure_policy(local)

    def test_policy_resolution_rejects_missing_diagnostic_and_unknown_kind(self) -> None:
        policy = load_preparation_failure_policy("chf")
        with self.assertRaisesRegex(FailureContractError, "reported_failure_message_missing"):
            policy.resolve(ClassifiedPreparationFailure(FailureKind.DIRECT_SOURCE_ISSUE.value))
        with self.assertRaisesRegex(FailureContractError, "unknown_classified_failure_kind"):
            policy.resolve(ClassifiedPreparationFailure("unknown_failure"))
        with self.assertRaisesRegex(FailureContractError, "invalid_classified_failure_message"):
            ClassifiedPreparationFailure(
                FailureKind.CANDIDATE_ISSUE.value,
                ProviderDiagnosticText("contains NUL\x00"),
            )

    def test_policy_rules_are_immutable_and_cannot_duplicate_kind(self) -> None:
        rule = PreparationFailureRule("reviewed", ReportedFailurePublication("input_rejected"))
        with self.assertRaisesRegex(FailureContractError, "duplicate_failure_policy_kind"):
            PreparationFailurePolicy((rule, rule))
        with self.assertRaisesRegex(FailureContractError, "invalid_failure_policy_rules"):
            PreparationFailurePolicy(())
        with self.assertRaisesRegex(FailureContractError, "invalid_failure_message"):
            replace(PublishedPreparationFailure("input_rejected", "valid"), failure_message="")


if __name__ == "__main__":
    unittest.main()
