"""Run NMRPeak's service against an unanswered prose-interpretation request."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import read_complete_body
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.controller import ControllerFailed
from provider_core.diagnostics import failure_evidence

from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.generation_runtime import GenerationRuntime
from nmrpeak_provider.interpreter import InterpreterUnavailable, InterpreterUnavailableReason
from nmrpeak_provider.product_result import RESULT_SCHEMA_ID, canonical_result_bytes
from nmrpeak_provider.runner_protocol import GenerateFrame, ValidateFrame
from nmrpeak_provider.interpreter_policy import InterpreterPolicy
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec
from nmrpeak_provider.provider_config import ObservationPolicy, ProviderProcessPolicy
from nmrpeak_provider.provider_process import ProviderAnnouncement, ProviderProcess
from tests.fakes.model_http import LoopbackHTTP, Reply, reply
from tests.fakes.interpreter_values import HF_VALUE
from tests.unit.test_analysis_session import scientific_lane
from tests.fakes.shared_api import SharedApi


class StalledProseServer(LoopbackHTTP):
    """Keep the request open until the SDK closes it or test cleanup releases it."""

    async def _accept(self, reader, writer):
        self.client_reader = reader
        await super()._accept(reader, writer)

    async def wait_for_client_eof(self):
        # The request handler has consumed the complete body and is awaiting
        # permission to reply, so no other task is reading this connection.
        await asyncio.wait_for(self.request_seen.wait(), 3)
        return await asyncio.wait_for(self.client_reader.read(1), 5)


class LocalDeliveryRecorder:
    """Acknowledge handoff to NMRPeak's local queue; no remote publication."""

    retirement_confirmed = True
    last_failure = None

    def __init__(self, endpoint, reports, report_received, acknowledge_report):
        self.endpoint, self.reports = endpoint, reports
        self.report_received, self.acknowledge_report = report_received, acknowledge_report

    async def submit(self, item):
        self.reports.append(item)
        self.report_received.set()
        # The SDK controller must await the delivery service's actual local
        # acceptance before calling this Job settled.
        await self.acknowledge_report.wait()
        return item.acceptance()

    async def retire(self):
        pass

    def can_retry(self, failure):
        return False


class ProseShutdownCase(unittest.IsolatedAsyncioTestCase):
    """One admitted HF Job and another available HF Job on that same lane."""

    original_job = "job:admitted-prose"
    next_job = "job:next-prose"
    interpretation_seconds = 1.5

    async def start_provider(self, *, hold_delivery_ack=False, answer=False):
        hf, hf_runner, hf_channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, chf_channel, _ = await self.enterAsyncContext(scientific_lane("chf"))
        runtime = GenerationRuntime("sha256:" + "1" * 64, hf, chf)
        self.science = (hf_runner, chf_runner)
        self.channels = (hf_channel, chf_channel)
        self.confirmed_provider_failure = None
        self.original_interpretation_failures = []

        def observe_failure(failure, **policy):
            self.original_interpretation_failures.append(failure)
            return failure_evidence(failure, **policy)

        self.enterContext(patch("nmrpeak_provider.input_interpreter.failure_evidence", observe_failure))
        hf_kind = runtime.hf.generation.analysis_kind_ref
        self.jobs_api = SharedApi({
            self.original_job: (hf_kind, b"Interpret these proton peaks."),
            self.next_job: (hf_kind, b"Interpret the next set of peaks."),
        })
        self.release_test_reply = asyncio.Event()
        response = Reply(wait_for=self.release_test_reply)
        if answer:
            completion = {"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "interpret-original-job", "type": "function", "function": {
                        "name": "submit_interpretation", "arguments": json.dumps({"value": HF_VALUE}),
                    },
                }],
            }}]}
            response = Reply(body=reply(completion).body, wait_for=self.release_test_reply)
        self.prose_server = await StalledProseServer([response]).start()
        self.addAsyncCleanup(self.prose_server.close)
        interpreter = InputInterpreter((OpenAIChatEndpointSpec(
            configuration_id="hf-prose-test", model="local-prose-test-model",
            url=self.prose_server.base_url + "/chat/completions", api_key="fixture-key",
            trust=self.prose_server.trust,
        ),), InterpreterPolicy(interpretation_timeout_seconds=self.interpretation_seconds))
        self.delivery_reports = []
        self.delivery_received = asyncio.Event()
        self.allow_delivery_ack = asyncio.Event()
        if not hold_delivery_ack:
            self.allow_delivery_ack.set()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        endpoint = DeliveryEndpoint(self.jobs_api.origin, self.jobs_api.provider_ref, Path(directory) / "unused.sock")
        self.provider = ProviderProcess(
            runtime=runtime, hf_session=self.science[0], chf_session=self.science[1],
            interpreter=interpreter, new_api=self.jobs_api.new_api,
            new_delivery=lambda: LocalDeliveryRecorder(
                endpoint, self.delivery_reports, self.delivery_received, self.allow_delivery_ack,
            ),
            policy=ProviderProcessPolicy(
                feed_interval_seconds=0.01, hello_interval_seconds=60,
                shutdown_drain_seconds=2, forced_join_seconds=0.1, inventory_maximum_pages=20,
                observation=ObservationPolicy(
                    poll_interval_seconds=0.01, retry_max_seconds=0.02, gap_seconds=10,
                ),
            ),
            announcement=ProviderAnnouncement("NMRPeak", "Prose shutdown test", ()),
        )
        ready = asyncio.Event()
        self.provider_run = None
        self.addAsyncCleanup(self._finish_provider)
        self.provider_run = asyncio.create_task(self.provider.run(on_ready=ready.set))
        await asyncio.wait_for(ready.wait(), 3)

    async def _finish_provider(self):
        self.allow_delivery_ack.set()
        self.release_test_reply.set()
        self.provider.request_stop()
        if self.provider_run is not None:
            if self.confirmed_provider_failure is not None:
                self.assertTrue(self.provider_run.done())
                self.assertIs(self.confirmed_provider_failure, self.provider_run.exception())
            else:
                await asyncio.wait_for(asyncio.shield(self.provider_run), 8)

    async def assert_original_job_failed_privately(self):
        with self.assertRaises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(asyncio.shield(self.provider_run), 8)
        controller = self.provider.lanes[0].controller
        retained = controller.last_failure
        self.assertIs(ControllerFailed, type(retained))
        self.assertIsNotNone(caught.exception.subgroup(lambda error: error is retained))
        original = retained.cause.cause
        self.assertIs(InterpreterUnavailable, type(original))
        self.assertIs(InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED, original.reason)
        self.assertTrue(any(failure is original.__cause__ for failure in self.original_interpretation_failures))
        self.assertIn(controller, self.provider._claims.live_owners())
        self.assertTrue(controller.shutdown_ready)
        self.assertTrue(self.provider.shutdown_ready)
        self.assertEqual([self.original_job], list(self.jobs_api.started))
        self.assertEqual(1, len(self.prose_server.requests))
        self.assertEqual([], self.delivery_reports)
        self.assertFalse(self.delivery_received.is_set())
        for runner, channel in zip(self.science, self.channels, strict=True):
            self.assertEqual([], channel.received_frames)
            self.assertTrue(runner.quiescent)
            self.assertFalse(runner.retired)
        self.assertFalse(any(path.endswith(("/fail", "/complete")) for _, path, _ in self.jobs_api.calls))
        self.assertFalse(self.release_test_reply.is_set(), msg="Only test teardown may release the withheld reply")
        self.confirmed_provider_failure = caught.exception

    async def assert_original_job_drained(self):
        await asyncio.wait_for(asyncio.shield(self.provider_run), 8)
        self.assertTrue(self.provider.shutdown_ready)
        self.assertEqual([self.original_job], list(self.jobs_api.started))
        original_attempt = self.jobs_api.started[self.original_job]
        starts = [body["job_ref"] for _, path, body in self.jobs_api.calls if path.endswith("/execution-attempts/start")]
        self.assertEqual([self.original_job], starts)
        self.assertEqual(1, len(self.prose_server.requests))
        self.assertEqual("local-prose-test-model", json.loads(self.prose_server.requests[0].content)["model"])
        self.assertEqual(1, len(self.delivery_reports))
        report, = self.delivery_reports
        self.assertIs(TerminalOperation.COMPLETE, report.operation)
        self.assertEqual(self.original_job, report.job_ref)
        self.assertEqual(original_attempt, report.execution_attempt_ref)
        schema, result = read_complete_body(report.body, execution_attempt_ref=original_attempt)
        self.assertEqual(RESULT_SCHEMA_ID, schema)
        self.assertEqual(canonical_result_bytes(["CCO", "OCC"], self.science[0].result_facts), result)
        self.assertEqual([ValidateFrame, GenerateFrame], [type(frame) for frame in self.channels[0].received_frames])
        self.assertEqual([], self.channels[1].received_frames)
        self.assertTrue(self.release_test_reply.is_set())
        for runner in self.science:
            self.assertTrue(runner.quiescent)
            self.assertFalse(runner.retired)
