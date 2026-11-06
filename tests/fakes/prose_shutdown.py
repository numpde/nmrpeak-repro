"""Run NMRPeak's service against an unanswered prose-interpretation request."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import read_fail_body
from provider_core.chat import ChatEndpoint
from provider_core.delivery_client import DeliveryEndpoint

from nmrpeak_provider.analysis_session import NmrpeakAnalysisSession
from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.interpreter_policy import InterpreterPolicy
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec
from nmrpeak_provider.provider_config import ObservationPolicy, ProviderProcessPolicy
from nmrpeak_provider.provider_process import ProviderAnnouncement, ProviderProcess
from tests.fakes.model_http import LoopbackHTTP, Reply
from tests.fakes.shared_api import SharedApi
from tests.unit.test_generation_runtime import generation_runtime


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

    def __init__(self, endpoint, reports):
        self.endpoint, self.reports = endpoint, reports

    async def submit(self, item):
        self.reports.append(item)
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

    async def start_provider(self):
        runtime = generation_runtime()
        hf_kind = runtime.hf.generation.analysis_kind_ref
        self.jobs_api = SharedApi({
            self.original_job: (hf_kind, b"Interpret these proton peaks."),
            self.next_job: (hf_kind, b"Interpret the next set of peaks."),
        })
        self.release_test_reply = asyncio.Event()
        self.prose_server = await StalledProseServer([Reply(wait_for=self.release_test_reply)]).start()
        self.addAsyncCleanup(self.prose_server.close)
        interpreter = InputInterpreter((OpenAIChatEndpointSpec(
            configuration_id="hf-prose-test", model="local-prose-test-model",
            url=self.prose_server.base_url + "/chat/completions", api_key="fixture-key",
            trust=self.prose_server.trust,
        ),), InterpreterPolicy(interpretation_timeout_seconds=self.interpretation_seconds))
        self.science = []
        for lane in (runtime.hf, runtime.chf):
            runner = Mock(result_facts=lane.result_facts, quiescent=True, retired=False)
            runner.validate.side_effect = AssertionError("No prose answer authorized validation")
            runner.generate.side_effect = AssertionError("No validation authorized generation")
            self.science.append(runner)
        self.delivery_reports = []
        directory = self.enterContext(tempfile.TemporaryDirectory())
        endpoint = DeliveryEndpoint(self.jobs_api.origin, self.jobs_api.provider_ref, Path(directory) / "unused.sock")
        self.provider = ProviderProcess(
            runtime=runtime, hf_session=self.science[0], chf_session=self.science[1],
            interpreter=interpreter, new_api=self.jobs_api.new_api,
            new_delivery=lambda: LocalDeliveryRecorder(endpoint, self.delivery_reports),
            policy=ProviderProcessPolicy(
                feed_interval_seconds=0.01, hello_interval_seconds=60,
                shutdown_drain_seconds=2, forced_join_seconds=0.1, inventory_maximum_pages=20,
                observation=ObservationPolicy(
                    poll_interval_seconds=0.01, retry_max_seconds=0.02, gap_seconds=10,
                ),
            ),
            announcement=ProviderAnnouncement("NMRPeak", "Prose shutdown test", ()),
        )
        # Observe the real calls: the installed SDK runs NMRPeak's prepare,
        # and NMRPeak's interpreter uses the SDK's real HTTPS endpoint.
        self.prepared_attempts, self.request_deadlines = [], []
        prepare, complete = NmrpeakAnalysisSession.prepare, ChatEndpoint.complete

        async def observe_prepare(session, acknowledged, *args, **kwargs):
            self.prepared_attempts.append(acknowledged.admission.execution_attempt_ref)
            return await prepare(session, acknowledged, *args, **kwargs)

        async def observe_request(endpoint, messages, tools, *, not_after):
            self.request_deadlines.append(not_after)
            return await complete(endpoint, messages, tools, not_after=not_after)

        self.enterContext(patch.object(NmrpeakAnalysisSession, "prepare", observe_prepare))
        self.enterContext(patch.object(ChatEndpoint, "complete", observe_request))
        ready = asyncio.Event()
        self.provider_run = None
        self.addAsyncCleanup(self._finish_provider)
        self.provider_run = asyncio.create_task(self.provider.run(on_ready=ready.set))
        await asyncio.wait_for(ready.wait(), 3)

    async def _finish_provider(self):
        self.release_test_reply.set()
        self.provider.request_stop()
        if self.provider_run is not None:
            await asyncio.wait_for(asyncio.gather(self.provider_run, return_exceptions=True), 8)

    async def assert_original_job_drained(self):
        await asyncio.wait_for(asyncio.shield(self.provider_run), 8)
        self.assertTrue(self.provider.shutdown_ready)
        self.assertEqual([self.original_job], list(self.jobs_api.started))
        original_attempt = self.jobs_api.started[self.original_job]
        starts = [body["job_ref"] for _, path, body in self.jobs_api.calls if path.endswith("/execution-attempts/start")]
        self.assertEqual([self.original_job], starts)
        self.assertEqual([original_attempt], self.prepared_attempts)
        self.assertEqual(1, len(self.request_deadlines))
        self.assertEqual(1, len(self.prose_server.requests))
        self.assertEqual("local-prose-test-model", json.loads(self.prose_server.requests[0].content)["model"])
        for runner in self.science:
            runner.validate.assert_not_called()
            runner.generate.assert_not_called()
        self.assertEqual(1, len(self.delivery_reports))
        report, = self.delivery_reports
        self.assertIs(TerminalOperation.FAIL, report.operation)
        self.assertEqual(self.original_job, report.job_ref)
        self.assertEqual(original_attempt, report.execution_attempt_ref)
        self.assertEqual(
            ("interpretation_failed", "Interpretation could not be completed. Generation did not start."),
            read_fail_body(report.body, execution_attempt_ref=original_attempt),
        )
        self.assertEqual((), self.provider._claims.live_owners())
        self.assertFalse(self.release_test_reply.is_set(), msg="Only test teardown may release the withheld reply")
