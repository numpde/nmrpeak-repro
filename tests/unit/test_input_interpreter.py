"""Prove prose translation cannot bypass product or runner admission."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from nmrpeak_provider.chf_binding import ChfRunnerInput
from nmrpeak_provider.input_interpreter import (
    InputInterpreter,
)
from nmrpeak_provider.interpreter_policy import (
    InterpreterPolicy,
    OpenAIChatCallPolicy,
)
from nmrpeak_provider.interpreter import (
    InterpreterEndpoint,
    InterpreterCall,
    InterpreterPrompt,
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
    def __init__(self, *, reject_first: bool = False) -> None:
        self.model_inputs: list[object] = []
        self.reject_first = reject_first

    def validate(
        self,
        *,
        execution_attempt_ref: str,
        provider_attempt_key: str,
        model_input: object,
    ) -> RunnerInputRejected | ValidatedRunnerRequest:
        self.model_inputs.append(model_input)
        if self.reject_first:
            self.reject_first = False
            return RunnerInputRejected("The loaded runner rejected this input.")
        return ValidatedRunnerRequest(self, object())


class BoundEndpoints:
    def __init__(self, endpoints: tuple[InterpreterEndpoint, ...]) -> None:
        self.endpoints = endpoints

    async def join_response_releases(self) -> None:
        pass


class InputInterpreterTests(unittest.TestCase):
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

    def test_product_rejection_propagates_its_exact_reason(self) -> None:
        invalid_value = VALUE | {
            "model_input": VALUE["model_input"] | {"formula": ""}
        }

        async def call(_prompt: object) -> InterpreterTurn:
            return turn(
                InterpreterTool.SUBMIT_INTERPRETATION,
                {"value": invalid_value},
            )

        with self.assertRaises(InputRejected) as raised:
            run_with_endpoint(call)

        self.assertIs(raised.exception.reason, InputRejectionReason.INVALID_FORMULA)
        self.assertEqual(str(raised.exception), "invalid_formula")

    def test_runner_rejection_does_not_ask_the_model_to_explain_it(self) -> None:
        prompts: list[InterpreterPrompt] = []

        async def call(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        with self.assertRaises(InterpretationRejected) as raised:
            run_with_endpoint(call, session=CapturingSession(reject_first=True))
        self.assertEqual(
            raised.exception.message,
            "The loaded runner rejected this input.",
        )
        self.assertEqual(len(prompts), 1)

    def test_runner_rejection_falls_back_with_a_fresh_prompt(self) -> None:
        prompts: list[InterpreterPrompt] = []

        async def call(prompt: InterpreterPrompt) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        session = CapturingSession(reject_first=True)
        validated = run_with_endpoints((call, call), session=session)

        self.assertIs(type(validated), ValidatedRunnerRequest)
        self.assertEqual(len(session.model_inputs), 2)
        self.assertEqual(
            [[message["role"] for message in prompt] for prompt in prompts],
            [["system", "user", "user"], ["system", "user", "user"]],
        )

    def test_reported_input_problem_remains_a_caller_failure(self) -> None:
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

    def test_endpoint_failure_is_retryable_and_does_not_log_source_text(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            raise InterpreterTransportError("connect_failed")

        with self.assertLogs(
            "nmrpeak_provider.input_interpreter", level="WARNING"
        ) as logged, self.assertRaises(InterpreterUnavailable):
            run_with_endpoint(call)
        rendered = "\n".join(logged.output)
        self.assertIn("endpoint fake-1 failed while preparing Attempt", rendered)
        self.assertIn("transport/connect_failed", rendered)
        self.assertIn("endpoints_exhausted", rendered)
        self.assertIn("No runner request was validated", rendered)
        self.assertNotIn(SOURCE.decode(), rendered)


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
