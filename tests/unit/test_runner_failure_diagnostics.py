"""Prove worker causes cross only a correlated, intact response stream."""

from tests.fakes.runner import admit_fake_session


from dataclasses import replace
import json
import unittest

from families.nmrpeak.runner_worker import serve_loaded_nmrpeak_runtime
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CODEC
from nmrpeak_provider.runner_protocol import FailedFrame, ValidateFrame, GenerateFrame, RunnerProtocolError
from tests.fakes.runner import FakeRunnerChannel
from nmrpeak_provider.runner_session import RunnerSession, RunnerSessionRetired
from tests.unit.test_chf_worker import READY, CORRELATION, MODEL_INPUT, RecordingRuntime, WorkerHarness, FACTS, DEADLINES, validate


class ScriptedConnection:
    def __init__(self, *, failed_write=None):
        self.incoming = bytearray(CHF_RUNNER_CODEC.encode(ValidateFrame(CORRELATION, MODEL_INPUT))
                                  + CHF_RUNNER_CODEC.encode(GenerateFrame(CORRELATION)))
        self.frames = []
        self.writes = 0
        self.failed_write = failed_write

    def recv_into(self, destination):
        count = min(len(destination), len(self.incoming))
        destination[:count] = self.incoming[:count]
        del self.incoming[:count]
        return count

    def sendall(self, data):
        self.writes += 1
        if self.writes == self.failed_write:
            raise OSError("Socket write failed after partial handoff")
        self.frames.append(CHF_RUNNER_CODEC.decode_frame(data))


class RunnerFailureDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_and_generation_causes_reach_the_provider(self):
        for phase in ("validation", "generation"):
            with self.subTest(phase=phase):
                primary = RuntimeError("Vocabulary failed; Bearer actual-secret")
                runtime = RecordingRuntime(**{phase + "_failure": primary})
                with WorkerHarness(runtime) as harness:
                    session = await self.enterAsyncContext(admit_fake_session(harness.provider, FACTS, DEADLINES, CHF_RUNNER_CODEC))
                    with self.assertRaises(RunnerSessionRetired) as raised:
                        admitted = await session.join_science(validate(session))
                        await session.join_science(session.generate(admitted))
                    self.assertIn("Vocabulary failed", str(raised.exception))
                    self.assertIn(f"No verified {phase} outcome", str(raised.exception))
                    self.assertNotIn("actual-secret", str(raised.exception))
                    await session.retire()
                self.assertIs(harness.failure, primary)

    async def test_wrong_phase_or_correlation_cannot_supply_an_attempt_failure(self):
        for phase, wrong_correlation in (("generate", False), ("validate", True)):
            class Channel(FakeRunnerChannel):
                def _accept_validate(self, frame):
                    correlation = replace(frame.correlation, attempt_ref="execution_attempt:sha256:" + "f" * 64) if wrong_correlation else frame.correlation
                    self._queue(FailedFrame(correlation, phase, "Foreign failure explanation"))

            channel = Channel(CHF_RUNNER_CODEC, READY)
            session = await self.enterAsyncContext(admit_fake_session(channel, FACTS, DEADLINES, CHF_RUNNER_CODEC))
            with self.subTest(phase=phase), self.assertRaises(RunnerSessionRetired) as raised:
                await session.join_science(validate(session))
            self.assertIn("response type or correlation is wrong", str(raised.exception))
            self.assertNotIn("Foreign failure explanation", str(raised.exception))
            await session.retire()
            self.assertTrue(channel.closed)

    async def test_result_encoding_failure_is_reported_before_transmission(self):
        connection = ScriptedConnection()
        with self.assertRaises(TypeError):
            serve_loaded_nmrpeak_runtime(connection, RecordingRuntime(candidates=object()), READY, CHF_RUNNER_CODEC)
        failed = connection.frames[-1]
        self.assertIs(type(failed), FailedFrame)
        self.assertEqual(failed.phase, "generate")
        self.assertIn("unsupported canonical JSON type", failed.diagnostic)

    async def test_partial_result_write_does_not_append_another_frame(self):
        connection = ScriptedConnection(failed_write=3)
        with self.assertRaisesRegex(OSError, "partial handoff"):
            serve_loaded_nmrpeak_runtime(connection, RecordingRuntime(candidates=["CCO"]), READY, CHF_RUNNER_CODEC)
        self.assertEqual(connection.writes, 3)
        self.assertFalse(any(type(frame) is FailedFrame for frame in connection.frames))

    async def test_failure_report_delivery_does_not_replace_primary(self):
        primary = RuntimeError("Original tensor failure")
        connection = ScriptedConnection(failed_write=3)
        with self.assertRaises(RuntimeError) as raised:
            serve_loaded_nmrpeak_runtime(connection, RecordingRuntime(generation_failure=primary), READY, CHF_RUNNER_CODEC)
        self.assertIs(raised.exception, primary)
        self.assertIn("partial handoff", " ".join(primary.__notes__))

    async def test_previous_protocol_cannot_admit_failure_frames(self):
        frame = FailedFrame(CORRELATION, "validate", "Vocabulary missing")
        document = json.loads(CHF_RUNNER_CODEC.encode(frame)[4:])
        document["v"] = 3
        with self.assertRaises(RunnerProtocolError):
            CHF_RUNNER_CODEC.decode_payload(canonical_json_bytes(document))

    async def test_failure_frames_reject_unknown_phases_and_invalid_diagnostics(self):
        for phase, diagnostic in (("other", "reason"), ([], "reason"), ("validate", ""), ("generate", "x" * 4097), ("generate", "bad\ntext")):
            with self.subTest(phase=phase), self.assertRaises(RunnerProtocolError):
                FailedFrame(CORRELATION, phase, diagnostic)
