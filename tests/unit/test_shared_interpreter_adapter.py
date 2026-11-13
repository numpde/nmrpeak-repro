"""Exercise Peak's scientific boundary through the installed shared interpreter."""

import asyncio
import json
from itertools import count
import os
from pathlib import Path
import tempfile
import time
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from provider_core.conversation import ConversationRetirementFailed
from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.interpreter import InterpreterUnavailable
from nmrpeak_provider.interpreter_policy import InterpreterPolicy
from nmrpeak_provider.lifecycle_lane import HF_LIFECYCLE_LANE
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec, load_openai_chat_endpoint_specs
from nmrpeak_provider.product_input import SUPPORTED_MULTIPLICITIES
from nmrpeak_provider.runner_session import ValidatedRunnerRequest
from tests.fakes.interpreter_values import HF_VALUE
from tests.fakes.model_http import LoopbackHTTP, Reply


_call_ids = count(1)


def reply(name, arguments):
    return {"role": "assistant", "tool_calls": [{"id": f"call-{next(_call_ids)}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class Factory:
    context_bytes = 1000000

    def __init__(self, name, answer):
        self.requests = []
        self.identity = json.dumps({"configuration_id": name, "model": "test",
                                    "configuration_sha256": "a" * 64}).encode()
        self.answer = answer
        self.retirement_confirmed = True

    def __call__(self, **_grant):
        return self

    async def complete(self, messages, tools, **_kwargs):
        self.requests.append(deepcopy(messages))
        return self.answer

    async def retire(self):
        pass


class SharedInterpreterAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_failure_does_not_mask_scientific_failure(self):
        first = Factory("first", reply("report_interpretation_problem", {"message": "Private claim."}))
        second = Factory("second", reply("submit_interpretation", {"value": HF_VALUE}))
        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=(first, second)):
            configured = InputInterpreter((SimpleNamespace(configuration_id="first", api_key="key-one"),
                                           SimpleNamespace(configuration_id="second", api_key="key-two")),
                                          InterpreterPolicy(interpretation_timeout_seconds=30))
        session = configured.new_session()
        scientific = RuntimeError("scientific failure")
        diagnostic = RuntimeError("diagnostic failure")
        def fail_diagnostic(_event):
            raise diagnostic
        with self.assertRaises(RuntimeError) as caught:
            await session.validate_freeform_input_async(
                source=b"A source", lane=HF_LIFECYCLE_LANE, validate_input=AsyncMock(side_effect=scientific),
                execution_attempt_ref="attempt", record_diagnostic=fail_diagnostic)
        self.assertIs(caught.exception, scientific)
        self.assertIs(session.diagnostic_failure, diagnostic)
        await session.retire()
        await session.join()

    async def test_cancelled_original_retains_independent_failure_across_reobservation(self):
        session = InputInterpreter((), InterpreterPolicy(interpretation_timeout_seconds=30)).new_session()
        failure = RuntimeError("independent transport failure")

        async def operation():
            raise asyncio.CancelledError() from failure

        session._run_task = asyncio.create_task(operation())
        session._closed = True
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError) as caught:
            await session.join()
        self.assertIs(caught.exception, failure)
        await session.join()
        self.assertIs(session.interruption_failure, failure)

    async def test_retiring_active_http_settles_original_task_and_can_repeat(self):
        release = asyncio.Event()
        server = await LoopbackHTTP([Reply(wait_for=release)]).start()
        try:
            spec = OpenAIChatEndpointSpec(configuration_id="first", url=server.base_url + "/chat/completions",
                                          model="fixture", api_key="fixture-key", trust=server.trust)
            session = InputInterpreter((spec,), InterpreterPolicy(interpretation_timeout_seconds=30)).new_session()
            validate = AsyncMock()
            task = asyncio.create_task(session.validate_freeform_input_async(
                source=b"A source", lane=HF_LIFECYCLE_LANE, validate_input=validate,
                execution_attempt_ref="attempt"))
            try:
                await asyncio.wait_for(server.request_seen.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(session.retire(), 2)
                await asyncio.wait_for(session.join(), 2)
                await asyncio.wait_for(session.retire(), 2)
                await asyncio.wait_for(session.join(), 2)
                self.assertTrue(session._shared.retirement_confirmed)
                validate.assert_not_awaited()
                self.assertEqual(len(server.requests), 1)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            await server.close()

    async def test_diagnostic_failure_does_not_veto_accepted_result(self):
        first = Factory("first", reply("report_interpretation_problem", {"message": "Private claim."}))
        second = Factory("second", reply("submit_interpretation", {"value": HF_VALUE}))
        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=(first, second)):
            configured = InputInterpreter((SimpleNamespace(configuration_id="first", api_key="key-one"),
                                           SimpleNamespace(configuration_id="second", api_key="key-two")),
                                          InterpreterPolicy(interpretation_timeout_seconds=30))
        session = configured.new_session()
        accepted = ValidatedRunnerRequest(object(), object())
        validate = AsyncMock(return_value=accepted)
        failure = RuntimeError("diagnostic recipient failed")
        def fail_diagnostic(_event):
            raise failure
        result = await session.validate_freeform_input_async(
            source=b"A source", lane=HF_LIFECYCLE_LANE, validate_input=validate,
            execution_attempt_ref="attempt", record_diagnostic=fail_diagnostic)
        self.assertIs(result, accepted)
        self.assertIs(session.diagnostic_failure, failure)
        await session.retire()
        await session.join()
        self.assertIs(session._shared.accepted_result.value, accepted)
        validate.assert_awaited_once()

    async def test_accepted_runner_request_stays_held_after_model_cleanup_failure(self):
        class HeldFactory(Factory):
            def __init__(self):
                super().__init__("first", reply("submit_interpretation", {"value": HF_VALUE}))
                self.retirement_confirmed = False
                self.fail_retirement = True

            async def retire(self):
                if self.fail_retirement:
                    raise RuntimeError("fixture model cleanup failed")
                self.retirement_confirmed = True

        model = HeldFactory()
        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=(model,)):
            configured = InputInterpreter((SimpleNamespace(configuration_id="first", api_key="key-one"),),
                                          InterpreterPolicy(interpretation_timeout_seconds=30))
        session = configured.new_session()
        accepted = ValidatedRunnerRequest(object(), object())
        validate = AsyncMock(return_value=accepted)
        with self.assertRaises(ConversationRetirementFailed):
            await session.validate_freeform_input_async(
                source=b"A source", lane=HF_LIFECYCLE_LANE, validate_input=validate,
                execution_attempt_ref="attempt")
        self.assertIs(session._shared.accepted_result.value, accepted)
        validate.assert_awaited_once()
        model.fail_retirement = False
        await session.retire()
        await session.join()
        self.assertIs(session._shared.accepted_result.value, accepted)
        validate.assert_awaited_once()

    async def test_multiplicity_constraint_comes_from_product_vocabulary(self):
        path = "value.model_input.spectra.1H.peaks.items.multiplicity"
        model = Factory("first", None)
        answers = iter((reply("describe_input_constraints", {"tool": "submit_interpretation", "field": path}),
                        reply("submit_interpretation", {"value": HF_VALUE})))
        complete = model.complete

        async def scripted(messages, tools, **kwargs):
            model.answer = next(answers)
            return await complete(messages, tools, **kwargs)

        model.complete = scripted
        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=(model,)):
            configured = InputInterpreter((SimpleNamespace(configuration_id="first", api_key="key-one"),),
                                          InterpreterPolicy(interpretation_timeout_seconds=30))
        session = configured.new_session()
        accepted = ValidatedRunnerRequest(object(), object())
        try:
            result = await session.validate_freeform_input_async(
                source=b"A source", lane=HF_LIFECYCLE_LANE, validate_input=AsyncMock(return_value=accepted),
                execution_attempt_ref="attempt")
            self.assertIs(result, accepted)
            feedback = json.loads(next(message["content"] for message in model.requests[1]
                                       if message["role"] == "tool"))
            self.assertEqual(set(feedback["value"]["allowed_values"]), set(SUPPORTED_MULTIPLICITIES))
            self.assertIn("do not substitute", feedback["value"]["note"])
        finally:
            await session.retire()
            await session.join()

    async def test_endpoint_loading_rejects_ambient_tls_keylog(self):
        from provider_core.api_config import ApiConfigurationRejected

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first.toml").write_text(
                'url = "https://example.test/v1/chat/completions"\n'
                'model = "fixture"\nkey_file = "first.key"\n'
            )
            (root / "first.key").write_text("fixture-key\n")
            with patch.dict(os.environ, {"SSLKEYLOGFILE": str(root / "keylog")}, clear=False):
                with self.assertRaises(ApiConfigurationRejected):
                    load_openai_chat_endpoint_specs(root)
            self.assertFalse((root / "keylog").exists())

    async def test_private_report_is_not_forwarded_to_next_model(self):
        marker = "private report marker"
        first = Factory("first", reply("report_interpretation_problem", {"message": marker}))
        second = Factory("second", reply("submit_interpretation", {"value": {}}))
        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=(first, second)):
            configured = InputInterpreter((SimpleNamespace(configuration_id="first", api_key="key-one"),
                                           SimpleNamespace(configuration_id="second", api_key="key-two")),
                                          InterpreterPolicy(interpretation_timeout_seconds=30))
        session = configured.new_session()
        accepted = ValidatedRunnerRequest(object(), object())
        validate = AsyncMock(return_value=accepted)
        with patch("nmrpeak_provider.interpretation_capability._InterpretationCapability.construct_interpretation") as construct, \
             patch.object(type(HF_LIFECYCLE_LANE), "bind_runner_input", return_value=object()):
            construct.return_value = object()
            result = await session.validate_freeform_input_async(
                source=b"A source", lane=HF_LIFECYCLE_LANE, validate_input=validate,
                execution_attempt_ref="attempt")
        self.assertIs(result, accepted)
        self.assertNotIn(marker, json.dumps(second.requests))
        validate.assert_awaited_once()
        await session.retire()
        await session.join()

    async def test_expired_window_does_not_start_validation(self):
        model = Factory("first", reply("submit_interpretation", {"value": {}}))
        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=(model,)):
            configured = InputInterpreter((SimpleNamespace(configuration_id="first", api_key="key-one"),),
                                          InterpreterPolicy(interpretation_timeout_seconds=0.05))
        session = configured.new_session()
        validate = AsyncMock()
        def slow_bind(_lane, _candidate):
            time.sleep(0.1)
            return object()
        with patch("nmrpeak_provider.interpretation_capability._InterpretationCapability.construct_interpretation", return_value=object()), \
             patch.object(type(HF_LIFECYCLE_LANE), "bind_runner_input", slow_bind):
            with self.assertRaises(InterpreterUnavailable):
                await session.validate_freeform_input_async(
                    source=b"A source", lane=HF_LIFECYCLE_LANE, validate_input=validate,
                    execution_attempt_ref="attempt")
        validate.assert_not_awaited()
        await session.retire()
        await session.join()
