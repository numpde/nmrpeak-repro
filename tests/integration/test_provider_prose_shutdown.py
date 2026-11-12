"""Service stop retains admitted prose work through failure or COMPLETE handoff.

An unanswered request exhausts its model window and propagates privately.
A response accepted before exhaustion creates a real COMPLETE obligation;
local delivery must acknowledge it before the stopped service can exit.
"""

import asyncio

from tests.fakes.prose_shutdown import ProseShutdownCase


class ProviderProseShutdownTests(ProseShutdownCase):
    async def test_stop_preserves_exhausted_original_without_starting_next_job(self):
        await self.start_provider()
        await asyncio.wait_for(self.prose_server.request_seen.wait(), 3)
        self.provider.request_stop()
        await self.assert_original_job_failed_privately()
        self.assertEqual(b"", await self.prose_server.wait_for_client_eof())

    async def test_stop_waits_for_original_complete_handoff_acknowledgement(self):
        await self.start_provider(hold_delivery_ack=True, answer=True)
        await asyncio.wait_for(self.prose_server.request_seen.wait(), 3)
        self.provider.request_stop()
        self.release_test_reply.set()
        await asyncio.wait_for(self.delivery_received.wait(), 4)

        # The original successful report has reached the local delivery
        # service. Custody remains with NMRPeak until that service accepts it.
        self.assertEqual(1, len(self.delivery_reports))
        self.assertFalse(self.provider_run.done())
        self.assertFalse(self.provider.shutdown_ready)
        self.assertEqual([self.original_job], list(self.jobs_api.started))

        self.allow_delivery_ack.set()
        await self.assert_original_job_drained()
