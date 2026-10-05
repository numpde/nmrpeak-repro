"""Exercise production interpretation, HTTP adaptation, repair, and fallback offline.

Adapted from Magnet's endpoint-fallback integration test. Only the HTTP peer is
simulated; NMRPeak's product constructor remains the final candidate authority.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from nmrpeak_provider.input_interpreter import _InterpretationCapability, _value_schema_for
from nmrpeak_provider.interpreter import (
    InterpreterUnavailable,
    InterpreterUnavailableReason,
    interpret,
)
from nmrpeak_provider.interpreter_policy import OpenAIChatCallPolicy
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.openai_chat_interpreter import (
    bind_openai_chat_endpoints,
    load_openai_chat_endpoint_specs,
)
from nmrpeak_provider.text_provenance import UserProvidedText


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


def _completion(value: object, *, tool: str = "submit_interpretation") -> dict[str, object]:
    arguments = {"message": "The source may be incomplete."} if tool == "report_input_problem" else {"value": value}
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "reasoning_content": "Private fixture reasoning.",
        "tool_calls": [{"id": "fixture-call", "type": "function", "function": {
            "name": tool, "arguments": json.dumps(arguments),
        }}],
    }}]}


class InterpreterEndpointFallbackTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def route(self, lane, behavior):
        source, value = _ethanol_source_and_value(lane.offering.implementation_ref)
        capability = _InterpretationCapability(lane)
        expected = capability.construct_interpretation(value)
        requests: list[tuple[str, dict[str, object]]] = []
        responses: list[httpx.Response] = []
        failures = []

        async def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            host = request.url.host
            requests.append((host, body))
            primary = host == "primary.example.test"
            if behavior == "all_unavailable" or (primary and behavior == "unavailable"):
                raise httpx.ConnectError("private connection detail", request=request)
            if primary and behavior == "timeout":
                await asyncio.Event().wait()
            if primary and behavior == "busy":
                response = httpx.Response(429, json={"error": {"message": "private busy detail"}})
                responses.append(response)
                return response
            if primary and behavior == "reported":
                response = httpx.Response(200, json=_completion(value, tool="report_input_problem"))
            elif primary and behavior == "constructor" and len(requests) == 1:
                invalid = deepcopy(value)
                invalid["model_input"]["spectra"]["1H"]["peaks"][0]["multiplicity"] = "unsupported-private-label"
                response = httpx.Response(200, json=_completion(invalid))
            elif primary and behavior == "protocol":
                response = httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": None,
                    "reasoning_content": "Private fixture reasoning.",
                    "tool_calls": [{"id": "fixture-call", "type": "function", "function": {
                        "name": "submit_interpretation", "arguments": "not JSON",
                    }}],
                }}]})
            else:
                response = httpx.Response(200, json=_completion(value))
            responses.append(response)
            return response

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, name in (("10-fallback.toml", "fallback"), ("05-primary.toml", "primary")):
                (root / filename).write_text(
                    f'id = "{name}"\nbase_url = "https://{name}.example.test/v1"\n'
                    f'api_key = "fixture-only"\nmodel = "{name}-model"\n',
                    encoding="utf-8",
                )
            specs = load_openai_chat_endpoint_specs(root)
            self.assertEqual([spec.configuration_id for spec in specs], ["primary", "fallback"])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
                with patch("nmrpeak_provider.openai_chat_interpreter.asyncio.sleep", return_value=None):
                    endpoints = bind_openai_chat_endpoints(
                        specs, OpenAIChatCallPolicy(
                            request_timeout_seconds=0.02 if behavior == "timeout" else 1,
                            turn_timeout_seconds=3,
                        ),
                        http_client=client,
                        submit_interpretation_description="Copy source-supported values.",
                        submit_interpretation_value_schema=_value_schema_for(lane),
                        report_input_problem_description="Report source evidence only.",
                    )

                    async def admit(candidate):
                        return candidate

                    async def run():
                        return await interpret(
                            source_text=UserProvidedText(source),
                            capability=capability,
                            endpoints=endpoints.endpoints,
                            interpretation_timeout_seconds=10,
                            report_endpoint_failure=failures.append,
                            admit_interpretation=admit,
                        )

                    try:
                        yield run, expected, requests, failures
                    finally:
                        await endpoints.join_response_releases()
                        self.assertTrue(all(response.is_closed for response in responses))
                        self.assertTrue(requests)
                        self.assertEqual(requests[0][1]["messages"][2]["content"], source)

    async def test_constructor_repair_preserves_reasoning_and_source_fidelity(self):
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            with self.subTest(lane=lane.offering.implementation_ref):
                async with self.route(lane, "constructor") as (run, expected, requests, failures):
                    result = await run()
                    self.assertEqual(result.admitted, expected)
                    self.assertEqual(result.attempted_configuration_ids, ("primary",))
                    self.assertEqual(len(requests), 2)
                    repair = requests[1][1]["messages"]
                    self.assertEqual(repair[3]["reasoning_content"], "Private fixture reasoning.")
                    self.assertNotIn("unsupported-private-label", json.dumps(repair[4:]))
                    self.assertEqual(failures, [])

    async def test_protocol_repair_then_fallback_starts_fresh(self):
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            with self.subTest(lane=lane.offering.implementation_ref):
                async with self.route(lane, "protocol") as (run, expected, requests, failures):
                    result = await run()
                    self.assertEqual(result.admitted, expected)
                    self.assertEqual(result.attempted_configuration_ids, ("primary", "fallback"))
                    self.assertEqual([len(body["messages"]) for _, body in requests], [3, 6, 9, 3])
                    self.assertEqual(requests[-1][1]["messages"], requests[0][1]["messages"])
                    self.assertEqual(len(failures), 1)

    async def test_report_and_transport_failure_allow_later_admission(self):
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            for behavior in ("reported", "unavailable"):
                with self.subTest(lane=lane.offering.implementation_ref, behavior=behavior):
                    async with self.route(lane, behavior) as (run, expected, requests, failures):
                        result = await run()
                        self.assertEqual(result.admitted, expected)
                        self.assertEqual(result.attempted_configuration_ids, ("primary", "fallback"))
                        self.assertEqual(len(failures), 1)
                        self.assertEqual(len(requests[-1][1]["messages"]), 3)

    async def test_busy_and_timeout_retry_then_fallback(self):
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            for behavior in ("busy", "timeout"):
                with self.subTest(lane=lane.offering.implementation_ref, behavior=behavior):
                    async with self.route(lane, behavior) as (run, expected, requests, failures):
                        result = await run()
                        self.assertEqual(result.admitted, expected)
                        self.assertEqual(result.attempted_configuration_ids, ("primary", "fallback"))
                        self.assertEqual(
                            [host for host, _ in requests],
                            ["primary.example.test", "primary.example.test", "fallback.example.test"],
                        )
                        self.assertEqual(len(failures), 1)
                        self.assertEqual(requests[-1][1]["messages"], requests[0][1]["messages"])

    async def test_all_unavailable_does_not_become_input_rejection(self):
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            with self.subTest(lane=lane.offering.implementation_ref):
                async with self.route(lane, "all_unavailable") as (run, _expected, requests, failures):
                    with self.assertRaises(InterpreterUnavailable) as raised:
                        await run()
                    self.assertIs(raised.exception.reason, InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED)
                    self.assertEqual(raised.exception.attempted_configuration_ids, ("primary", "fallback"))
                    self.assertEqual(
                        [host for host, _ in requests],
                        ["primary.example.test"] * 2 + ["fallback.example.test"] * 2,
                    )
                    self.assertEqual(len(failures), 2)


if __name__ == "__main__":
    unittest.main()
