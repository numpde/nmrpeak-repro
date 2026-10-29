"""Retain the scientific/native owner before setup can launch its child."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from nmrpeak_provider.provider_config import RunnerDeadlines
from nmrpeak_provider.runner_session import RunnerAdmissionError, prepare_runner_session
from tests.integration.test_native_runner_session import native_runner
from tests.fakes.native_runner import lane_contract


class NativeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_lock_wait_keeps_the_loop_responsive_and_cancels_without_effect(self):
        facts, codec, _, _ = lane_contract("hf")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = asyncio.create_task(prepare_runner_session(
                str(root / "session.sock"), facts, RunnerDeadlines(1, 1, 1, 1, 20), codec,
            ))
            try:
                await asyncio.sleep(0)
                self.assertFalse(original.done(), "Endpoint probing blocked the event loop")
            finally:
                original.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await original
            self.assertEqual(list(root.iterdir()), [])

    async def test_stop_before_control_connection_confirms_no_child_effect(self):
        with native_runner("hf") as (endpoint, scratch, facts, codec, _):
            owner = await prepare_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec)
            self.assertFalse(owner.native.control_connected)
            self.assertEqual(list(scratch.iterdir()), [])
            owner.cancel()
            with self.assertRaises(RunnerAdmissionError) as caught:
                await owner.connect()
            self.assertIs(caught.exception.owner, owner)
            self.assertTrue(owner.retirement_confirmed)
            self.assertFalse(owner.native.control_connected)
            self.assertIsNone(owner.native.original_exit)
            self.assertEqual(list(scratch.iterdir()), [])
