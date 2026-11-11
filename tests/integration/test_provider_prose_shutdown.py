"""NMRPeak stops admitting Jobs but retains an admitted Job until local handoff.

The SDK owns HTTP-process cleanup and the Attempt's delivery obligation.
These tests do not select an HTTP-helper implementation or prescribe how long
a model request is allowed to run after the operator requests service stop.
"""

import asyncio

from tests.fakes.prose_shutdown import ProseShutdownCase


class ProviderProseShutdownTests(ProseShutdownCase):
    async def test_stop_drains_the_admitted_prose_job_without_starting_the_next_job(self):
        await self.start_provider()
        await asyncio.wait_for(self.prose_server.request_seen.wait(), 3)
        self.provider.request_stop()
        await self.assert_original_job_drained()
        self.assertEqual(b"", await self.prose_server.wait_for_client_eof())

    async def test_stop_waits_for_the_original_job_report_handoff_acknowledgement(self):
        await self.start_provider(hold_delivery_ack=True)
        await asyncio.wait_for(self.prose_server.request_seen.wait(), 3)
        self.provider.request_stop()
        await asyncio.wait_for(self.delivery_received.wait(), 4)

        # The local delivery service received the Job's failure report but
        # has not acknowledged accepting custody. NMRPeak cannot exit and
        # must not start the next Job during that wait.
        self.assertEqual(1, len(self.delivery_reports))
        self.assertFalse(self.provider_run.done())
        self.assertFalse(self.provider.shutdown_ready)
        self.assertEqual([self.original_job], list(self.jobs_api.started))

        self.allow_delivery_ack.set()
        await self.assert_original_job_drained()
        self.assertEqual(b"", await self.prose_server.wait_for_client_eof())
