"""Prove prose translation cannot bypass product or runner admission."""

from __future__ import annotations

import unittest
from typing import cast
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
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.runner_session import RunnerInputRejected, ValidatedRunnerRequest


SOURCE = b"Formula C2H6O. 1H: 1.25 (t, 3H, J 7.1 Hz). 13C: 58.1."
ODD_FORMULA_SOURCE = (
    b"Formula C16H27NO8S. 1H: 1.25 (t, 3H, J 7.1 Hz). 13C: 58.1."
)
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
        "formula": "C16H27NO8S",
        "spectra": {"1H": VALUE["model_input"]["spectra"]["1H"]},
    },
}
CHF_VALUE = {
    "schema_id": "nmrpeak.structure_generation.request.v1",
    "model_input": VALUE["model_input"] | {"formula": "C16H27NO8S"},
}


class CapturingSession:
    def __init__(self, *, reject_first: bool = False) -> None:
        self.model_inputs: list[object] = []
        self.reject_first = reject_first

    def validate(
        self, **values: object
    ) -> RunnerInputRejected | ValidatedRunnerRequest:
        self.model_inputs.append(values["model_input"])
        if self.reject_first:
            self.reject_first = False
            return RunnerInputRejected()
        return ValidatedRunnerRequest(self, object())


class BoundEndpoints:
    def __init__(self, endpoint: InterpreterEndpoint) -> None:
        self.endpoints = (endpoint,)

    async def join_response_releases(self) -> None:
        pass


class InputInterpreterTests(unittest.TestCase):
    def test_both_prompts_limit_formula_handling_to_transcription(self) -> None:
        for lane, value, required_text, excluded_text in (
            (
                HF_LIFECYCLE_LANE,
                HF_VALUE,
                "At least one proton peak is required",
                "Both spectra are required",
            ),
            (
                CHF_LIFECYCLE_LANE,
                CHF_VALUE,
                "Both spectra are required",
                "At least one proton peak is required",
            ),
        ):
            prompts: list[object] = []

            async def call(prompt: object) -> InterpreterTurn:
                prompts.append(prompt)
                return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

            with self.subTest(lane=lane.offering.implementation_ref):
                run_with_endpoint(call, source=ODD_FORMULA_SOURCE, lane=lane)
                self.assertEqual(len(prompts), 1)
                messages = cast(list[dict[str, str]], prompts[0])
                self.assertEqual(
                    [message["role"] for message in messages],
                    ["system", "user", "user"],
                )
                self.assertIn(
                    "transcription into the selected JSON shape",
                    messages[0]["content"],
                )
                self.assertIn(
                    "Copy the complete molecular formula exactly",
                    messages[1]["content"],
                )
                self.assertIn(required_text, messages[1]["content"])
                self.assertNotIn(excluded_text, messages[1]["content"])
                self.assertNotIn("formula must be neutral", messages[1]["content"])
                self.assertEqual(messages[2]["content"], ODD_FORMULA_SOURCE.decode())

    def test_typed_candidate_crosses_existing_parser_and_runner_validation(self) -> None:
        async def call(_prompt: object) -> InterpreterTurn:
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        session = CapturingSession()
        validated = run_with_endpoint(call, session=session)

        self.assertIs(type(validated), ValidatedRunnerRequest)
        self.assertEqual(len(session.model_inputs), 1)
        self.assertIs(type(session.model_inputs[0]), ChfRunnerInput)

    def test_runner_repair_does_not_invite_changes_to_supplied_values(self) -> None:
        prompts: list[object] = []

        async def call(prompt: object) -> InterpreterTurn:
            prompts.append(prompt)
            return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": VALUE})

        run_with_endpoint(call, session=CapturingSession(reject_first=True))

        self.assertEqual(len(prompts), 2)
        repaired = cast(list[dict[str, str]], prompts[1])
        self.assertEqual(repaired[-2]["role"], "tool")
        self.assertIn(
            "preserve the caller's supplied values",
            repaired[-2]["content"],
        )
        self.assertNotIn("corrected complete document", repaired[-2]["content"])
        self.assertIn(
            "cannot accept the supplied input unchanged",
            repaired[-2]["content"],
        )
        self.assertEqual(repaired[-1]["role"], "user")
        self.assertIn(
            "Never change a supplied value",
            repaired[-1]["content"],
        )

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
    source: bytes = SOURCE,
    lane: object = CHF_LIFECYCLE_LANE,
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
            source=source,
            lane=lane,
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
