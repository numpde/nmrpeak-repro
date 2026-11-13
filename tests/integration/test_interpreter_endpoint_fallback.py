"""Exercise production interpretation, HTTP adaptation, repair, and fallback offline.

Adapted from Magnet's endpoint-fallback integration test. Only the HTTP peer is
simulated; NMRPeak's product constructor remains the final candidate authority.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from tests.fakes.model_http import LoopbackHTTP, Reply, reply

from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.interpretation_capability import _InterpretationCapability
from nmrpeak_provider.interpreter import (
    InterpreterUnavailable,
    InterpreterUnavailableReason,
)
from nmrpeak_provider.interpreter_policy import InterpreterPolicy
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec


_FIXTURES = Path(__file__).parents[1] / "model_behavior/fixtures"


def _ethanol_source_and_value(lane_name: str) -> tuple[str, dict[str, object]]:
    corpus = json.loads((_FIXTURES / "interpreter_cases.json").read_text("utf-8"))
    scenario = next(item for item in corpus["scenarios"] if item["id"] == "ethanol_point")
    variant = scenario["variants"][lane_name]
    source = (_FIXTURES / variant["source_file"]).read_text("utf-8")
    value = variant["expected_interpretation"]
    peak = value["model_input"]["spectra"]["1H"]["peaks"][0]
    if peak["shift_lo"] != peak["shift_hi"]:
        raise AssertionError("The ethanol fixture must contain a point-valued proton shift")
    source_lines = (
        f"Molecular formula: {value['model_input']['formula']}.",
        f"Unassigned 1H peak: {peak['shift_lo']} ppm, multiplicity "
        f"{peak['multiplicity']}, integral {peak['integral']} H, "
        f"J {peak['j_hz'][0]} Hz.",
    )
    for line in source_lines:
        if line not in source.splitlines():
            raise AssertionError("The integration candidate has a fact absent from its source")
    if lane_name == "chf":
        carbon = value["model_input"]["spectra"]["13C"]["peaks"][0]["shift"]
        if f"Unassigned 13C peak: {carbon} ppm." not in source.splitlines():
            raise AssertionError("The integration candidate has a carbon shift absent from its source")
    return source, value


def _completion(value: object, *, tool: str = "submit_interpretation", call_id: str = "fixture-call") -> dict[str, object]:
    arguments = {"message": "The source may be incomplete."} if tool == "report_interpretation_problem" else {"value": value}
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "reasoning_content": "Private fixture reasoning.",
        "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": tool, "arguments": json.dumps(arguments),
        }}],
    }}]}


class InterpreterEndpointFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_repair(self, *, transient):
        source, value = _ethanol_source_and_value("hf")
        invalid = deepcopy(value)
        invalid["model_input"]["spectra"]["1H"]["peaks"][0]["multiplicity"] = "unsupported"
        first = reply({"error": {"message": "private service detail"}}, status=503) if transient else reply(
            _completion(invalid, call_id="invalid"))
        server = await LoopbackHTTP([first, reply(_completion(value, call_id="valid"))]).start()
        try:
            spec = OpenAIChatEndpointSpec(configuration_id="primary", url=server.base_url + "/chat/completions",
                                          api_key="fixture-only", model="primary", trust=server.trust)
            session = InputInterpreter((spec,), InterpreterPolicy(interpretation_timeout_seconds=5)).new_session()
            validate = AsyncMock(side_effect=lambda candidate: candidate)
            events = []
            try:
                result = await session.validate_freeform_input_async(
                    source=source.encode(), lane=HF_LIFECYCLE_LANE, validate_input=validate,
                    execution_attempt_ref="attempt", record_diagnostic=events.append)
                expected = HF_LIFECYCLE_LANE.bind_runner_input(_InterpretationCapability(HF_LIFECYCLE_LANE).construct_interpretation(value))
                self.assertEqual(result, expected)
                self.assertEqual(len(server.requests), 2)
                validate.assert_awaited_once()
                if transient:
                    self.assertTrue(any("http_status=503" in event for event in events))
                    self.assertNotIn("private service detail", str(events))
            finally:
                await session.retire()
                await session.join()
        finally:
            await server.close()

    async def test_transient_service_failure_retries_same_endpoint(self):
        await self.exercise_repair(transient=True)

    async def test_rejected_candidate_is_corrected_before_runner_admission(self):
        await self.exercise_repair(transient=False)

    async def test_report_handoff_keeps_source_and_accepts_next_model(self):
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            source, value = _ethanol_source_and_value(lane.offering.implementation_ref)
            primary = await LoopbackHTTP([reply(_completion(value, tool="report_interpretation_problem"))]).start()
            fallback = await LoopbackHTTP([reply(_completion(value))]).start()
            try:
                specs = tuple(OpenAIChatEndpointSpec(
                    configuration_id=name, url=server.base_url + "/chat/completions",
                    api_key="fixture-only", model=name, trust=server.trust,
                ) for name, server in (("primary", primary), ("fallback", fallback)))
                session = InputInterpreter(specs, InterpreterPolicy(interpretation_timeout_seconds=10)).new_session()
                validate = AsyncMock(side_effect=lambda value: value)
                try:
                    result = await session.validate_freeform_input_async(
                        source=source.encode(), lane=lane, validate_input=validate,
                        execution_attempt_ref="attempt")
                    self.assertEqual(result, lane.bind_runner_input(_InterpretationCapability(lane).construct_interpretation(value)))
                    validate.assert_awaited_once()
                    second = json.loads(fallback.requests[0].content)
                    self.assertTrue(any(message.get("content") == source for message in second["messages"]))
                    self.assertNotIn("The source may be incomplete.", json.dumps(second))
                    self.assertEqual(len(primary.requests), 1)
                    self.assertEqual(len(fallback.requests), 1)
                finally:
                    await session.retire()
                    await session.join()
            finally:
                await primary.close()
                await fallback.close()

    async def test_all_unavailable_stays_operational_failure(self):
        source, _ = _ethanol_source_and_value("hf")
        primary = await LoopbackHTTP([Reply(disconnect=True)] * 2).start()
        try:
            spec = OpenAIChatEndpointSpec(configuration_id="primary", url=primary.base_url + "/chat/completions",
                                          api_key="fixture-only", model="primary", trust=primary.trust)
            session = InputInterpreter((spec,), InterpreterPolicy(interpretation_timeout_seconds=5)).new_session()
            validate = AsyncMock()
            try:
                with self.assertRaises(InterpreterUnavailable) as caught:
                    await session.validate_freeform_input_async(source=source.encode(), lane=HF_LIFECYCLE_LANE,
                                                               validate_input=validate, execution_attempt_ref="attempt")
                self.assertIs(caught.exception.reason, InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED)
                validate.assert_not_awaited()
                self.assertEqual(len(primary.requests), 2)
            finally:
                await session.retire()
                await session.join()
        finally:
            await primary.close()
