"""Prove prose translation cannot bypass product or runner admission."""

from __future__ import annotations

import unittest
from typing import cast
from unittest.mock import patch

from nmrpeak_provider.chf_binding import ChfRunnerInput
from nmrpeak_provider.input_interpreter import (
    InputInterpreter,
    _value_schema_for,
)
from nmrpeak_provider.interpreter_policy import (
    InterpreterPolicy,
    OpenAIChatCallPolicy,
)
from nmrpeak_provider.interpreter import (
    CandidateConstructionExhausted,
    InterpreterEndpoint,
    InterpreterCall,
    InterpreterPrompt,
    InterpreterProtocolError,
    InterpretationRejected,
    InterpreterTool,
    InterpreterToolInvocation,
    InterpreterTransportError,
    InterpreterTurn,
    InterpreterUnavailable,
    ReportedInputProblem,
)
from nmrpeak_provider.lifecycle_lane import (
    CHF_LIFECYCLE_LANE,
    HF_LIFECYCLE_LANE,
    LifecycleLane,
)
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec
from nmrpeak_provider.product_input import InputRejected, InputRejectionReason
from nmrpeak_provider.runner_session import RunnerInputRejected, ValidatedRunnerRequest
from nmrpeak_provider.runner_protocol import (
    RunnerRejectionReason,
    runner_rejection_diagnostic,
)


SOURCE = b"Formula C2H6O. 1H: 1.25 (t, 3H, J 7.1 Hz). 13C: 58.1."
VALUE = {
    "schema_id": "nmrpeak.structure_generation.request.v1",
    "model_input": {
        "formula": "C2H6O",
        "spectra": {
            "1H": {
                "peaks": [
                    {
                        "shift_lo": "1.25",
                        "shift_hi": "1.25",
                        "integral": "3",
                        "multiplicity": "t",
                        "j_hz": ["7.1"],
                    }
                ]
            },
            "13C": {"peaks": [{"shift": "58.1"}]},
        },
    },
}
HF_VALUE = {
    "schema_id": "nmrpeak.structure_generation.request.v1",
    "model_input": {
        "formula": "C2H6O",
        "spectra": {"1H": VALUE["model_input"]["spectra"]["1H"]},
    },
}


class CapturingSession:
    def __init__(self, *, reject_count: int = 0) -> None:
        self.model_inputs: list[object] = []
        self.reject_count = reject_count

    def validate(
        self,
        *,
        execution_attempt_ref: str,
        provider_attempt_key: str,
        model_input: object,
    ) -> RunnerInputRejected | ValidatedRunnerRequest:
        self.model_inputs.append(model_input)
        if self.reject_count:
            self.reject_count -= 1
            return RunnerInputRejected(
                runner_rejection_diagnostic(
                    RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED, 512
                ),
                512,
            )
        return ValidatedRunnerRequest(self, object())


class BoundEndpoints:
    def __init__(self, endpoints: tuple[InterpreterEndpoint, ...]) -> None:
        self.endpoints = endpoints

    async def join_response_releases(self) -> None:
        pass


class InputInterpreterTests(unittest.TestCase):
    def test_hf_and_chf_tool_schemas_match_the_product_shapes(self) -> None:
        for lane, expected_nuclei in (
            (HF_LIFECYCLE_LANE, {"1H"}),
            (CHF_LIFECYCLE_LANE, {"1H", "13C"}),
        ):
            with self.subTest(lane=lane.offering.implementation_ref):
                schema = _value_schema_for(lane)
                self.assertEqual(
                    schema["properties"]["schema_id"]["const"],
                    "nmrpeak.structure_generation.request.v1",
                )
                spectra = schema["properties"]["model_input"]["properties"]["spectra"]
                self.assertEqual(set(spectra["required"]), expected_nuclei)
                self.assertEqual(set(spectra["properties"]), expected_nuclei)
                peak = spectra["properties"]["1H"]["properties"]["peaks"]["items"]
                self.assertEqual(
                    set(peak["required"]),
                    {"shift_lo", "shift_hi", "integral", "multiplicity", "j_hz"},
                )
                self.assertEqual(peak["properties"]["integral"]["type"], "string")
                self.assertEqual(peak["properties"]["multiplicity"], {"type": "string"})
                self.assertEqual(
                    peak["properties"]["j_hz"]["items"]["type"], "string"
                )

    def test_each_lane_delivers_its_prompt_and_source_separately(self) -> None:
        for lane, value, required_text, excluded_text in (
            (
                HF_LIFECYCLE_LANE,
                HF_VALUE,
                "At least one proton peak is required",
                "Both spectra are required",
            ),
            (
                CHF_LIFECYCLE_LANE,
                VALUE,
                "Both spectra are required",
                "At least one proton peak is required",
            ),
        ):
            prompts: list[InterpreterPrompt] = []

            async def call(prompt: InterpreterPrompt) -> InterpreterTurn:
                prompts.append(prompt)
                return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

            with self.subTest(lane=lane.offering.implementation_ref):
                run_with_endpoint(call, lane=lane)
                self.assertEqual(len(prompts), 1)
                messages = prompts[0]
                self.assertEqual(
                    [message["role"] for message in messages],
                    ["system", "user", "user"],
                )
                self.assertIn(required_text, messages[1]["content"])
                self.assertNotIn(excluded_text, messages[1]["content"])
                self.assertEqual(messages[2]["content"], SOURCE.decode())

    def test_typed_candidate_crosses_existing_parser_and_runner_validation(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        session = CapturingSession()
        validated = run_with_endpoint(call, session=session)

        self.assertIs(type(validated), ValidatedRunnerRequest)
        self.assertEqual(len(session.model_inputs), 1)
        self.assertIs(type(session.model_inputs[0]), ChfRunnerInput)

    def test_protocol_repair_returns_the_exact_failure_to_the_model(self) -> None:
        prompts: list[InterpreterPrompt] = []
        turns = iter(
            (
                InterpreterTurn(
                    assistant_message={"role": "assistant", "content": None},
                    invocation=InterpreterToolInvocation("unknown_tool", {}),
                    tool_call_ids=("call-1",),
                ),
                turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE}),
            )
        )

        async def call(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return next(turns)

        run_with_endpoint(call)

        repair = prompts[1][3:]
        self.assertEqual(
            [message["role"] for message in repair],
            ["assistant", "tool", "user"],
        )
        self.assertEqual(repair[1]["content"], "unexpected_tool_invocation")
        self.assertIn("The rejection is not evidence", repair[2]["content"])
        self.assertNotEqual(repair[1]["content"], repair[2]["content"])

    def test_product_rejection_repairs_then_exposes_typed_candidate_issue(self) -> None:
        invalid_value = VALUE | {
            "model_input": VALUE["model_input"] | {"formula": ""}
        }
        prompts: list[InterpreterPrompt] = []

        async def call(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(
                InterpreterTool.SUBMIT_INTERPRETATION,
                {"value": invalid_value},
            )

        with self.assertRaises(CandidateConstructionExhausted) as raised:
            run_with_endpoint(call)

        self.assertIs(
            raised.exception.issue.reason,
            InputRejectionReason.INVALID_FORMULA,
        )
        self.assertEqual(
            raised.exception.issue.pointer,
            "/model_input/formula",
        )
        self.assertEqual(len(prompts), 3)
        self.assertIn("/model_input/formula", prompts[1][-2]["content"])
        self.assertNotIn("invalid_formula", str(raised.exception))

    def test_non_json_candidate_preserves_the_encoder_failure(self) -> None:
        invalid_value = VALUE | {"unexpected_number": 1.5}

        async def call(_prompt: object) -> InterpreterTurn:
            return turn(
                InterpreterTool.SUBMIT_INTERPRETATION,
                {"value": invalid_value},
            )

        with self.assertRaises(InterpreterUnavailable) as raised:
            run_with_endpoint(call)

        self.assertIsInstance(raised.exception.__cause__, ExceptionGroup)
        assert isinstance(raised.exception.__cause__, ExceptionGroup)
        protocol_failure = raised.exception.__cause__.exceptions[0]
        self.assertIsInstance(protocol_failure, InterpreterProtocolError)
        self.assertEqual(
            str(protocol_failure),
            "unsupported canonical JSON type: float",
        )
        self.assertIsInstance(protocol_failure.__cause__, TypeError)
        self.assertEqual(
            str(protocol_failure.__cause__),
            "unsupported canonical JSON type: float",
        )

    def test_invalid_freeform_utf8_retains_the_decoder_failure(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            raise AssertionError("Invalid source text must not reach an endpoint")

        with self.assertRaises(InputRejected) as raised:
            run_with_endpoint(call, source=b"\xff")

        self.assertIs(raised.exception.reason, InputRejectionReason.INVALID_UTF8)
        self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)

    def test_empty_or_controlled_freeform_source_stops_before_endpoint(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            raise AssertionError("Invalid source text must not reach an endpoint")

        for source, reason in (
            (b"", InputRejectionReason.EMPTY_INPUT),
            (b"Formula C2H6O\x00", InputRejectionReason.DISALLOWED_CONTROL),
            (b"Formula C2H6O\xe2\x80\x8b", InputRejectionReason.DISALLOWED_CONTROL),
        ):
            with self.subTest(source=source), self.assertRaises(InputRejected) as raised:
                run_with_endpoint(call, source=source)
            self.assertIs(raised.exception.reason, reason)

    def test_runner_rejection_repairs_on_the_same_endpoint(self) -> None:
        prompts: list[InterpreterPrompt] = []

        async def call(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        session = CapturingSession(reject_count=1)
        validated = run_with_endpoint(call, session=session)
        self.assertIs(type(validated), ValidatedRunnerRequest)
        self.assertEqual(len(session.model_inputs), 2)
        self.assertEqual(
            [[message["role"] for message in prompt] for prompt in prompts],
            [["system", "user", "user"],
             ["system", "user", "user", "assistant", "tool", "user"]],
        )
        self.assertEqual(
            prompts[1][-2]["content"],
            "The tokenizer produced 512 input tokens; this model accepts at most 511.",
        )

    def test_runner_rejection_falls_back_with_a_fresh_prompt(self) -> None:
        prompts: list[InterpreterPrompt] = []

        async def call(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        session = CapturingSession(reject_count=3)
        validated = run_with_endpoints((call, call), session=session)

        self.assertIs(type(validated), ValidatedRunnerRequest)
        self.assertEqual(len(session.model_inputs), 4)
        self.assertEqual(
            [[message["role"] for message in prompt] for prompt in prompts],
            [
                ["system", "user", "user"],
                ["system", "user", "user", "assistant", "tool", "user"],
                ["system", "user", "user", "assistant", "tool", "user",
                 "assistant", "tool", "user"],
                ["system", "user", "user"],
            ],
        )

    def test_runner_rejection_requires_complete_repair_route(self) -> None:
        async def call(_prompt: InterpreterPrompt) -> InterpreterTurn:
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        with self.assertRaises(InterpretationRejected) as raised:
            run_with_endpoints(
                (call, call),
                session=CapturingSession(reject_count=6),
            )
        self.assertEqual(
            raised.exception.message,
            "The tokenizer produced 512 input tokens; this model accepts at most 511.",
        )
        self.assertEqual(raised.exception.token_count, 512)

    def test_single_endpoint_report_remains_unverified_model_evidence(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            return turn(
                InterpreterTool.REPORT_INPUT_PROBLEM,
                {
                    "message": (
                        "The carbon-13 peak list is missing. Submit a new Job with both "
                        "proton and carbon-13 peak lists."
                    )
                },
            )

        with self.assertRaises(ReportedInputProblem) as raised:
            run_with_endpoint(call)
        self.assertEqual(
            raised.exception.message,
            "The carbon-13 peak list is missing. Submit a new Job with both proton and "
            "carbon-13 peak lists.",
        )

    def test_model_report_falls_back_to_a_validated_candidate(self) -> None:
        prompts: list[InterpreterPrompt] = []

        async def report(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(
                InterpreterTool.REPORT_INPUT_PROBLEM,
                {"message": "The integral is missing."},
            )

        async def submit(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        validated = run_with_endpoints((report, submit))
        self.assertIs(type(validated), ValidatedRunnerRequest)
        self.assertEqual(
            [[item["role"] for item in prompt] for prompt in prompts],
            [["system", "user", "user"], ["system", "user", "user"]],
        )

    def test_all_endpoint_reports_remain_model_evidence(self) -> None:
        async def report(_prompt: InterpreterPrompt) -> InterpreterTurn:
            return turn(
                InterpreterTool.REPORT_INPUT_PROBLEM,
                {"message": "Model claim about source; unverified."},
            )

        with self.assertRaises(ReportedInputProblem) as raised:
            run_with_endpoints((report, report))
        self.assertEqual(str(raised.exception), "reported_input_problem")

    def test_report_mixed_with_transport_or_runner_rejection_is_unavailable(self) -> None:
        async def report(_prompt: InterpreterPrompt) -> InterpreterTurn:
            return turn(
                InterpreterTool.REPORT_INPUT_PROBLEM,
                {"message": "Model claim about source; unverified."},
            )

        async def transport(_prompt: InterpreterPrompt) -> InterpreterTurn:
            raise InterpreterTransportError("connect_failed")

        async def candidate(_prompt: InterpreterPrompt) -> InterpreterTurn:
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        with self.assertRaises(InterpreterUnavailable):
            run_with_endpoints((report, transport))
        with self.assertRaises(InterpreterUnavailable):
            run_with_endpoints(
                (report, candidate),
                session=CapturingSession(reject_count=3),
            )

    def test_constructor_rejection_mixed_with_transport_is_unavailable(self) -> None:
        invalid_value = VALUE | {
            "model_input": VALUE["model_input"] | {"formula": ""}
        }

        async def invalid(_prompt: InterpreterPrompt) -> InterpreterTurn:
            return turn(
                InterpreterTool.SUBMIT_INTERPRETATION,
                {"value": invalid_value},
            )

        async def transport(_prompt: InterpreterPrompt) -> InterpreterTurn:
            raise InterpreterTransportError("connect_failed")

        with self.assertRaises(InterpreterUnavailable):
            run_with_endpoints((invalid, transport))

    def test_endpoint_failure_is_retryable_and_does_not_log_source_text(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            raise InterpreterTransportError("connect_failed")

        with self.assertLogs(
            "nmrpeak_provider.input_interpreter", level="WARNING"
        ) as logged, self.assertRaises(InterpreterUnavailable) as raised:
            run_with_endpoint(call)
        rendered = "\n".join(logged.output)
        self.assertIn("endpoint fake-1 failed while preparing Attempt", rendered)
        self.assertIn("transport/connect_failed", rendered)
        self.assertIn("endpoints_exhausted", rendered)
        self.assertIn("No runner request was validated", rendered)
        self.assertNotIn(SOURCE.decode(), rendered)
        self.assertIsInstance(raised.exception.__cause__, ExceptionGroup)
        assert isinstance(raised.exception.__cause__, ExceptionGroup)
        self.assertEqual(len(raised.exception.__cause__.exceptions), 1)
        self.assertIsInstance(
            raised.exception.__cause__.exceptions[0],
            InterpreterTransportError,
        )

    def test_endpoint_protocol_failure_is_preserved_as_the_unavailable_cause(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            return cast(InterpreterTurn, object())

        with self.assertRaises(InterpreterUnavailable) as raised:
            run_with_endpoint(call)

        self.assertIsInstance(raised.exception.__cause__, ExceptionGroup)
        assert isinstance(raised.exception.__cause__, ExceptionGroup)
        self.assertEqual(len(raised.exception.__cause__.exceptions), 1)
        protocol_failure = raised.exception.__cause__.exceptions[0]
        self.assertIsInstance(protocol_failure, InterpreterProtocolError)
        self.assertEqual(str(protocol_failure), "invalid_turn_type")


def run_with_endpoint(
    call: InterpreterCall,
    *,
    source: bytes = SOURCE,
    lane: LifecycleLane = CHF_LIFECYCLE_LANE,
    session: CapturingSession | None = None,
) -> ValidatedRunnerRequest:
    return run_with_endpoints((call,), source=source, lane=lane, session=session)


def run_with_endpoints(
    calls: tuple[InterpreterCall, ...],
    *,
    source: bytes = SOURCE,
    lane: LifecycleLane = CHF_LIFECYCLE_LANE,
    session: CapturingSession | None = None,
) -> ValidatedRunnerRequest:
    endpoints = tuple(
        InterpreterEndpoint(f"fake-{index}", call)
        for index, call in enumerate(calls, start=1)
    )
    interpreter = InputInterpreter(
        (
            OpenAIChatEndpointSpec(
                configuration_id="configured",
                base_url="https://interpreter.invalid/v1",
                api_key="test-key",
                model="configured-model",
                reasoning_effort=None,
            ),
        ),
        InterpreterPolicy(
            call_policy=OpenAIChatCallPolicy(
                request_timeout_seconds=1,
                turn_timeout_seconds=3,
            ),
            interpretation_timeout_seconds=1,
        ),
    )
    with patch(
        "nmrpeak_provider.input_interpreter.bind_openai_chat_endpoints",
        return_value=BoundEndpoints(endpoints),
    ):
        return interpreter.validate_freeform_input(
            source=source,
            lane=lane,
            session=session if session is not None else CapturingSession(),
            execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
            provider_attempt_key="provider-attempt:test",
        )


def turn(name: InterpreterTool, arguments: object) -> InterpreterTurn:
    return InterpreterTurn(
        assistant_message={"role": "assistant", "content": None},
        invocation=InterpreterToolInvocation(name.value, arguments),
        tool_call_ids=("call-1",),
    )


if __name__ == "__main__":
    unittest.main()
