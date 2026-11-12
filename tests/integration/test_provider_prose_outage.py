"""Prose outages propagate on exhaustion and recover within a live model window."""

from collections import Counter

from provider_core.attempt_record import TerminalOperation

from nmrpeak_provider.canonical_json import canonical_json_bytes
from tests.fakes.interpreter_values import HF_VALUE, VALUE
from tests.fakes.prose_outage import ProseOutageCase


class ProviderProseOutageTests(ProseOutageCase):
    async def test_exhausted_service_preserves_the_original_operator_failure(self):
        await self.check_outage(backlog=64)

    async def test_ten_times_more_waiting_jobs_does_not_raise_outage_traffic(self):
        await self.check_outage(backlog=640)

    async def test_prose_outage_does_not_defer_a_locally_rejected_input(self):
        await self.start_provider(backlog=64, interpretation_seconds=6)
        await self.wait_for_prose_refusal()
        invalid_job = "job:invalid-during-prose-outage"
        self.jobs_api.inputs[invalid_job] = (self.chf_kind, b"\xef\xbb\xbf{}")
        failed = await self.wait_for_report(
            lambda item: item.job_ref == invalid_job and item.operation is TerminalOperation.FAIL,
            seconds=1,
        )
        self.assertEqual(self.jobs_api.started[invalid_job], failed.execution_attempt_ref)
        self.assertEqual(1, len(self.prose_server.received), msg=self.evidence())
        self.assertEqual([], self.science[1][1].received_frames, msg=self.evidence())
        await self.wait_for_operator_failure()
        self.assertEqual(2, len(self.prose_server.received), msg=self.evidence())
        first, second = self.prose_server.received
        self.assertEqual("job:prose-0", second["job_ref"])
        self.assertFalse(second["available"])
        self.assertGreaterEqual(second["at"] - first["at"], self.prose_server.retry_after_seconds)
        self.assert_reports_match_original_science(held_jobs=("job:prose-0",))

    async def test_exhaustion_does_not_start_queued_typed_work_on_the_same_lane(self):
        await self.start_provider(backlog=2)
        await self.wait_for_prose_refusal()
        self.assertGreater(self.prose_services[0].retry_seconds, 0, msg=self.evidence())
        typed_job = "job:typed-hf-during-prose-outage"
        hf_kind = self.jobs_api.inputs["job:prose-0"][0]
        self.jobs_api.inputs[typed_job] = (hf_kind, canonical_json_bytes(HF_VALUE))
        await self.wait_for_operator_failure()
        self.assertEqual(["job:prose-0"], list(self.jobs_api.started), msg=self.evidence())
        self.assertEqual([], self.reports, msg=self.evidence())
        self.assertEqual([], self.science[0][1].received_frames, msg=self.evidence())
        self.assertEqual(1, len(self.prose_server.received), msg=self.evidence())

    async def test_restoration_before_exhaustion_drains_jobs_without_restarting_provider(self):
        # This window spans Retry-After. Restore the peer before its next send,
        # so the admitted Job can succeed through the existing SDK HTTP retry.
        await self.start_provider(backlog=6, interpretation_seconds=6)
        await self.wait_for_prose_refusal()
        self.jobs_api.inputs[self.typed_job] = (self.chf_kind, canonical_json_bytes(VALUE))
        await self.wait_for_report(
            lambda item: item.job_ref == self.typed_job and item.operation is TerminalOperation.COMPLETE,
            seconds=1,
        )
        waiting_before_restoration = set(self.jobs_api.inputs) - self.jobs_api.started.keys()
        self.assertTrue(waiting_before_restoration, msg=self.evidence())
        self.assertFalse(any(item.operation is TerminalOperation.FAIL for item in self.reports))
        self.prose_server.restore()
        offered_jobs = set(self.jobs_api.inputs)
        drain_seconds = self.prose_server.retry_after_seconds + 6 * self.interpretation_seconds + 2
        await self.wait_for_report(
            lambda _item: offered_jobs <= {item.job_ref for item in self.reports},
            seconds=drain_seconds,
        )
        self.assertFalse(self.provider_run.done(), msg=self.evidence())
        self.assertEqual(Counter(offered_jobs), Counter(item.job_ref for item in self.reports), msg=self.evidence())
        self.assertTrue(all(item.operation is TerminalOperation.COMPLETE for item in self.reports), msg=self.evidence())
        first, recovered = self.prose_server.received[:2]
        self.assertEqual(first["job_ref"], recovered["job_ref"])
        self.assertFalse(first["available"])
        self.assertTrue(recovered["available"])
        self.assertGreaterEqual(recovered["at"] - first["at"], self.prose_server.retry_after_seconds)
        await self.finish_provider()
        self.assertTrue(self.provider.shutdown_ready, msg=self.evidence())
        self.assert_reports_match_original_science()
