"""A normal NMRPeak stop closes admission and drains the original prose Job."""

import asyncio

from tests.fakes.prose_shutdown import ProseShutdownCase


class ProviderProseShutdownTests(ProseShutdownCase):
    async def test_stop_drains_the_admitted_prose_job_without_starting_the_next_job(self):
        await self.start_provider()
        await asyncio.wait_for(self.prose_server.request_seen.wait(), 3)
        original_deadline, = self.request_deadlines
        self.assertGreater(original_deadline, asyncio.get_running_loop().time())
        self.provider.request_stop()
        # The test HTTPS server still holds the only reply. An admission stop
        # must let the original request reach its deadline and report failure.
        await self.assert_original_job_drained()
        self.assertGreaterEqual(asyncio.get_running_loop().time(), original_deadline)
        self.assertEqual([original_deadline], self.request_deadlines)
        self.assertEqual(b"", await self.prose_server.wait_for_client_eof())
