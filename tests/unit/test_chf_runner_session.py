"""Prove READY admission, one-request sequencing, and boot retirement."""

from __future__ import annotations

from dataclasses import replace
from threading import Thread
import unittest

from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
    ChfRunnerProtonPeak,
)
from nmrpeak_provider.chf_runner_protocol import ReadyFrame, RetireFrame
from nmrpeak_provider.chf_runner_session import (
    ChfInputRejected,
    ChfRunnerAdmissionError,
    ChfRunnerDeadlines,
    ChfRunnerSession,
    ChfRunnerSessionRetired,
    GeneratedChfCandidates,
    ValidatedChfRequest,
)
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
    RunnerResultRejected,
    canonical_result_bytes,
)
from tests.fakes.chf_runner import FakeChfRunnerChannel, FakeRunnerFault


BOOT = "boot:" + "1" * 32
ATTEMPT_REF = "execution_attempt:sha256:" + "2" * 64
ATTEMPT_KEY = "nmrpeak-provider.v1:" + "3" * 64
FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id="nmrpeak.runner_session.chf.v1",
    checkpoint_ref="sha256:" + "4" * 64,
    image_input_ref="sha256:" + "5" * 64,
)
DEADLINES = ChfRunnerDeadlines(0.1, 0.1, 0.1, 0.1)
MODEL_INPUT = ChfRunnerInput(
    "C2H6O",
    (ChfRunnerProtonPeak("1.25", 3, "t", "7.1_"),),
    (ChfRunnerCarbonPeak("70.4"),),
)


def ready_frame() -> ReadyFrame:
    return ReadyFrame(
        boot_generation=BOOT,
        runner_ref="nmrpeak_chf_v1",
        runner_contract_id=FACTS.runner_contract_id,
        release_sha256=FACTS.checkpoint_ref,
        source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
        image_input_id=FACTS.image_input_ref,
        target="cpu-x86_64",
        device="cpu",
        decode_policy_id="nmrpeak_chf_decode_v1",
    )


def validate(session: ChfRunnerSession) -> ValidatedChfRequest | ChfInputRejected:
    return session.validate(
        execution_attempt_ref=ATTEMPT_REF,
        provider_attempt_key=ATTEMPT_KEY,
        model_input=MODEL_INPUT,
    )


class ChfRunnerSessionTests(unittest.TestCase):
    def test_ready_compares_every_provider_owned_deployment_fact(self) -> None:
        mismatches = {
            "runner_ref": "nmrpeak_other_v1",
            "runner_contract_id": "nmrpeak.runner_session.other.v1",
            "release_sha256": "sha256:" + "6" * 64,
            "source_closure_sha256": "sha256:" + "7" * 64,
            "image_input_id": "sha256:" + "8" * 64,
            "target": "cuda12-x86_64",
            "device": "cuda",
            "decode_policy_id": "other_decode_v1",
        }
        for field, value in mismatches.items():
            channel = FakeChfRunnerChannel(replace(ready_frame(), **{field: value}))
            with self.subTest(field=field):
                with self.assertRaises(ChfRunnerAdmissionError):
                    ChfRunnerSession.admit(channel, FACTS, DEADLINES)
                self.assertTrue(channel.closed)

    def test_fake_runner_validates_then_generates_with_one_correlation(self) -> None:
        channel = FakeChfRunnerChannel(ready_frame(), candidates=("CCO", "OCC"))
        session = ChfRunnerSession.admit(channel, FACTS, DEADLINES)

        accepted = validate(session)
        self.assertIsInstance(accepted, ValidatedChfRequest)
        assert isinstance(accepted, ValidatedChfRequest)
        candidates = session.generate(accepted)

        self.assertIs(type(candidates), GeneratedChfCandidates)
        self.assertEqual(["CCO", "OCC"], candidates.value)
        validate_frame, generate_frame = channel.received_frames[:2]
        self.assertEqual(validate_frame.correlation, generate_frame.correlation)
        self.assertEqual(ATTEMPT_REF, validate_frame.correlation.attempt_ref)
        self.assertIs(FACTS, session.result_facts)

    def test_deterministic_validation_rejection_preserves_the_boot(self) -> None:
        channel = FakeChfRunnerChannel(ready_frame(), rejected_validations=1)
        session = ChfRunnerSession.admit(channel, FACTS, DEADLINES)

        self.assertIsInstance(validate(session), ChfInputRejected)
        accepted = validate(session)
        self.assertIsInstance(accepted, ValidatedChfRequest)
        assert isinstance(accepted, ValidatedChfRequest)
        self.assertEqual(["CCO", "OCC"], session.generate(accepted).value)

    def test_uncertain_or_wrong_exchange_retires_the_boot(self) -> None:
        faults = (
            FakeRunnerFault.WRONG_VALIDATED_CORRELATION,
            FakeRunnerFault.MALFORMED_VALIDATION,
            FakeRunnerFault.WRONG_RESULT_CORRELATION,
            FakeRunnerFault.REJECT_GENERATION,
            FakeRunnerFault.EOF_DURING_GENERATION,
            FakeRunnerFault.TIMEOUT_DURING_GENERATION,
        )
        for fault in faults:
            channel = FakeChfRunnerChannel(ready_frame(), fault=fault)
            session = ChfRunnerSession.admit(channel, FACTS, DEADLINES)
            with self.subTest(fault=fault):
                with self.assertRaises(ChfRunnerSessionRetired):
                    accepted = validate(session)
                    if isinstance(accepted, ValidatedChfRequest):
                        session.generate(accepted)
                self.assertTrue(channel.closed)

    def test_cancellation_wakes_a_blocked_generation_and_retires(self) -> None:
        channel = FakeChfRunnerChannel(
            ready_frame(),
            fault=FakeRunnerFault.BLOCK_GENERATION,
        )
        session = ChfRunnerSession.admit(channel, FACTS, ChfRunnerDeadlines(1, 1, 5, 1))
        accepted = validate(session)
        self.assertIsInstance(accepted, ValidatedChfRequest)
        assert isinstance(accepted, ValidatedChfRequest)
        failures: list[BaseException] = []

        def generate() -> None:
            try:
                session.generate(accepted)
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=generate)
        thread.start()
        self.assertTrue(channel.generate_received.wait(timeout=1))
        session.cancel()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], ChfRunnerSessionRetired)

    def test_idle_retirement_is_boot_scoped_and_prevents_reuse(self) -> None:
        channel = FakeChfRunnerChannel(ready_frame())
        session = ChfRunnerSession.admit(channel, FACTS, DEADLINES)

        session.retire()

        self.assertTrue(channel.closed)
        self.assertEqual(RetireFrame(BOOT), channel.received_frames[-1])
        with self.assertRaises(ChfRunnerSessionRetired):
            validate(session)

    def test_retire_send_failure_preserves_handoff_uncertainty(self) -> None:
        channel = FakeChfRunnerChannel(
            ready_frame(),
            fault=FakeRunnerFault.RETIRE_SEND_UNCERTAIN,
        )
        session = ChfRunnerSession.admit(channel, FACTS, DEADLINES)

        with self.assertRaisesRegex(
            ChfRunnerSessionRetired,
            "Cannot determine whether idle CHF RETIRE was handed off",
        ):
            session.retire()

        self.assertEqual(RetireFrame(BOOT), channel.received_frames[-1])
        self.assertTrue(channel.closed)

    def test_candidate_semantics_remain_owned_by_the_result_validator(self) -> None:
        channel = FakeChfRunnerChannel(
            ready_frame(),
            candidates={"not": "a candidate array"},
        )
        session = ChfRunnerSession.admit(channel, FACTS, DEADLINES)
        accepted = validate(session)
        self.assertIsInstance(accepted, ValidatedChfRequest)
        assert isinstance(accepted, ValidatedChfRequest)

        candidates = session.generate(accepted)

        with self.assertRaisesRegex(RunnerResultRejected, "JSON array"):
            canonical_result_bytes(candidates.value, session.result_facts)


if __name__ == "__main__":
    unittest.main()
