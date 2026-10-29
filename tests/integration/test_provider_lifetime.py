"""Keep real FD exclusion while a fatal CLI still owns an unfinished SCI thread."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest


_CLI = r'''
import asyncio
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import ssl
import sys
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from provider_core.api_config import ApiSettings, HttpsSettings
from nmrpeak_provider import provider_main
from nmrpeak_provider.frozen_generation import FrozenGeneration
from nmrpeak_provider.provider_config import RunnerDeadlines, decode_provider_runtime_config
from nmrpeak_provider.provider_startup import ProviderStartup
from nmrpeak_provider.provider_identity_lock import ProviderIdentityLock
from nmrpeak_provider.runner_session import RunnerSession
from tests.fakes.native_runner import lane_contract
from tests.fakes.runner import FakeNativeControl, FakeRunnerChannel
from tests.fakes.shared_api import SharedApi
from tests.unit.test_frozen_generation import FILES, runtime
from tests.unit.test_runtime_config_v2 import RUNTIME

lock_path, release, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
descriptor = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
identity_lock = ProviderIdentityLock(descriptor, "provider:nmrpeak")
# Root-owned file admission is covered separately; this probe owns a real flock.
entered = Event()
deadlines = RunnerDeadlines(2, 2, 5, 5, 0.2)

def blocked(**_arguments):
    entered.set()
    while not release.exists():
        time.sleep(0.01)

async def prepare(socket, facts, _policy, codec):
    _, _, ready, _ = lane_contract("hf" if "/hf/" in socket else "chf")
    ready = replace(ready, release_sha256=facts.checkpoint_ref, image_input_id=facts.image_input_ref)
    channel = FakeRunnerChannel(codec, ready)
    owner = RunnerSession(None, facts, deadlines, codec, None,
                          native=FakeNativeControl(channel, deadlines), startup_started=time.monotonic())
    if "/hf/" in socket:
        owner._validate = blocked
    return owner

class Process:
    shutdown_ready = True  # This fixture creates no API/Delivery exchanges.

    def __init__(self, **arguments):
        self.runner = arguments["hf_session"]

    def request_stop(self):
        pass

    async def run(self, *, on_ready):
        if mode == "blocked":
            _, _, _, model_input = lane_contract("hf")
            self.original = self.runner.validate(
                execution_attempt_ref="execution_attempt:sha256:" + "2" * 64,
                provider_attempt_key="nmrpeak-provider.v1:" + "3" * 64,
                model_input=model_input,
            )
            while not entered.is_set():
                await asyncio.sleep(0.001)
            raise RuntimeError("Injected service failure after original SCI dispatch")

settings = ApiSettings(SharedApi({}).identity,
                       HttpsSettings(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), 1, 1), "web")
binding = SimpleNamespace(api={"provider_ref": "provider:nmrpeak"})
definition = SimpleNamespace(request_seconds=1)
readiness = Mock()
with patch.object(provider_main, "load_provider_startup", return_value=ProviderStartup(decode_provider_runtime_config(RUNTIME), definition, binding, settings)), \
     patch.object(provider_main, "load_frozen_generation", return_value=FrozenGeneration("sha256:" + "1" * 64, runtime(), FILES)), \
     patch.object(provider_main, "InputInterpreter"), \
     patch.object(provider_main, "load_openai_chat_endpoint_specs", return_value=()), \
     patch.object(provider_main, "_require_empty_legacy_journal"), \
     patch.object(provider_main.ProviderReadiness, "begin", return_value=readiness), \
     patch.object(provider_main.ProviderIdentityLock, "acquire_volume", return_value=identity_lock), \
     patch.object(provider_main, "prepare_runner_session", side_effect=prepare), \
     patch.object(provider_main, "ProviderProcess", Process):
    status = provider_main.main()
readiness.close.assert_called_once()
print("CLI_STATUS=" + str(status), flush=True)
if mode == "clean":
    while not release.exists():
        time.sleep(0.01)
raise SystemExit(status)
'''


class ProviderLifetimeTests(unittest.TestCase):
    @contextmanager
    def cli(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock, release = root / "identity.lock", root / "release"
            lock.write_bytes(b"provider:nmrpeak\n")
            child = subprocess.Popen([sys.executable, "-P", "-c", _CLI, str(lock), str(release), mode],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "CLI did not report its outcome")
                self.assertEqual(child.stdout.readline().strip(), b"CLI_STATUS=" + (b"1" if mode == "blocked" else b"0"))
                self.assertIsNone(child.poll(), "This probe requires a still-running original process")
                with lock.open("rb") as competitor:
                    yield competitor
            finally:
                release.write_bytes(b"finish original SCI work")
                try:
                    _, errors = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    raise AssertionError("Owned qualification child failed to exit after its SCI gate was released")
            self.assertEqual(child.returncode, 1 if mode == "blocked" else 0, errors.decode(errors="replace"))
            with lock.open("rb") as after_exit:
                fcntl.flock(after_exit, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_unconfirmed_science_keeps_exclusion_until_the_original_process_exits(self):
        with self.cli("blocked") as competitor:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_confirmed_cleanup_explicitly_releases_exclusion_while_process_is_alive(self):
        with self.cli("clean") as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
