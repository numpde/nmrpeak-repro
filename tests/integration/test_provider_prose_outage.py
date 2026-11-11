"""The service respects prose HTTP postponement across Jobs and resumes itself."""

import asyncio
from collections import Counter

from provider_core.attempt_record import TerminalOperation

from nmrpeak_provider.canonical_json import canonical_json_bytes
from tests.fakes.interpreter_values import HF_VALUE, VALUE
from tests.fakes.prose_outage import ProseOutageCase


class ProviderProseOutageTests(ProseOutageCase):
    async def test_retry_after_survives_new_jobs_and_the_same_service_recovers(self):
        await self.check_outage(backlog=64)

    async def test_ten_times_more_waiting_jobs_does_not_raise_outage_traffic(self):
        await self.check_outage(backlog=640)

    async def test_prose_outage_does_not_defer_a_locally_rejected_input(self):
        await self.start_provider(backlog=64)
        await self.wait_for_prose_refusal()
        # The original Job must also settle before unrelated input is offered.
        await self.wait_for_report(
            lambda item: item.job_ref == "job:prose-0" and item.operation is TerminalOperation.FAIL,
            seconds=1,
        )
        invalid_job = "job:invalid-during-prose-outage"
        self.jobs_api.inputs[invalid_job] = (self.chf_kind, b"\xef\xbb\xbf{}")
        failed = await self.wait_for_report(
            lambda item: item.job_ref == invalid_job and item.operation is TerminalOperation.FAIL,
            seconds=1,
        )
        self.assertEqual(self.jobs_api.started[invalid_job], failed.execution_attempt_ref)
        self.assertEqual(1, len(self.prose_server.received), msg=self.evidence())
        self.assertEqual([], self.science[1][1].received_frames, msg=self.evidence())
        self.assertFalse(self.provider_run.done(), msg=self.evidence())

    async def test_deferred_prose_job_does_not_hide_typed_work_on_the_same_lane(self):
        await self.start_provider(backlog=2)
        await self.wait_for_prose_refusal()
        await self.wait_for_report(
            lambda item: item.job_ref == "job:prose-0" and item.operation is TerminalOperation.FAIL,
            seconds=3,
        )
        self.assertGreater(self.prose_services[0].retry_seconds, 0, msg=self.evidence())
        typed_job = "job:typed-hf-during-prose-outage"
        hf_kind = self.jobs_api.inputs["job:prose-0"][0]
        self.jobs_api.inputs[typed_job] = (hf_kind, canonical_json_bytes(HF_VALUE))
        completed = await self.wait_for_report(
            lambda item: item.job_ref == typed_job and item.operation is TerminalOperation.COMPLETE,
            seconds=1,
        )
        self.assertEqual(self.jobs_api.started[typed_job], completed.execution_attempt_ref)
        self.assertGreater(self.prose_services[0].retry_seconds, 0, msg=self.evidence())
        self.assertNotIn("job:prose-1", self.jobs_api.started, msg=self.evidence())
        self.assertEqual(1, len(self.prose_server.received), msg=self.evidence())
        self.assertFalse(self.provider_run.done(), msg=self.evidence())

    async def test_restoring_the_prose_service_drains_every_offered_job_without_restarting_the_provider(self):
        await self.start_provider(backlog=6)
        await self.wait_for_prose_refusal()
        first_request_at = self.prose_server.received[0]["at"]
        self.jobs_api.inputs[self.typed_job] = (self.chf_kind, canonical_json_bytes(VALUE))
        await self.wait_for_report(
            lambda item: item.job_ref == self.typed_job and item.operation is TerminalOperation.COMPLETE,
            seconds=1,
        )
        await asyncio.sleep(max(0, first_request_at + self.outage_seconds - asyncio.get_running_loop().time()))
        failed_before_restoration = {item.job_ref for item in self.reports if item.operation is TerminalOperation.FAIL}
        waiting_before_restoration = set(self.jobs_api.inputs) - self.jobs_api.started.keys()
        self.assertTrue(failed_before_restoration, msg=self.evidence())
        self.assertTrue(waiting_before_restoration, msg=self.evidence())
        self.prose_server.restore()

        # The service discovers every remaining Job itself. The declared drain
        # budget allows the outstanding service wait, one original interpretation
        # allowance per HF Job, and two seconds for runner frames/local handoffs.
        drain_seconds = self.prose_server.retry_after_seconds + 6 * self.interpretation_seconds + 2
        offered_jobs = set(self.jobs_api.inputs)
        await self.wait_for_report(
            lambda _item: offered_jobs <= {item.job_ref for item in self.reports},
            seconds=drain_seconds,
        )
        self.assertFalse(self.provider_run.done(), msg=self.evidence())
        self.assertEqual(Counter(offered_jobs), Counter(item.job_ref for item in self.reports), msg=self.evidence())
        completed = {item.job_ref for item in self.reports if item.operation is TerminalOperation.COMPLETE}
        self.assertTrue(waiting_before_restoration <= completed, msg=self.evidence())
        self.assertTrue(failed_before_restoration.isdisjoint(completed), msg=self.evidence())
        self.assertLessEqual(asyncio.get_running_loop().time() - self.prose_server.restored_at,
                             drain_seconds, msg=self.evidence())

        # All seven original reports reached local custody while the provider
        # still ran. Stop only now, to check final settlement of its original
        # controllers and continued usability of both original runner sessions.
        await self.finish_provider()
        self.assertTrue(self.provider.shutdown_ready, msg=self.evidence())
        self.assert_reports_match_original_science()
