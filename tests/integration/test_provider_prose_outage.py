"""The service respects prose HTTP postponement across Jobs and resumes itself."""

from tests.fakes.prose_outage import ProseOutageCase


class ProviderProseOutageTests(ProseOutageCase):
    async def test_retry_after_survives_new_jobs_and_the_same_service_recovers(self):
        await self.check_outage(backlog=64)

    async def test_ten_times_more_waiting_jobs_does_not_raise_outage_traffic(self):
        await self.check_outage(backlog=640)
