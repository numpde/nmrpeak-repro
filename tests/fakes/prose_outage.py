"""Run the real NMRPeak loop through an unavailable prose interpretation service.

The HTTPS peer is real. Scientific work uses the maintained runner frame codec
and local delivery acknowledges custody only; this fixture does not simulate a
central Jobs API publication receipt or claim native worker continuity.
"""

import asyncio
from collections import Counter
import json
from math import floor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from provider_core.attempt_record import TerminalOperation
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.diagnostics import failure_evidence
from provider_core.interpreter import ProseService

from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.generation_runtime import GenerationRuntime
from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.interpreter_policy import InterpreterPolicy
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec
from nmrpeak_provider.provider_config import ObservationPolicy, ProviderProcessPolicy
from nmrpeak_provider.provider_process import ProviderAnnouncement, ProviderProcess
from nmrpeak_provider.runner_protocol import GenerateFrame, ValidateFrame
from tests.fakes.interpreter_values import HF_VALUE, VALUE
from tests.fakes.model_http import LoopbackHTTP, reply
from tests.fakes.shared_api import SharedApi
from tests.unit.test_analysis_session import scientific_lane


class ObservedProseService(ProseService):
    """Retain the actual client's refusal and retry classification."""

    def __init__(self, factory):
        super().__init__(factory)
        self.retry_observations = []
        self.retry_observed = asyncio.Event()

    def _postpone(self, failure, delay):
        observed_at = asyncio.get_running_loop().time()
        super()._postpone(failure, delay)
        response = failure.response
        self.retry_observations.append({
            "at": observed_at,
            "status": response.status if response is not None else None,
            "retry_after": [value.decode("ascii", errors="replace") for name, value in response.headers
                            if name.lower() == b"retry-after"] if response is not None else [],
            "delay": delay,
            "failure": failure_evidence(failure, secrets=("fixture-key",)),
        })
        self.retry_observed.set()


class OutageJobsApi(SharedApi):
    """Offer unstarted Jobs within the service's requested page size."""

    def response(self, request):
        document = super().response(request)
        if request.url.path == "/provider/v1/jobs":
            limit = int(parse_qs(request.url.query.decode("ascii"))["limit"][0])
            document["jobs"] = document["jobs"][:limit]
        return document


class ProseOutageServer(LoopbackHTTP):
    """Ask every Job to wait, then restore the same HTTPS endpoint."""

    retry_after_seconds = 2

    def __init__(self, source_jobs):
        super().__init__([self.respond] * (2 * len(source_jobs) + 10))
        self.source_jobs = source_jobs
        self.restored_at = None
        self.received = []

    def restore(self):
        self.restored_at = asyncio.get_running_loop().time()

    def respond(self, request, _number):
        document = json.loads(request.content)
        source = next(message["content"] for message in document["messages"]
                      if message.get("role") == "user"
                      and message.get("content") in self.source_jobs)
        self.received.append({
            "at": asyncio.get_running_loop().time(),
            "job_ref": self.source_jobs[source],
            "model": document["model"],
            "available": self.restored_at is not None,
        })
        if self.restored_at is None:
            return reply({"error": {"message": "Prose interpreter is temporarily unavailable"}},
                         status=503, headers=(("retry-after", str(self.retry_after_seconds)),))
        return reply({"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "interpret-this-job", "type": "function", "function": {
                    "name": "submit_interpretation", "arguments": json.dumps({"value": HF_VALUE}),
                },
            }],
        }}]})


class LocalOutageDelivery:
    """Record each exact report and acknowledge local custody, not publication."""

    retirement_confirmed = True
    last_failure = None

    def __init__(self, endpoint, reports, changed):
        self.endpoint, self.reports, self.changed = endpoint, reports, changed

    async def submit(self, item):
        self.reports.append(item)
        self.changed.set()
        return item.acceptance()

    async def retire(self):
        pass

    def can_retry(self, failure):
        return False


class ProseOutageCase(unittest.IsolatedAsyncioTestCase):
    # Allow real HTTPS child startup while keeping this positive model window
    # shorter than Retry-After, so the first Job cannot wait through the refusal.
    interpretation_seconds = 1.5
    outage_seconds = 2.25
    typed_job = "job:typed-during-prose-outage"

    async def start_provider(self, *, backlog):
        hf, hf_runner, hf_channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, chf_channel, _ = await self.enterAsyncContext(scientific_lane("chf"))
        self.science = ((hf_runner, hf_channel), (chf_runner, chf_channel))
        self.chf_kind = chf.generation.analysis_kind_ref
        source_jobs = {
            f"Job {number}: Formula C2H6O. 1H: 1.25 (t, 3H, J 7.1 Hz).": f"job:prose-{number}"
            for number in range(backlog)
        }
        self.jobs_api = OutageJobsApi({
            job: (hf.generation.analysis_kind_ref, source.encode())
            for source, job in source_jobs.items()
        })
        self.prose_server = await ProseOutageServer(source_jobs).start()
        self.addAsyncCleanup(self.prose_server.close)
        with patch("nmrpeak_provider.input_interpreter.ProseService", ObservedProseService):
            configured = InputInterpreter((OpenAIChatEndpointSpec(
                configuration_id="hf-prose-outage", model="local-prose-test-model",
                url=self.prose_server.base_url + "/chat/completions", api_key="fixture-key",
                trust=self.prose_server.trust,
            ),), InterpreterPolicy(interpretation_timeout_seconds=self.interpretation_seconds))
        self.prose_services = configured.services
        self.interpretation_failures = []

        def observe_failure(failure, **policy):
            evidence = failure_evidence(failure, **policy)
            self.interpretation_failures.append(evidence)
            return evidence

        self.enterContext(patch("nmrpeak_provider.input_interpreter.failure_evidence", observe_failure))
        self.reports = []
        self.report_changed = asyncio.Event()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        endpoint = DeliveryEndpoint(self.jobs_api.origin, self.jobs_api.provider_ref, Path(directory) / "unused.sock")
        self.provider = ProviderProcess(
            runtime=GenerationRuntime("sha256:" + "1" * 64, hf, chf),
            hf_session=hf_runner, chf_session=chf_runner, interpreter=configured,
            new_api=self.jobs_api.new_api,
            new_delivery=lambda: LocalOutageDelivery(endpoint, self.reports, self.report_changed),
            policy=ProviderProcessPolicy(
                feed_interval_seconds=0.01, hello_interval_seconds=60,
                shutdown_drain_seconds=2, forced_join_seconds=0.1, inventory_maximum_pages=20,
                observation=ObservationPolicy(
                    poll_interval_seconds=0.01, retry_max_seconds=0.02, gap_seconds=10,
                ),
            ),
            announcement=ProviderAnnouncement("NMRPeak", "Prose service outage test", ()),
        )
        ready = asyncio.Event()
        self.provider_run = None
        self.addAsyncCleanup(self.finish_provider)
        self.provider_run = asyncio.create_task(self.provider.run(on_ready=ready.set))
        await asyncio.wait_for(ready.wait(), 3)

    async def finish_provider(self):
        self.provider.request_stop()
        if self.provider_run is not None:
            await asyncio.wait_for(asyncio.shield(self.provider_run), 8)

    async def wait_for_report(self, predicate, *, seconds):
        async def observe():
            while True:
                self.report_changed.clear()
                for report in self.reports:
                    if predicate(report):
                        return report
                await self.report_changed.wait()

        return await asyncio.wait_for(observe(), seconds)

    async def wait_for_prose_refusal(self):
        service = self.prose_services[0]
        await asyncio.wait_for(service.retry_observed.wait(), 3)
        refusal = service.retry_observations[0]
        self.assertEqual(503, refusal["status"], msg=self.evidence())
        self.assertEqual([str(self.prose_server.retry_after_seconds)], refusal["retry_after"], msg=self.evidence())
        self.assertGreaterEqual(refusal["delay"], self.prose_server.retry_after_seconds, msg=self.evidence())
        self.assertEqual(["job:prose-0"], [request["job_ref"] for request in self.prose_server.received],
                         msg=self.evidence())

    def evidence(self):
        first = self.prose_server.received[0]["at"] if self.prose_server.received else 0
        return json.dumps({
            "retry_after_seconds": self.prose_server.retry_after_seconds,
            "interpretation_seconds": self.interpretation_seconds,
            "outage_seconds": self.outage_seconds,
            "requests": [{**entry, "at": round(entry["at"] - first, 6)}
                         for entry in self.prose_server.received],
            "retry_observations": [{**entry, "at": round(entry["at"] - first, 6)}
                                   for service in self.prose_services for entry in service.retry_observations],
            "interpretation_failures": self.interpretation_failures,
            "started_jobs": list(self.jobs_api.started),
            "reports": [{"job_ref": item.job_ref, "operation": item.operation.value}
                        for item in self.reports],
        }, sort_keys=True)

    async def check_outage(self, *, backlog):
        await self.start_provider(backlog=backlog)
        await self.wait_for_prose_refusal()
        first_request_at = self.prose_server.received[0]["at"]
        # Offer healthy work after the actual client classified Retry-After.
        # The CHF loop must discover it while HF is recovering.
        self.jobs_api.inputs[self.typed_job] = (self.chf_kind, canonical_json_bytes(VALUE))
        await self.wait_for_report(
            lambda report: report.job_ref == self.typed_job and report.operation is TerminalOperation.COMPLETE,
            seconds=1,
        )
        await asyncio.sleep(max(0, first_request_at + 1 - asyncio.get_running_loop().time()))
        self.assertEqual(1, len(self.prose_server.received), msg=self.evidence())
        self.assertFalse(self.provider_run.done(), msg=self.evidence())
        self.assertEqual([], self.science[0][1].received_frames, msg=self.evidence())

        await asyncio.sleep(max(0, first_request_at + self.outage_seconds - asyncio.get_running_loop().time()))
        unavailable_requests = tuple(self.prose_server.received)
        # One HF lane uses this service. Its two-second Retry-After gives at
        # most one initial request and one retry window during this outage,
        # independently of the number of offered Jobs.
        request_ceiling = 1 + floor(self.outage_seconds / self.prose_server.retry_after_seconds)
        self.assertLessEqual(len(unavailable_requests), request_ceiling, msg=self.evidence())
        for earlier, later in zip(unavailable_requests, unavailable_requests[1:]):
            self.assertGreaterEqual(later["at"] - earlier["at"], self.prose_server.retry_after_seconds,
                                    msg=self.evidence())

        unstarted_jobs = set(self.jobs_api.inputs) - self.jobs_api.started.keys()
        self.assertTrue(unstarted_jobs, msg=self.evidence())
        self.prose_server.restore()
        # Allow the remaining requested wait, the unchanged interpretation
        # budget and two seconds for discovery, runner frames and local handoff.
        recovery_seconds = self.prose_server.retry_after_seconds + self.interpretation_seconds + 2
        recovered = await self.wait_for_report(
            lambda report: report.job_ref in unstarted_jobs and report.operation is TerminalOperation.COMPLETE,
            seconds=recovery_seconds,
        )
        self.assertEqual(self.jobs_api.started[recovered.job_ref], recovered.execution_attempt_ref)
        self.assertLessEqual(asyncio.get_running_loop().time() - self.prose_server.restored_at,
                             recovery_seconds, msg=self.evidence())
        self.assertTrue(any(entry["job_ref"] == recovered.job_ref and entry["available"]
                            for entry in self.prose_server.received), msg=self.evidence())
        await self.finish_provider()
        self.assertTrue(self.provider.shutdown_ready, msg=self.evidence())
        self.assertTrue(all(entry["model"] == "local-prose-test-model"
                            for entry in self.prose_server.received), msg=self.evidence())
        self.assertNotIn(self.typed_job, [entry["job_ref"] for entry in self.prose_server.received])
        self.assert_reports_match_original_science()

    def assert_reports_match_original_science(self):
        """Every admitted Job settles once through the same scientific sessions."""
        starts = [body["job_ref"] for _, path, body in self.jobs_api.calls
                  if path.endswith("/execution-attempts/start")]
        self.assertEqual(Counter(starts), Counter(self.jobs_api.started.keys()), msg=self.evidence())
        self.assertEqual(Counter(item.job_ref for item in self.reports), Counter(starts), msg=self.evidence())
        completed = Counter(item.execution_attempt_ref for item in self.reports
                            if item.operation is TerminalOperation.COMPLETE)
        validations = Counter(frame.correlation.attempt_ref for _, channel in self.science
                              for frame in channel.received_frames if type(frame) is ValidateFrame)
        generations = Counter(frame.correlation.attempt_ref for _, channel in self.science
                              for frame in channel.received_frames if type(frame) is GenerateFrame)
        self.assertEqual(completed, validations, msg=self.evidence())
        self.assertEqual(completed, generations, msg=self.evidence())
        for runner, _ in self.science:
            self.assertTrue(runner.quiescent)
            self.assertFalse(runner.retired)
