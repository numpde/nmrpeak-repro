"""Observe request-input release at the HF/CHF result publication boundary."""

from dataclasses import replace
import unittest
import weakref

from families.nmrpeak.runner_worker import serve_loaded_nmrpeak_runtime
from families.nmrpeak.runner_runtime import NmrpeakRuntimeInputRejected
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CODEC
from nmrpeak_provider.hf_binding import HfRunnerInput
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CODEC, HF_RUNNER_CONTRACT_ID
from nmrpeak_provider.product_result import HF_RESULT_IDENTITY
from nmrpeak_provider.runner_protocol import (
    GenerateFrame,
    ResultFrame,
    RejectedFrame,
    RunnerRejectionReason,
    RetireFrame,
    ValidateFrame,
)
from tests.unit.test_chf_worker import CORRELATION, MODEL_INPUT, READY


class TrackedInput:
    def __init__(self, value):
        self.value = value

    def wire_document(self):
        return self.value.wire_document()


class InputReleasingRuntime:
    def validate(self, model_input):
        pass

    def generate(self, model_input):
        return ["CCO"]


class WorkerQuiescenceTests(unittest.TestCase):
    def test_token_rejection_releases_input_before_publishing_the_response(self):
        decoded_inputs = []

        def parse_input(document):
            value = TrackedInput(CHF_RUNNER_CODEC.parse_model_input(document))
            decoded_inputs.append(weakref.ref(value))
            return value

        class RejectedRuntime(InputReleasingRuntime):
            def validate(self, model_input):
                raise NmrpeakRuntimeInputRejected(RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED, 512)

        codec = replace(CHF_RUNNER_CODEC, model_input_type=TrackedInput, parse_model_input=parse_input)
        connection = ScriptedConnection(
            CHF_RUNNER_CODEC.encode(ValidateFrame(CORRELATION, MODEL_INPUT))
            + CHF_RUNNER_CODEC.encode(RetireFrame(READY.boot_generation)), CHF_RUNNER_CODEC,
            lambda: self.assertTrue(decoded_inputs and all(value() is None for value in decoded_inputs)),
        )
        self.assertEqual(serve_loaded_nmrpeak_runtime(connection, RejectedRuntime(), READY, codec), 0)
        self.assertIsInstance(connection.results[0], RejectedFrame)

    def test_each_result_releases_its_input_before_another_attempt_can_reuse_the_boot(self):
        hf_ready = replace(
            READY,
            runner_ref=HF_RESULT_IDENTITY.runner_ref,
            runner_contract_id=HF_RUNNER_CONTRACT_ID,
            decode_policy_id=HF_RESULT_IDENTITY.decode_policy.decode_policy_id,
        )
        for codec, model_input, ready in (
            (HF_RUNNER_CODEC, HfRunnerInput(MODEL_INPUT.molecular_formula, MODEL_INPUT.proton_peaks), hf_ready),
            (CHF_RUNNER_CODEC, MODEL_INPUT, READY),
        ):
            with self.subTest(lane=codec.lane_name):
                decoded_inputs = []

                def parse_input(document):
                    value = TrackedInput(codec.parse_model_input(document))
                    decoded_inputs.append(weakref.ref(value))
                    return value

                observed_codec = replace(codec, model_input_type=TrackedInput, parse_model_input=parse_input)
                second = replace(
                    CORRELATION,
                    correlation_id="request:" + "7" * 32,
                    attempt_ref="execution_attempt:sha256:" + "8" * 64,
                    provider_attempt_key="nmrpeak-provider.v1:" + "9" * 64,
                )
                commands = (
                    ValidateFrame(CORRELATION, model_input), GenerateFrame(CORRELATION),
                    ValidateFrame(second, model_input), GenerateFrame(second),
                    RetireFrame(ready.boot_generation),
                )
                connection = ScriptedConnection(
                    b"".join(codec.encode(command) for command in commands),
                    codec,
                    lambda: self.assertTrue(
                        decoded_inputs and all(value() is None for value in decoded_inputs),
                        "Result publication still retains an earlier request's input",
                    ),
                )

                self.assertEqual(
                    serve_loaded_nmrpeak_runtime(connection, InputReleasingRuntime(), ready, observed_codec),
                    0,
                )
                self.assertEqual(
                    connection.results,
                    [ResultFrame(CORRELATION, ["CCO"]), ResultFrame(second, ["CCO"])],
                )
                self.assertEqual(len(decoded_inputs), 2)


class ScriptedConnection:
    def __init__(self, incoming, codec, before_result):
        self.incoming = bytearray(incoming)
        self.codec = codec
        self.before_result = before_result
        self.results = []

    def recv_into(self, destination):
        count = min(len(destination), len(self.incoming))
        destination[:count] = self.incoming[:count]
        del self.incoming[:count]
        return count

    def sendall(self, data):
        frame = self.codec.decode_frame(data)
        if type(frame) in (ResultFrame, RejectedFrame):
            self.before_result()
            self.results.append(frame)
