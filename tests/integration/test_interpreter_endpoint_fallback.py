"""Exercise production interpretation, HTTP adaptation, repair, and fallback offline.

Adapted from Magnet's endpoint-fallback integration test. Only the HTTP peer is
simulated; NMRPeak's product constructor remains the final candidate authority.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from nmrpeak_provider.input_interpreter import _InterpretationCapability, _value_schema_for
from nmrpeak_provider.interpreter import interpret
from nmrpeak_provider.interpreter_policy import OpenAIChatCallPolicy
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.openai_chat_interpreter import (
    bind_openai_chat_endpoints,
    load_openai_chat_endpoint_specs,
)
from nmrpeak_provider.text_provenance import UserProvidedText


_VALUE = {
    "schema_id": "nmrpeak.structure_generation.request.v1",
    "model_input": {
        "formula": "C2H6O",
        "spectra": {
            "1H": {"peaks": [{
                "shift_lo": "1.25", "shift_hi": "1.25", "integral": "3",
                "multiplicity": "t", "j_hz": ["7.1"],
            }]},
            "13C": {"peaks": [{"shift": "58.1"}]},
        },
    },
}


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
        value = json.loads(json.dumps(_VALUE))
        if lane is HF_LIFECYCLE_LANE:
            del value["model_input"]["spectra"]["13C"]
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
            if primary and behavior == "unavailable":
                raise httpx.ConnectError("private connection detail", request=request)
            if primary and behavior == "reported":
                response = httpx.Response(200, json=_completion(value, tool="report_input_problem"))
            elif primary and behavior == "constructor" and len(requests) == 1:
                invalid = json.loads(json.dumps(value))
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
                            request_timeout_seconds=1, turn_timeout_seconds=3,
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
                            source_text=UserProvidedText("Formula C2H6O; measured NMR peaks."),
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


if __name__ == "__main__":
    unittest.main()
