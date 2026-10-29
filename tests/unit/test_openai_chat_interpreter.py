"""Exercise NMR's OpenAI adapter over the installed SDK HTTP owner.

The loopback server supplies actual HTTP bytes. It does not substitute for the
SDK transport. Only the unsettled-owner test substitutes an exact SDK
ExchangeFailed because that cleanup state cannot be made deterministic with a
kernel socket.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.fakes.model_http import LoopbackHTTP, Reply, reply
from provider_core.transport import ExchangeFailed

from nmrpeak_provider.interpreter import InterpreterTransportError
from nmrpeak_provider.interpreter_policy import OpenAIChatCallPolicy
from nmrpeak_provider.local_input import LocalInputFailureReason, LocalInputSnapshotError
from nmrpeak_provider.openai_chat_interpreter import (
    OpenAIChatEndpointSpec, bind_openai_chat_endpoints,
    load_openai_chat_endpoint_specs,
)

_VALUE_SCHEMA = {
    "type": "object", "properties": {"answer": {"type": "integer"}},
    "required": ["answer"], "additionalProperties": False,
}






@asynccontextmanager
async def endpoints(script, *, request_seconds=0.2, turn_seconds=1.6, before_send=None,
                    configs=None, report=None):
    server = await LoopbackHTTP(script).start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_rows = configs or [("10-only.toml", "only", "secret", "model", None)]
            for filename, config_id, key, model, effort in config_rows:
                _write_config(root / filename, configuration_id=config_id, base_url=server.base_url,
                              api_key=key, model=model, reasoning_effort=effort)
            specs = load_openai_chat_endpoint_specs(root)
            owner = bind_openai_chat_endpoints(
                specs, OpenAIChatCallPolicy(request_timeout_seconds=request_seconds, turn_timeout_seconds=turn_seconds),
                before_send=before_send,
                submit_interpretation_description="Submit the fixture value.",
                submit_interpretation_value_schema=_VALUE_SCHEMA,
                report_input_problem_description="Report a fixture source problem.",
                report_endpoint_failure=report,
            )
            try:
                yield owner, server
            finally:
                await owner.retire()
    finally:
        await server.close()

class OpenAIChatInterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_loading_is_ordered_resource_free_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_config(root / "20-local.toml", configuration_id="local", api_key="local-secret")
            _write_config(root / "10-remote.toml", configuration_id="remote", api_key="remote-secret")
            with patch("nmrpeak_provider.openai_chat_interpreter._OpenAIChatCall") as call:
                specs = load_openai_chat_endpoint_specs(root)
        self.assertEqual([x.configuration_id for x in specs], ["remote", "local"])
        self.assertTrue(all(type(x) is OpenAIChatEndpointSpec for x in specs))
        self.assertNotIn("remote-secret", repr(specs))
        self.assertNotIn("local-secret", repr(specs))
        call.assert_not_called()
        with self.assertRaises(FrozenInstanceError):
            specs[0].model = "mutated"

    async def test_policy_bounds_and_validation(self):
        policy = OpenAIChatCallPolicy(request_timeout_seconds=4, turn_timeout_seconds=10)
        self.assertEqual((policy.request_timeout_seconds, policy.turn_timeout_seconds), (4.0, 10.0))
        for request, turn in ((True, 10), (0, 10), (float("inf"), 10), (4, False), (4, 0), (4, float("nan")), (5, 10)):
            with self.subTest(request=request, turn=turn), self.assertRaises(ValueError):
                OpenAIChatCallPolicy(request_timeout_seconds=request, turn_timeout_seconds=turn)

    async def test_wire_request_schema_order_reasoning_and_response(self):
        payload = {"value": {"answer": 1}}
        configs = [("20-local.toml", "local", "local-secret", "local-model", None),
                   ("10-remote.toml", "remote", "remote-secret", "remote-model", "none")]
        async with endpoints([reply(_completion("submit_interpretation", payload)) for _ in range(2)], configs=configs) as (owner, server):
            first = await owner.endpoints[0].call([{"role":"system","content":"generic"},{"role":"user","content":"source"}])
            await owner.endpoints[1].call([])
        self.assertEqual([e.configuration_id for e in owner.endpoints], ["remote", "local"])
        request = server.requests[0]
        self.assertEqual((request.method, request.target), ("POST", "/v1/chat/completions"))
        self.assertEqual(request.headers["authorization"], "Bearer remote-secret")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "remote-model")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual([x["function"]["name"] for x in body["tools"]], ["submit_interpretation", "report_input_problem"])
        self.assertEqual(body["tools"][0]["function"]["parameters"]["properties"]["value"], _VALUE_SCHEMA)
        self.assertEqual([x["role"] for x in body["messages"]], ["system", "user"])
        self.assertNotIn("reasoning_effort", json.loads(server.requests[1].content))
        self.assertEqual(first.invocation.arguments, payload)
        self.assertNotIn("remote-secret", repr(owner.endpoints))

    async def test_tool_schema_is_snapshotted(self):
        observed = []
        async def before_send(request):
            observed.append(json.loads(request.content))
        schema = {"type":"object","properties":{"answer":{"type":"integer"}},"required":["answer"]}
        server = await LoopbackHTTP([reply(_completion("submit_interpretation", {"value":{"answer":1}}))]).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "10.toml"
                _write_config(path, base_url=server.base_url)
                owner = bind_openai_chat_endpoints(load_openai_chat_endpoint_specs(d),
                    OpenAIChatCallPolicy(request_timeout_seconds=.2, turn_timeout_seconds=1.5),
                    before_send=before_send, submit_interpretation_description="Submit.",
                    submit_interpretation_value_schema=schema, report_input_problem_description="Report.")
                schema["properties"]["answer"]["type"] = "string"
                await owner.endpoints[0].call([])
                await owner.retire()
            self.assertEqual(observed[0]["tools"][0]["function"]["parameters"]["properties"]["value"]["properties"]["answer"]["type"], "integer")
        finally:
            await server.close()

    async def test_reasoning_turn_is_preserved_for_protocol_repair(self):
        message = _completion_message("submit_interpretation", "not JSON", call_id="repair")
        message["reasoning_content"] = "retained reasoning"
        script = [reply({"choices":[{"message":message}]}), reply(_completion("submit_interpretation", {"value":{"answer":1}}))]
        async with endpoints(script) as (owner, server):
            first = await owner.endpoints[0].call([{"role":"user","content":"fixture"}])
            self.assertIsNone(first.invocation)
            await owner.endpoints[0].call([first.assistant_message, {"role":"tool","tool_call_id":"repair","content":"Rejected"}])
        sent = json.loads(server.requests[1].content)
        self.assertEqual(sent["messages"][0]["reasoning_content"], "retained reasoning")
        self.assertEqual(first.assistant_message, message)

    async def test_reasoning_values_and_plain_turn_contract(self):
        for value in (False, 1, [], {}):
            doc = _completion("submit_interpretation", {"value":{"answer":1}})
            doc["choices"][0]["message"]["reasoning_content"] = value
            async with endpoints([reply(doc), reply(doc)]) as (owner, _server):
                with self.assertRaises(InterpreterTransportError) as caught:
                    await owner.endpoints[0].call([])
            self.assertEqual(caught.exception.reason, "invalid_response_envelope")
        async with endpoints([reply({"choices":[{"message":{"role":"assistant","content":"repair"}}]})]) as (owner, _server):
            turn = await owner.endpoints[0].call([])
        self.assertIsNone(turn.invocation)
        self.assertEqual(turn.tool_call_ids, ())

    async def test_status_and_connection_retries_are_bounded(self):
        ambiguous = {"error":{"type":"invalid_request_error","code":None,"param":None,"message":"private"}}
        cases = [([reply(ambiguous, status=401), reply(_completion("submit_interpretation", {"value":7}))], None, 2),
                 ([Reply(disconnect=True), reply(_completion("submit_interpretation", {"value":7}))], None, 2),
                 ([reply(ambiguous, status=401)]*2, "http_401", 2),
                 ([reply({"error":{"message":"private"}}, status=503)]*2, "http_503", 2),
                 ([reply({"error":{"message":"private"}}, status=400)], "http_400", 1)]
        for script, expected, count in cases:
            async with endpoints(script) as (owner, server):
                if expected is None:
                    result = await owner.endpoints[0].call([])
                    self.assertEqual(result.invocation.arguments, {"value":7})
                else:
                    with self.assertRaises(InterpreterTransportError) as caught:
                        await owner.endpoints[0].call([])
                    self.assertEqual(caught.exception.reason, expected)
            self.assertEqual(len(server.requests), count)

    async def test_gzip_success_and_error_limits_apply_after_decoding(self):
        oversized = json.dumps(_completion("submit_interpretation", {"value":"x"*(256*1024)})).encode()
        async with endpoints([reply(gzip.compress(oversized), headers=(("content-encoding","gzip"),))]) as (owner, _):
            with self.assertRaises(InterpreterTransportError) as caught:
                await owner.endpoints[0].call([])
        self.assertEqual(caught.exception.reason, "response_too_large")
        error = json.dumps({"error":{"message":"x"*(16*1024)}}).encode()
        async with endpoints([reply(gzip.compress(error), status=400, headers=(("content-encoding","gzip"),))]) as (owner, _):
            with self.assertRaises(InterpreterTransportError) as caught:
                await owner.endpoints[0].call([])
        self.assertEqual((caught.exception.reason, caught.exception.http_status), ("http_400",400))

    async def test_endpoint_diagnostics_keep_status_safe_fields_and_unknown_values(self):
        docs = [reply({"error":{"type":"authentication_error","code":"invalid_api_key","message":"private Bearer do-not-display","param":"hidden"}}, status=400, headers=(("x-request-id","req_safe123"),)),
                reply({"error":{"type":"private_scalar","code":"private_scalar","message":"private scalar"}}, status=400, headers=(("x-request-id","private_scalar"),))]
        for expected in (("authentication_error","invalid_api_key","req_safe123"), ("private_scalar","private_scalar","private_scalar")):
            async with endpoints([docs.pop(0)]) as (owner, _):
                with self.assertRaises(InterpreterTransportError) as caught:
                    await owner.endpoints[0].call([{"role":"user","content":"private"}])
            error = caught.exception
            self.assertEqual((error.error_type,error.error_code,error.request_id), expected)
            self.assertEqual(error.http_status,400)
            self.assertNotIn("do-not-display",str(error))

    async def test_invalid_gzip_uses_the_existing_bounded_endpoint_retry(self):
        async with endpoints([reply(b"not gzip", headers=(("content-encoding","gzip"),)), reply(b"not gzip", headers=(("content-encoding","gzip"),))]) as (owner, server):
            with self.assertRaises(InterpreterTransportError) as caught:
                await owner.endpoints[0].call([])
        self.assertEqual((caught.exception.reason,len(server.requests)),("endpoint_unavailable",2))

    async def test_request_and_turn_deadlines_are_original_absolute_budgets(self):
        async def slow_then_success(request, index):
            if index == 0:
                await asyncio.sleep(.06)
            return reply(_completion("submit_interpretation", {"value":7}))
        server = await LoopbackHTTP([slow_then_success, slow_then_success]).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                _write_config(Path(d)/"10.toml",base_url=server.base_url)
                owner=bind_openai_chat_endpoints(load_openai_chat_endpoint_specs(d),OpenAIChatCallPolicy(request_timeout_seconds=.03,turn_timeout_seconds=1.1),submit_interpretation_description="Submit.",submit_interpretation_value_schema=_VALUE_SCHEMA,report_input_problem_description="Report.")
                turn=await owner.endpoints[0].call([])
                await owner.retire()
            self.assertEqual(turn.invocation.arguments,{"value":7})
            self.assertEqual(len(server.requests),2)
        finally: await server.close()

    async def test_before_send_pacing_consumes_request_deadline_and_retired_owner_dispatches_nothing(self):
        started=[]
        async def pace(_request):
            started.append(asyncio.get_running_loop().time())
            await asyncio.sleep(.04)
        async with endpoints([],request_seconds=.02,turn_seconds=1.05,before_send=pace) as (owner,server):
            with self.assertRaises(InterpreterTransportError):
                await owner.endpoints[0].call([])
            self.assertEqual(server.requests,[])
            await owner.retire()
            with self.assertRaises(RuntimeError): await owner.endpoints[0].call([])
        self.assertEqual(len(started),2)

    async def test_concurrent_retirement_closes_admission_and_cancels_original_exchange(self):
        gate=asyncio.Event()
        server=await LoopbackHTTP([Reply(wait_for=gate)]).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                _write_config(Path(d)/"10.toml",base_url=server.base_url)
                owner=bind_openai_chat_endpoints(load_openai_chat_endpoint_specs(d),OpenAIChatCallPolicy(request_timeout_seconds=2,turn_timeout_seconds=5),submit_interpretation_description="Submit.",submit_interpretation_value_schema=_VALUE_SCHEMA,report_input_problem_description="Report.")
                call=asyncio.create_task(owner.endpoints[0].call([]))
                await asyncio.wait_for(server.request_seen.wait(),1)
                await owner.retire()
                with self.assertRaises(asyncio.CancelledError): await call
                with self.assertRaises(RuntimeError): await owner.endpoints[0].call([])
                self.assertEqual(len(server.requests),1)
        finally:
            gate.set()
            await server.close()

    async def test_unconfirmed_sdk_cleanup_escapes_fallback_and_retry_without_replacement(self):
        failure=ExchangeFailed("retirement unconfirmed",cleanup_failures=(OSError("socket still owned"),))
        made=[]
        class Unconfirmed:
            exchange_seconds=1.0
            retirement_seconds=1.0
            retirement_confirmed=False
            pending_owner=None
            async def exchange_decoded(self,*_args,owner=None,**_kwargs):
                self.owner=owner
                raise failure
            async def retire(self,*,owner=None):
                if owner is not self.owner: raise AssertionError("owner identity changed")
                raise OSError("cleanup still unconfirmed")
        server = await LoopbackHTTP([]).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                _write_config(Path(d)/"10.toml",base_url=server.base_url)
                owner=bind_openai_chat_endpoints(load_openai_chat_endpoint_specs(d),OpenAIChatCallPolicy(request_timeout_seconds=.2,turn_timeout_seconds=1.5),submit_interpretation_description="Submit.",submit_interpretation_value_schema=_VALUE_SCHEMA,report_input_problem_description="Report.")
                with patch("nmrpeak_provider.openai_chat_interpreter.HttpTransport",side_effect=lambda **_kw: made.append(Unconfirmed()) or made[-1]):
                    with self.assertRaises(BaseExceptionGroup):
                        await owner.endpoints[0].call([])
                    with self.assertRaises(OSError):
                        await owner.retire()
                    with self.assertRaises(RuntimeError):
                        await owner.endpoints[0].call([])
            self.assertEqual(len(made),1)
            self.assertEqual(server.requests,[])
        finally:
            await server.close()

    async def test_request_construction_consumes_the_original_request_allowance(self):
        import time
        import httpx
        construct = httpx.Request

        def delayed_request(*args, **kwargs):
            time.sleep(.03)
            return construct(*args, **kwargs)

        async with endpoints([reply(_completion('submit_interpretation', {'value': 7}))],
                             request_seconds=.01, turn_seconds=1.6) as (owner, server):
            with patch('nmrpeak_provider.openai_chat_interpreter.httpx.Request', side_effect=delayed_request):
                with self.assertRaises(InterpreterTransportError):
                    await owner.endpoints[0].call([])
            self.assertEqual(server.requests, [])

    async def test_received_nonretryable_status_survives_known_body_io_failure(self):
        async with endpoints([Reply(status=400, body=b'{"error":', declared_bytes=1000)]) as (owner, server):
            with self.assertRaises(InterpreterTransportError) as caught:
                await owner.endpoints[0].call([])
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(caught.exception.reason, 'http_400')
        self.assertEqual(len(server.requests), 1)

    async def test_unknown_local_defect_and_certificate_failure_never_enter_retry(self):
        import ssl
        from provider_core.network import NetworkIOObservation
        for primary in (RuntimeError('local adapter defect'), ssl.SSLCertVerificationError(1, 'certificate rejected')):
            failure = ExchangeFailed('Original exchange failed', primary=primary, network_failure=primary,
                                     network_io=NetworkIOObservation(stage='tls', error=primary))
            made = []

            class SettledFailure:
                retirement_confirmed = True
                async def exchange_decoded(self, *_args, **_kwargs):
                    raise failure
                async def retire(self, **_kwargs):
                    pass

            async with endpoints([]) as (owner, server):
                with patch('nmrpeak_provider.openai_chat_interpreter.HttpTransport',
                           side_effect=lambda **_kw: made.append(SettledFailure()) or made[-1]):
                    with self.assertRaises(ExchangeFailed) as caught:
                        await owner.endpoints[0].call([])
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(made), 1)
                self.assertEqual(server.requests, [])

    async def test_configuration_validation_and_snapshot_limits(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            _write_config(root/"10.toml",configuration_id="same")
            _write_config(root/"20.toml",configuration_id="same")
            with self.assertRaisesRegex(ValueError,"IDs must be unique"):
                load_openai_chat_endpoint_specs(root)
        bad = [("id = \"x\"\nbase_url = \"https://x\"\napi_key = \"\"\nmodel = \"m\"\n", "invalid interpreter API key"),
               ("id = \"x\"\nbase_url = \"https://x\"\napi_key = \"s\"\nmodel = \"m\"\nreasoning_effort = \"turbo\"\n", "invalid interpreter reasoning effort"),
               ("id = \"x\"\nbase_url = \"https://x\"\napi_key = \"s\"\nmodel = \"m\"\n", None)]
        for text, expected in bad[:2]:
            with tempfile.TemporaryDirectory() as d:
                (Path(d)/"10.toml").write_text(text)
                with self.assertRaisesRegex(ValueError,expected): load_openai_chat_endpoint_specs(d)
        invalid_urls=(" https://models.example/v1", "https://models.example/v1\t",
                      "https://models.exa\tmple/v1", "https://models.exa mple/v1",
                      "https://models.example:bad/v1", "https://models.example:0/v1",
                      "https://models.example:65536/v1", "https://models.exa\u00a0mple/v1",
                      "https://models.exa\u200bmple/v1")
        for url in invalid_urls:
            with tempfile.TemporaryDirectory() as d:
                _write_config(Path(d)/"10.toml",base_url=url)
                with self.assertRaisesRegex(ValueError,"invalid interpreter base URL"): load_openai_chat_endpoint_specs(d)
        for config_id, model in (("UPPER", "model"), ("only", " ")):
            with tempfile.TemporaryDirectory() as d:
                _write_config(Path(d)/"10.toml",configuration_id=config_id,model=model)
                with self.assertRaisesRegex(ValueError,"invalid interpreter endpoint identity"):
                    load_openai_chat_endpoint_specs(d)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"10.toml"; target=Path(d)/"target"; _write_config(target); p.symlink_to(target)
            with self.assertRaisesRegex(ValueError,"invalid interpreter endpoint configuration"): load_openai_chat_endpoint_specs(d)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"10.toml"; p.write_bytes(b"#"*(1024*1024))
            with self.assertRaisesRegex(ValueError,"invalid interpreter endpoint configuration"): load_openai_chat_endpoint_specs(d)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"10.toml"; p.write_bytes(b"invalid = [")
            with self.assertRaisesRegex(ValueError,"invalid interpreter endpoint configuration") as caught:
                load_openai_chat_endpoint_specs(d)
            self.assertEqual(type(caught.exception.__cause__).__name__,"TOMLDecodeError")

    async def test_snapshot_limit_errors_keep_honest_configuration_messages(self):
        cases=((LocalInputFailureReason.TOO_MANY_SELECTED_FILES,"at most 4 interpreter endpoints are supported"),
               (LocalInputFailureReason.TOO_MANY_DIRECTORY_ENTRIES,"interpreter configuration directory has too many entries"))
        for reason,message in cases:
            with patch("nmrpeak_provider.openai_chat_interpreter.read_ordered_bounded_regular_files",side_effect=LocalInputSnapshotError(reason)):
                with self.assertRaisesRegex(ValueError,f"^{message}$"):
                    load_openai_chat_endpoint_specs(Path("unused"))


def _write_config(path, *, configuration_id="only", base_url="http://127.0.0.1:8000/v1", api_key="secret", model="model", reasoning_effort=None):
    rows=[f'id = "{configuration_id}"',f'base_url = "{base_url}"',f'api_key = "{api_key}"',f'model = "{model}"']
    if reasoning_effort is not None: rows.append(f'reasoning_effort = "{reasoning_effort}"')
    path.write_text("\n".join(rows)+"\n",encoding="utf-8")

def _completion(name,arguments):
    return {"choices":[{"message":_completion_message(name,json.dumps(arguments))}]}

def _completion_message(name,arguments,*,call_id="call-1"):
    return {"role":"assistant","content":None,"tool_calls":[{"id":call_id,"type":"function","function":{"name":name,"arguments":arguments}}]}

if __name__ == "__main__": unittest.main()
