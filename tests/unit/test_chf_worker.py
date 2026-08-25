"""Prove the checkpoint-free CHF worker loop over the production codec."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
from threading import Thread
import unittest
from unittest.mock import patch

from families.nmrpeak.runner_runtime import NmrpeakRuntimeInputRejected
from families.nmrpeak.runner_worker import serve_loaded_nmrpeak_runtime
from nmrpeak_provider.canonical_json import JsonValue
from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
)
from nmrpeak_provider.nmrpeak_binding import RunnerProtonPeak
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CONTRACT_ID,
    CHF_RUNNER_CODEC,
)
from nmrpeak_provider.runner_protocol import (
    AttemptCorrelation,
    RunnerProtocolError,
    GenerateFrame,
    ReadyFrame,
    RetireFrame,
    ValidateFrame,
    ValidatedFrame,
)
from nmrpeak_provider.runner_session import (
    RunnerInputRejected,
    RunnerDeadlines,
    RunnerSession,
    RunnerSessionRetired,
    ValidatedRunnerRequest,
)
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
)


_WORKER_PATH = (
    Path(__file__).resolve().parents[2]
    / "models/nmrpeak_chf_v1/runner/worker.py"
)
_SPEC = importlib.util.spec_from_file_location("nmrpeak_chf_worker", _WORKER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
worker_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker_module)

BOOT = "boot:" + "1" * 32
ATTEMPT_REF = "execution_attempt:sha256:" + "2" * 64
ATTEMPT_KEY = "nmrpeak-provider.v1:" + "3" * 64
FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "4" * 64,
    image_input_ref="sha256:" + "5" * 64,
)
READY = ReadyFrame(
    boot_generation=BOOT,
    runner_ref=CHF_RESULT_IDENTITY.runner_ref,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    release_sha256=FACTS.checkpoint_ref,
    source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
    image_input_id=FACTS.image_input_ref,
    target="cpu-x86_64",
    device="cpu",
    decode_policy_id=CHF_RESULT_IDENTITY.decode_policy.decode_policy_id,
)
MODEL_INPUT = ChfRunnerInput(
    "C2H6O",
    (RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
    (ChfRunnerCarbonPeak("70.4"),),
)
CORRELATION = AttemptCorrelation(BOOT, "request:" + "6" * 32, ATTEMPT_REF, ATTEMPT_KEY)
DEADLINES = RunnerDeadlines(1, 1, 1, 1, 1)


class ChfWorkerTests(unittest.TestCase):
    def test_worker_loads_the_verified_descriptor_before_publishing_ready(self) -> None:
        checkpoint = object()
        runtime = RecordingRuntime(candidates=["CCO"])
        connection = object()
        served: list[tuple[object, object, ReadyFrame, object]] = []

        def serve(
            received_connection: object,
            received_runtime: object,
            ready: ReadyFrame,
            codec: object,
        ) -> int:
            served.append((received_connection, received_runtime, ready, codec))
            return 0

        with (
            patch.object(
                worker_module,
                "open_verified_checkpoint",
                return_value=CheckpointContext(checkpoint),
            ) as open_checkpoint,
            patch.object(
                worker_module,
                "load_nmrpeak_chf_runtime",
                return_value=runtime,
            ) as load_runtime,
            patch.object(
                worker_module,
                "serve_loaded_nmrpeak_runtime",
                side_effect=serve,
            ),
        ):
            result = worker_module.serve_chf_worker(
                connection,
                checkpoint_ref=FACTS.checkpoint_ref,
                image_input_id=FACTS.image_input_ref,
                boot_generation=BOOT,
            )

        self.assertEqual(result, 0)
        open_checkpoint.assert_called_once_with(FACTS.checkpoint_ref)
        load_runtime.assert_called_once_with(checkpoint)
        self.assertEqual(served, [(connection, runtime, READY, CHF_RUNNER_CODEC)])

    def test_loaded_worker_completes_the_provider_session_and_retires(self) -> None:
        runtime = RecordingRuntime(candidates=["CCO", "OCC"])
        with WorkerHarness(runtime) as harness:
            session = RunnerSession.admit(harness.provider, FACTS, DEADLINES, CHF_RUNNER_CODEC)
            validated = validate(session)
            self.assertIsInstance(validated, ValidatedRunnerRequest)
            assert isinstance(validated, ValidatedRunnerRequest)
            generated = session.generate(validated)
            session.retire()

        self.assertEqual(generated.value, ["CCO", "OCC"])
        self.assertEqual(runtime.validated, [MODEL_INPUT])
        self.assertEqual(runtime.generated, [MODEL_INPUT])
        self.assertEqual(harness.result, 0)

    def test_deterministic_rejection_keeps_the_loaded_boot_reusable(self) -> None:
        runtime = RecordingRuntime(rejections=1, candidates=["CCO"])
        with WorkerHarness(runtime) as harness:
            session = RunnerSession.admit(harness.provider, FACTS, DEADLINES, CHF_RUNNER_CODEC)
            rejection = validate(session)
            self.assertIsInstance(rejection, RunnerInputRejected)
            assert isinstance(rejection, RunnerInputRejected)
            self.assertEqual(
                rejection.message,
                "The test runtime rejected this model input.",
            )
            validated = validate(session)
            self.assertIsInstance(validated, ValidatedRunnerRequest)
            assert isinstance(validated, ValidatedRunnerRequest)
            self.assertEqual(session.generate(validated).value, ["CCO"])
            session.retire()

        self.assertEqual(runtime.validated, [MODEL_INPUT, MODEL_INPUT])
        self.assertEqual(runtime.generated, [MODEL_INPUT])
        self.assertEqual(harness.result, 0)

    def test_generate_and_retire_require_the_current_validated_request(self) -> None:
        commands = (
            GenerateFrame(CORRELATION),
            RetireFrame("boot:" + "f" * 32),
        )
        for command in commands:
            with self.subTest(command=type(command).__name__), WorkerHarness(
                RecordingRuntime()
            ) as harness:
                receive_ready(harness.provider)
                harness.provider.sendall(CHF_RUNNER_CODEC.encode(command))

            self.assertIsInstance(harness.failure, RunnerProtocolError)

    def test_retire_rejects_the_current_boot_while_a_request_is_pending(self) -> None:
        with WorkerHarness(RecordingRuntime()) as harness:
            receive_ready(harness.provider)
            harness.provider.sendall(
                CHF_RUNNER_CODEC.encode(ValidateFrame(CORRELATION, MODEL_INPUT))
            )
            self.assertEqual(
                CHF_RUNNER_CODEC.receive(harness.provider),
                ValidatedFrame(CORRELATION),
            )
            harness.provider.sendall(CHF_RUNNER_CODEC.encode(RetireFrame(BOOT)))

        self.assertIsInstance(harness.failure, RunnerProtocolError)

    def test_unexpected_runtime_failures_terminate_the_worker(self) -> None:
        validation_failure = RuntimeError("validation failed")
        with WorkerHarness(
            RecordingRuntime(validation_failure=validation_failure)
        ) as harness:
            session = RunnerSession.admit(harness.provider, FACTS, DEADLINES, CHF_RUNNER_CODEC)
            with self.assertRaises(RunnerSessionRetired):
                validate(session)
        self.assertIs(harness.failure, validation_failure)

        generation_failure = RuntimeError("generation failed")
        with WorkerHarness(
            RecordingRuntime(generation_failure=generation_failure)
        ) as harness:
            session = RunnerSession.admit(harness.provider, FACTS, DEADLINES, CHF_RUNNER_CODEC)
            validated = validate(session)
            assert isinstance(validated, ValidatedRunnerRequest)
            with self.assertRaises(RunnerSessionRetired):
                session.generate(validated)
        self.assertIs(harness.failure, generation_failure)


class RecordingRuntime:
    def __init__(
        self,
        *,
        rejections: int = 0,
        candidates: JsonValue | None = None,
        validation_failure: BaseException | None = None,
        generation_failure: BaseException | None = None,
    ) -> None:
        self.rejections = rejections
        self.candidates = ["CCO"] if candidates is None else candidates
        self.validation_failure = validation_failure
        self.generation_failure = generation_failure
        self.validated: list[ChfRunnerInput] = []
        self.generated: list[ChfRunnerInput] = []

    def validate(self, model_input: ChfRunnerInput) -> None:
        self.validated.append(model_input)
        if self.validation_failure is not None:
            raise self.validation_failure
        if self.rejections:
            self.rejections -= 1
            raise NmrpeakRuntimeInputRejected(
                "The test runtime rejected this model input."
            )

    def generate(self, model_input: ChfRunnerInput) -> JsonValue:
        self.generated.append(model_input)
        if self.generation_failure is not None:
            raise self.generation_failure
        return self.candidates


class CheckpointContext:
    def __init__(self, checkpoint: object) -> None:
        self.checkpoint = checkpoint

    def __enter__(self) -> object:
        return self.checkpoint

    def __exit__(self, *_error: object) -> None:
        return None


class WorkerHarness:
    def __init__(self, runtime: RecordingRuntime) -> None:
        self.runtime = runtime
        self.provider, self.worker = socket.socketpair()
        self.result: int | None = None
        self.failure: BaseException | None = None
        self.thread = Thread(target=self._serve)

    def __enter__(self) -> WorkerHarness:
        self.thread.start()
        return self

    def __exit__(self, *_error: object) -> None:
        self.provider.close()
        self.thread.join(timeout=1)
        self.worker.close()
        if self.thread.is_alive():
            raise AssertionError("CHF worker did not stop after owner closure")

    def _serve(self) -> None:
        try:
            self.result = serve_loaded_nmrpeak_runtime(
                self.worker,
                self.runtime,
                READY,
                CHF_RUNNER_CODEC,
            )
        except BaseException as error:
            self.failure = error
        finally:
            self.worker.close()


def validate(
    session: RunnerSession,
) -> ValidatedRunnerRequest | RunnerInputRejected:
    return session.validate(
        execution_attempt_ref=ATTEMPT_REF,
        provider_attempt_key=ATTEMPT_KEY,
        model_input=MODEL_INPUT,
    )


def receive_ready(connection: socket.socket) -> None:
    received = CHF_RUNNER_CODEC.receive(connection)
    if received != READY:
        raise AssertionError("CHF worker did not publish its measured READY frame")


if __name__ == "__main__":
    unittest.main()
