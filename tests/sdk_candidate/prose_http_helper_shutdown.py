"""Stop NMRPeak while its prose interpreter's HTTP subprocess is still owned.

Run explicitly against an installed SDK wheel that starts provider_core.chat_worker.
The ordinary integration suite also checks service shutdown on the released SDK,
which performs HTTP in the provider process.
"""

import asyncio
from importlib.util import find_spec
from unittest.mock import patch

from tests.fakes.prose_shutdown import ProseShutdownCase


class ProseHttpHelperShutdownTests(ProseShutdownCase):
    async def test_stop_after_os_creates_helper_before_sdk_receives_handle(self):
        await self.check_stop_with_helper(hold_launch=True)

    async def test_stop_after_prose_endpoint_receives_request_before_it_answers(self):
        await self.check_stop_with_helper(hold_launch=False)

    async def check_stop_with_helper(self, *, hold_launch):
        self.assertIsNotNone(
            find_spec("provider_core.chat_worker"),
            msg="Install the SDK candidate with a prose HTTP helper to run this qualification.",
        )
        helper_created = asyncio.Event()
        return_handle_to_sdk = asyncio.Event()
        os_reported_exit = asyncio.Event()
        return_exit_to_sdk = asyncio.Event()
        os_wait_by_helper = {}
        provider_ready_when_exit_returned = []
        spawn = asyncio.create_subprocess_exec
        if not hold_launch:
            return_handle_to_sdk.set()

        async def observe_prose_helper(*command, **options):
            process = await spawn(*command, **options)
            if ("-m", "provider_core.chat_worker") not in zip(command, command[1:]):
                return process
            wait_for_os_exit = process.wait
            os_wait_by_helper[process] = wait_for_os_exit

            async def deliver_exit_to_sdk():
                returncode = await wait_for_os_exit()
                os_reported_exit.set()
                await return_exit_to_sdk.wait()
                provider_ready_when_exit_returned.append(self.provider.shutdown_ready)
                return returncode

            process.wait = deliver_exit_to_sdk
            helper_created.set()
            await return_handle_to_sdk.wait()
            return process

        # Only delay delivery of real OS observations. NMRPeak and the installed
        # SDK still construct the request, run the child and decide when to stop.
        with patch.object(asyncio, "create_subprocess_exec", observe_prose_helper):
            try:
                await self.start_provider()
                await asyncio.wait_for(helper_created.wait(), 5)
                if not hold_launch:
                    await asyncio.wait_for(self.prose_server.request_seen.wait(), 5)
                else:
                    self.assertFalse(self.prose_server.request_seen.is_set())

                original_deadline, = self.request_deadlines
                self.assertGreater(original_deadline, asyncio.get_running_loop().time())
                self.provider.request_stop()
                self.assertFalse(self.provider.shutdown_ready)
                return_handle_to_sdk.set()
                await asyncio.wait_for(self.prose_server.request_seen.wait(), 5)

                # The test HTTPS server never answers. The original helper
                # exits at the interpreter deadline. Until the SDK receives
                # that exit result, the service must still own this Job.
                await asyncio.wait_for(os_reported_exit.wait(), 5)
                self.assertEqual(1, len(os_wait_by_helper))
                helper, = os_wait_by_helper
                self.assertIsNotNone(helper.returncode)
                self.assertGreaterEqual(asyncio.get_running_loop().time(), original_deadline)
                self.assertEqual(b"", await self.prose_server.wait_for_client_eof())
                self.assertFalse(self.provider_run.done())
                self.assertFalse(self.provider.shutdown_ready)
                self.assertEqual(1, len(self.provider._claims.live_owners()))

                return_exit_to_sdk.set()
                await self.assert_original_job_drained()
                self.assertEqual([original_deadline], self.request_deadlines)
                self.assertEqual([False], provider_ready_when_exit_returned)
            finally:
                return_handle_to_sdk.set()
                return_exit_to_sdk.set()
                # Release a test child even if a regression fails an assertion
                # before NMRPeak can finish its normal drain. Never use this
                # cleanup as evidence of successful service shutdown.
                for helper, wait_for_os_exit in os_wait_by_helper.items():
                    if helper.returncode is None:
                        try:
                            helper.kill()
                        except ProcessLookupError:
                            pass
                        await asyncio.wait_for(wait_for_os_exit(), 3)
