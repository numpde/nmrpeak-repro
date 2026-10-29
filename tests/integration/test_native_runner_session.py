"""Attach scientific requests to the installed SDK's actual native child owner."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import errno
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from provider_core.runner import LOCK_NAME
from nmrpeak_provider import native_policy
from nmrpeak_provider.runner_protocol import GenerateFrame
from nmrpeak_provider.runner_session import (
    NativeWorkerExited, RunnerSessionRetired, RunnerDeadlines, open_runner_session,
)
from tests.fakes.native_runner import lane_contract
from tests.unit.test_runner_session import ATTEMPT_REF, ATTEMPT_KEY


@contextmanager
def native_runner(lane, *, generation_mode="healthy", partial_retirement=False, native_budgets=(0.1, 0.1, 1)):
    facts, codec, _, model_input = lane_contract(lane)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        endpoint = root / "runner.sock"
        (root / LOCK_NAME).touch(mode=0o600)
        scratch, status = root / "scratch", root / "status"
        scratch.mkdir(mode=0o700)
        status.mkdir(mode=0o700)
        with (root / "native.log").open("wb") as log:
            owner = subprocess.Popen([
                sys.executable, "-m", "tests.fakes.native_owner",
                "partial" if partial_retirement else "normal",
                "--role", facts.identity.runner_ref,
                "--socket", str(endpoint), "--scratch", str(scratch),
                "--status-directory", str(status), "--owner-session-lifetime",
                "--startup-seconds", "5", "--terminate-seconds", str(native_budgets[0]),
                "--kill-seconds", str(native_budgets[1]), "--cleanup-seconds", str(native_budgets[2]), "--",
                sys.executable, "-m", "tests.fakes.native_runner", "--lane", lane,
                "--generation-mode", generation_mode,
            ], stdout=log, stderr=log)
            try:
                yield endpoint, scratch, facts, codec, model_input
            finally:
                owner.send_signal(signal.SIGTERM)
                try:
                    owner.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    owner.kill()
                    owner.wait(timeout=5)
                    raise AssertionError("Fixture native owner did not settle after shutdown")


class NativeRunnerSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_scientific_eof_with_live_child_stops_and_confirms_retirement(self):
        with native_runner("chf", generation_mode="eof_alive", native_budgets=(
                native_policy.TERMINATE_SECONDS, native_policy.KILL_SECONDS, native_policy.CLEANUP_SECONDS)) as (endpoint, scratch, facts, codec, model_input):
            session = (await open_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec))
            prepared = (await session.join_science(session.validate(execution_attempt_ref=ATTEMPT_REF,
                                        provider_attempt_key=ATTEMPT_KEY, model_input=model_input)))
            path = scratch / session.native.operation
            try:
                with self.assertRaises(RunnerSessionRetired) as caught:
                    (await session.join_science(session.generate(prepared)))
                self.assertNotIsInstance(caught.exception, NativeWorkerExited)
                (await session.retire())
                self.assertTrue(session.retirement_confirmed)
                self.assertIsNone(session.native.original_exit)
                self.assertEqual(session.native.retirement.returncode, -signal.SIGKILL)
                self.assertFalse(path.exists())
            finally:
                # Fixture containment grants no retirement or crash evidence.
                session.native.close()

    async def test_wrapped_reset_observes_actual_exit_before_local_interruption(self):
        with native_runner("chf", generation_mode="crash") as (endpoint, _, facts, codec, model_input):
            session = (await open_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec))
            prepared = (await session.join_science(session.validate(execution_attempt_ref=ATTEMPT_REF,
                                        provider_attempt_key=ATTEMPT_KEY, model_input=model_input)))
            session._channel = GatedChannel(session._channel, codec, reset=True)
            try:
                with self.assertRaises(NativeWorkerExited) as caught:
                    (await session.join_science(session.generate(prepared)))
                original = session.native.original_exit
                self.assertEqual(original.returncode, 17)
                self.assertIs(caught.exception.native, session.native)
            finally:
                (await session.retire())
            self.assertIs(session.native.original_exit, original)
            self.assertTrue(session.retirement_confirmed)

    async def test_matching_failure_frame_requires_observed_original_exit(self):
        with native_runner("hf", generation_mode="reported_failure") as (endpoint, _, facts, codec, model_input):
            session = (await open_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec))
            prepared = (await session.join_science(session.validate(execution_attempt_ref=ATTEMPT_REF,
                                        provider_attempt_key=ATTEMPT_KEY, model_input=model_input)))
            try:
                with self.assertRaises(NativeWorkerExited) as caught:
                    (await session.join_science(session.generate(prepared)))
                self.assertEqual(session.native.original_exit.returncode, 1)
                self.assertIn("Scientific fixture failed", str(caught.exception.cause))
            finally:
                (await session.retire())

    async def test_stop_before_observation_does_not_reconstruct_a_physically_earlier_exit(self):
        with native_runner("chf", generation_mode="crash") as (endpoint, scratch, facts, codec, model_input):
            session = await open_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec)
            prepared = await session.join_science(session.validate(
                execution_attempt_ref=ATTEMPT_REF, provider_attempt_key=ATTEMPT_KEY, model_input=model_input))
            pid = int((scratch / session.native.operation / "child.pid").read_text())
            import os
            pidfd = os.pidfd_open(pid)
            gate = GatedChannel(session._channel, codec)
            session._channel = gate
            original = session.generate(prepared)
            try:
                self.assertTrue(await asyncio.to_thread(gate.entered.wait, 2))
                exited = await asyncio.to_thread(select.select, [pidfd], [], [], 2)
                self.assertTrue(exited[0])
                session.cancel()
            finally:
                gate.release.set()
                os.close(pidfd)
            with self.assertRaises(RunnerSessionRetired) as caught:
                await session.join_science(original)
            self.assertNotIsInstance(caught.exception, NativeWorkerExited)
            await session.retire()
            self.assertEqual(session.native.retirement.returncode, 17)
            self.assertIsNone(session.native.original_exit)

    async def test_expired_wait_retains_the_original_native_observation(self):
        with native_runner("hf") as (endpoint, _, facts, codec, _):
            session = await open_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec)
            session.cancel()
            entered, release = Event(), Event()
            observe = session.native.observe_retirement

            def delayed_completion(*, deadline):
                observe(deadline=deadline)
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Fixture did not release the original native observer")

            with patch.object(session.native, "observe_retirement", side_effect=delayed_completion) as reader:
                try:
                    with self.assertRaises(TimeoutError):
                        await session.control.observe_retirement(wait_seconds=0.02)
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    retained = session.control.reader
                    self.assertFalse(retained.done())
                    retiring = asyncio.create_task(session.retire())
                    await asyncio.sleep(0)
                    retiring.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await retiring
                    self.assertIs(session.control.reader, retained)
                    self.assertFalse(session.retirement_confirmed)
                finally:
                    release.set()
                    await session.retire()
                self.assertIs(session.control.reader, retained)
                self.assertEqual(reader.call_count, 1)
                self.assertTrue(session.retirement_confirmed)

    async def test_partial_native_frame_poison_retains_exclusion_without_a_second_reader(self):
        with native_runner("chf", partial_retirement=True) as (endpoint, _, facts, codec, _):
            session = (await open_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec))
            with patch.object(session.native, "observe_retirement", wraps=session.native.observe_retirement) as reader:
                with self.assertRaises(RunnerSessionRetired):
                    (await session.retire())
                retained = session.control.reader
                with self.assertRaises(RunnerSessionRetired):
                    (await session.retire())
                self.assertIs(session.control.reader, retained)
                self.assertEqual(reader.call_count, 1)
                self.assertIsNone(session.native.original_exit)
                self.assertFalse(session.retirement_confirmed)
            # Fixture containment closes the failed object; it grants no receipt.
            session.native.close()

    async def test_cancellation_after_validation_confirms_native_retirement(self):
        with native_runner("hf") as (endpoint, scratch, facts, codec, model_input):
            session = (await open_runner_session(str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec))
            (await session.join_science(session.validate(execution_attempt_ref=ATTEMPT_REF,
                             provider_attempt_key=ATTEMPT_KEY, model_input=model_input)))
            path = scratch / session.native.operation
            session.cancel()
            (await session.retire())
            self.assertTrue(session.retirement_confirmed)
            self.assertIsNone(session.native.original_exit)
            self.assertFalse(path.exists())

    async def test_sdk_attachment_keeps_warm_science_then_confirms_child_and_scratch_retirement(self):
        for lane in ("hf", "chf"):
            with self.subTest(lane=lane), native_runner(lane) as (endpoint, scratch, facts, codec, model_input):
                session = (await open_runner_session(
                    str(endpoint), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec,
                ))
                try:
                    operation = session.native.operation
                    child_path = scratch / operation
                    child_pid = (child_path / "child.pid").read_text()
                    for _ in range(2):
                        prepared = (await session.join_science(session.validate(
                            execution_attempt_ref=ATTEMPT_REF,
                            provider_attempt_key=ATTEMPT_KEY,
                            model_input=model_input,
                        )))
                        self.assertEqual((await session.join_science(session.generate(prepared))).value, ["CCO"])
                        self.assertEqual((child_path / "child.pid").read_text(), child_pid)
                        self.assertEqual(session.native.operation, operation)
                finally:
                    (await session.retire())
                self.assertTrue(session.native.retirement.scratch_released)
                self.assertFalse(child_path.exists())
                self.assertTrue(session.retirement_confirmed)



class GatedChannel:
    """Fault only the original receive after an actual Generate dispatch."""

    def __init__(self, channel, codec, *, reset=False):
        self.channel, self.codec, self.reset = channel, codec, reset
        self.entered, self.release = Event(), Event()
        self.generating = False

    def __getattr__(self, name):
        return getattr(self.channel, name)

    def sendall(self, data):
        self.channel.sendall(data)
        self.generating = type(self.codec.decode_frame(data)) is GenerateFrame

    def recv_into(self, buffer):
        if self.generating:
            self.entered.set()
            if self.reset:
                raise ConnectionResetError(errno.ECONNRESET, "Injected original receive reset")
            if not self.release.wait(3):
                raise AssertionError("Fixture did not release the original scientific read")
        return self.channel.recv_into(buffer)
