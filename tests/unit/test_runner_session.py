"""Prove READY admission, one-request sequencing, and boot retirement."""

from __future__ import annotations

from tests.fakes.runner import admit_fake_session

from dataclasses import replace
import asyncio
from concurrent.futures import Future
import time
from types import SimpleNamespace
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from nmrpeak_provider.diagnostic_text import exception_diagnostic
from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
)
from nmrpeak_provider.nmrpeak_binding import RunnerProtonPeak
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CONTRACT_ID,
    CHF_RUNNER_CODEC,
)
from nmrpeak_provider.hf_binding import HfRunnerInput
from nmrpeak_provider.hf_runner_protocol import (
    HF_RUNNER_CODEC,
    HF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.runner_protocol import (
    RunnerChannelClosed,
    runner_rejection_diagnostic,
    ReadyFrame,
    RunnerRejectionReason,
)
from nmrpeak_provider.runner_session import (
    RunnerInputRejected,
    RunnerAdmissionError,
    RunnerDeadlines,
    RunnerSession,
    RunnerSessionRetired,
    RunnerValidationDrift,
    GeneratedRunnerCandidates,
    ValidatedRunnerRequest,
    open_runner_session,
)
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
    RunnerResultRejected,
    canonical_result_bytes,
)
from tests.fakes.runner import FakeRunnerChannel, FakeRunnerFault


BOOT = "boot:" + "1" * 32
ATTEMPT_REF = "execution_attempt:sha256:" + "2" * 64
ATTEMPT_KEY = "nmrpeak-provider.v1:" + "3" * 64
FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "4" * 64,
    image_input_ref="sha256:" + "5" * 64,
)
DEADLINES = RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1)
FRAME_DEADLINES = replace(DEADLINES, connect_seconds=2, retire_seconds=2)
MODEL_INPUT = ChfRunnerInput(
    "C2H6O",
    (RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
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


def validate(session: RunnerSession) -> Future[ValidatedRunnerRequest | RunnerInputRejected]:
    return session.validate(
        execution_attempt_ref=ATTEMPT_REF,
        provider_attempt_key=ATTEMPT_KEY,
        model_input=MODEL_INPUT,
    )


class RunnerSessionTests(unittest.IsolatedAsyncioTestCase):

    async def test_startup_observation_interruption_preserves_the_partial_owner(self):
        class ClosedChannel(FakeRunnerChannel):
            def recv_into(self, destination):
                return 0

        channel = ClosedChannel(CHF_RUNNER_CODEC, ready_frame())
        interruption = KeyboardInterrupt("Startup observation interrupted")
        with patch("tests.fakes.runner.FakeNativeControl.observe_retirement", side_effect=interruption):
            with self.assertRaises(RunnerAdmissionError) as caught:
                await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))
        owner = caught.exception.owner
        self.assertIsNotNone(owner)
        self.assertIs(owner.control.reader.exception(), interruption)
        self.assertFalse(owner.retirement_confirmed)
        self.assertIsInstance(caught.exception.__cause__.exceptions[0], RunnerChannelClosed)
        rendered = exception_diagnostic(caught.exception, limit=8192)
        self.assertIn("connection closed during header", rendered)
        self.assertIn("Startup observation interrupted", rendered)

    async def test_fragmented_replies_cannot_renew_the_phase_budget(self):
        for phase in ("ready", "validate", "generate"):
            with self.subTest(phase=phase):
                elapsed = [0.0]
                clock_start = time.monotonic()

                class FragmentedChannel(FakeRunnerChannel):
                    fragment = False

                    def recv_into(self, destination):
                        if self.fragment:
                            delay = 0.06
                            if self._timeout < delay:
                                elapsed[0] += self._timeout
                                raise TimeoutError("Fragment did not arrive before socket timeout")
                            elapsed[0] += delay
                            destination = destination[:8]
                        return super().recv_into(destination)

                channel = FragmentedChannel(CHF_RUNNER_CODEC, ready_frame())
                session = None
                if phase != "ready":
                    session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))
                if phase == "generate":
                    prepared = await session.join_science(validate(session))
                channel.fragment = True
                with patch("nmrpeak_provider.runner_session.time", SimpleNamespace(monotonic=lambda: clock_start + elapsed[0])):
                    expected = RunnerAdmissionError if phase == "ready" else RunnerSessionRetired
                    with self.assertRaises(expected):
                        if phase == "ready":
                            await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))
                        elif phase == "validate":
                            await session.join_science(validate(session))
                        else:
                            await session.join_science(session.generate(prepared))
                self.assertLessEqual(elapsed[0], 0.100001)
                if session is not None:
                    await session.retire()


    async def test_absent_private_endpoint_expires_under_the_connect_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            deadlines = replace(DEADLINES, connect_seconds=0.02, retire_seconds=20)
            with self.assertRaisesRegex(RunnerAdmissionError, "connect deadline"):
                (await open_runner_session(
                    str(Path(directory) / "absent.sock"),
                    FACTS,
                    deadlines,
                    CHF_RUNNER_CODEC,
                ))

    async def test_private_endpoint_must_be_an_owner_only_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "runner.sock"
            socket_path.write_text("not a socket", encoding="ascii")
            with self.assertRaisesRegex(RunnerAdmissionError, "Unix socket"):
                await open_runner_session(str(socket_path), FACTS, replace(DEADLINES, retire_seconds=20), CHF_RUNNER_CODEC)

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "runner.sock"
            with ListeningEndpoint(socket_path):
                os.chmod(socket_path, 0o660)
                with self.assertRaisesRegex(RunnerAdmissionError, "owner-only"):
                    await open_runner_session(str(socket_path), FACTS, replace(DEADLINES, retire_seconds=20), CHF_RUNNER_CODEC)


    async def test_ready_compares_every_provider_owned_deployment_fact(self) -> None:
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
            channel = FakeRunnerChannel(CHF_RUNNER_CODEC, replace(ready_frame(), **{field: value}))
            with self.subTest(field=field):
                with self.assertRaises(RunnerAdmissionError):
                    await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))
                self.assertTrue(channel.closed)

    async def test_fake_runner_validates_then_generates_with_one_correlation(self) -> None:
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame(), candidates=("CCO", "OCC"))
        session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))

        accepted = await session.join_science(validate(session))
        self.assertIsInstance(accepted, ValidatedRunnerRequest)
        assert isinstance(accepted, ValidatedRunnerRequest)
        candidates = await session.join_science(session.generate(accepted))

        self.assertIs(type(candidates), GeneratedRunnerCandidates)
        self.assertEqual(["CCO", "OCC"], candidates.value)
        validate_frame, generate_frame = channel.received_frames[:2]
        self.assertEqual(validate_frame.correlation, generate_frame.correlation)
        self.assertEqual(ATTEMPT_REF, validate_frame.correlation.attempt_ref)
        self.assertIs(FACTS, session.result_facts)

    async def test_hf_session_uses_its_exact_ready_facts_and_model_input_codec(self) -> None:
        facts = ProviderResultFacts(
            identity=HF_RESULT_IDENTITY,
            runner_contract_id=HF_RUNNER_CONTRACT_ID,
            checkpoint_ref="sha256:" + "6" * 64,
            image_input_ref="sha256:" + "7" * 64,
        )
        ready = ReadyFrame(
            boot_generation=BOOT,
            runner_ref=HF_RESULT_IDENTITY.runner_ref,
            runner_contract_id=HF_RUNNER_CONTRACT_ID,
            release_sha256=facts.checkpoint_ref,
            source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
            image_input_id=facts.image_input_ref,
            target="cpu-x86_64",
            device="cpu",
            decode_policy_id=HF_RESULT_IDENTITY.decode_policy.decode_policy_id,
        )
        model_input = HfRunnerInput(
            "C2H6O",
            (RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
        )
        channel = FakeRunnerChannel(HF_RUNNER_CODEC, ready, candidates=("CCO",))
        session = await self.enterAsyncContext(admit_fake_session(channel, facts, FRAME_DEADLINES, HF_RUNNER_CODEC))

        validated = (await session.join_science(session.validate(
            execution_attempt_ref=ATTEMPT_REF,
            provider_attempt_key=ATTEMPT_KEY,
            model_input=model_input,
        )))
        self.assertIs(type(validated), ValidatedRunnerRequest)
        assert isinstance(validated, ValidatedRunnerRequest)
        self.assertEqual(["CCO"], (await session.join_science(session.generate(validated))).value)
        self.assertEqual(model_input, channel.received_frames[0].model_input)
        self.assertIs(facts, session.result_facts)

    async def test_deterministic_validation_rejection_preserves_the_boot(self) -> None:
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame(), rejected_validations=1)
        session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))

        rejection = await session.join_science(validate(session))
        self.assertIsInstance(rejection, RunnerInputRejected)
        assert isinstance(rejection, RunnerInputRejected)
        self.assertEqual(rejection.reason, RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED)
        self.assertEqual(rejection.token_count, 512)
        self.assertEqual(
            rejection.message,
            runner_rejection_diagnostic(RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED, 512),
        )
        accepted = await session.join_science(validate(session))
        self.assertIsInstance(accepted, ValidatedRunnerRequest)
        assert isinstance(accepted, ValidatedRunnerRequest)
        self.assertEqual(["CCO", "OCC"], (await session.join_science(session.generate(accepted))).value)

    async def test_tokenizer_or_dictionary_drift_retires_without_input_rejection(self) -> None:
        for reason in (
            RunnerRejectionReason.TOKENIZER_EMPTY_OUTPUT,
            RunnerRejectionReason.DICTIONARY_TOKEN_MISSING,
        ):
            with self.subTest(reason=reason):
                channel = FakeRunnerChannel(
                    CHF_RUNNER_CODEC, ready_frame(), rejected_validations=1,
                    rejected_reason=reason,
                )
                session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))
                with self.assertRaises(RunnerValidationDrift) as raised:
                    await session.join_science(validate(session))
                self.assertEqual(raised.exception.reason, reason)
                self.assertTrue(session.retired)
                await session.retire()
                self.assertTrue(channel.closed)

    async def test_uncertain_or_wrong_exchange_retires_the_boot(self) -> None:
        faults = (
            FakeRunnerFault.WRONG_VALIDATED_CORRELATION,
            FakeRunnerFault.MALFORMED_VALIDATION,
            FakeRunnerFault.WRONG_RESULT_CORRELATION,
            FakeRunnerFault.REJECT_GENERATION,
            FakeRunnerFault.EOF_DURING_GENERATION,
            FakeRunnerFault.TIMEOUT_DURING_GENERATION,
        )
        for fault in faults:
            channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame(), fault=fault)
            session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))
            with self.subTest(fault=fault):
                with self.assertRaises(RunnerSessionRetired):
                    accepted = await session.join_science(validate(session))
                    if isinstance(accepted, ValidatedRunnerRequest):
                        await session.join_science(session.generate(accepted))
                await session.retire()
                self.assertTrue(channel.closed)

    async def test_cancellation_wakes_a_blocked_generation_and_retires(self):
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame(), fault=FakeRunnerFault.BLOCK_GENERATION)
        session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, RunnerDeadlines(2, 2, 2, 5, 2), CHF_RUNNER_CODEC))
        accepted = await session.join_science(validate(session))
        original = session.generate(accepted)
        self.assertTrue(await asyncio.to_thread(channel.generate_received.wait, 1))
        session.cancel()
        with self.assertRaises(RunnerSessionRetired):
            await session.join_science(original)
        await session.retire()
        self.assertTrue(session.retirement_confirmed)

    async def test_idle_retirement_settles_its_native_port_and_prevents_reuse(self) -> None:
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame())
        session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))

        await session.retire()

        self.assertTrue(channel.closed)
        self.assertTrue(session.retirement_confirmed)
        self.assertEqual(channel.received_frames, [])
        self.assertTrue(session.native.interrupted)
        self.assertTrue(session.native.stop_requested)
        self.assertTrue(session.native.closed)
        with self.assertRaises(RunnerSessionRetired):
            await session.join_science(validate(session))

    async def test_failed_native_stop_does_not_claim_retirement(self):
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame())
        context = admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC)
        session = await context.__aenter__()
        with patch.object(session.native, "request_stop", side_effect=OSError("Control stop failed")):
            with self.assertRaises(RunnerSessionRetired):
                await session.retire()
        self.assertFalse(session.retirement_confirmed)
        self.assertTrue(channel.closed)
        with self.assertRaises(RunnerSessionRetired):
            await context.__aexit__(None, None, None)

    async def test_candidate_semantics_remain_owned_by_the_result_validator(self) -> None:
        channel = FakeRunnerChannel(
            CHF_RUNNER_CODEC,
            ready_frame(),
            candidates={"not": "a candidate array"},
        )
        session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, FRAME_DEADLINES, CHF_RUNNER_CODEC))
        accepted = await session.join_science(validate(session))
        self.assertIsInstance(accepted, ValidatedRunnerRequest)
        assert isinstance(accepted, ValidatedRunnerRequest)

        candidates = await session.join_science(session.generate(accepted))

        with self.assertRaisesRegex(RunnerResultRejected, "JSON array"):
            canonical_result_bytes(candidates.value, session.result_facts)


class ListeningEndpoint:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def __enter__(self) -> socket.socket:
        self.listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.listener.listen(1)
        return self.listener

    def __exit__(self, *_error: object) -> None:
        self.listener.close()


if __name__ == "__main__":
    unittest.main()
