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
    InterpreterTool,
    InterpreterToolInvocation,
    InterpreterTransportError,
    InterpreterTurn,
    InterpreterUnavailable,
    ReportedInputProblem,
)
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE
from nmrpeak_provider.runner_session import ValidatedRunnerRequest


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


class CapturingSession:
    def __init__(self) -> None:
        self.model_inputs: list[object] = []

    def validate(self, **values: object) -> ValidatedRunnerRequest:
        self.model_inputs.append(values["model_input"])
        return ValidatedRunnerRequest(self, object())


class BoundEndpoints:
    def __init__(self, endpoint: InterpreterEndpoint) -> None:
        self.endpoints = (endpoint,)

    async def join_response_releases(self) -> None:
        pass


class InputInterpreterTests(unittest.TestCase):
    def test_typed_candidate_crosses_existing_parser_and_runner_validation(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        session = CapturingSession()
        validated = run_with_endpoint(call, session=session)

        self.assertIs(type(validated), ValidatedRunnerRequest)
        self.assertEqual(len(session.model_inputs), 1)
        self.assertIs(type(session.model_inputs[0]), ChfRunnerInput)

    def test_reported_input_problem_remains_a_caller_failure(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            return turn(
                InterpreterTool.REPORT_INPUT_PROBLEM,
                {"message": "Provide both proton and carbon peak lists."},
            )

        with self.assertRaises(ReportedInputProblem) as raised:
            run_with_endpoint(call)
        self.assertEqual(
            raised.exception.message,
            "Provide both proton and carbon peak lists.",
        )

    def test_endpoint_failure_is_retryable_and_does_not_log_source_text(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            raise InterpreterTransportError("connect_failed")

        with self.assertLogs(
            "nmrpeak_provider.input_interpreter", level="WARNING"
        ) as logged, self.assertRaises(InterpreterUnavailable):
            run_with_endpoint(call)
        rendered = "\n".join(logged.output)
        self.assertIn("endpoint fake failed while preparing Attempt", rendered)
        self.assertIn("transport/connect_failed", rendered)
        self.assertIn("endpoints_exhausted", rendered)
        self.assertIn("No runner request was validated", rendered)
        self.assertNotIn(SOURCE.decode(), rendered)


def run_with_endpoint(
    call: object,
    *,
    session: CapturingSession | None = None,
) -> ValidatedRunnerRequest:
    endpoint = InterpreterEndpoint("fake", "fake-model", call)
    interpreter = InputInterpreter(
        (object(),),
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
        return_value=BoundEndpoints(endpoint),
    ):
        return interpreter.validate_freeform_input(
            source=SOURCE,
            lane=CHF_LIFECYCLE_LANE,
            session=session or CapturingSession(),
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
