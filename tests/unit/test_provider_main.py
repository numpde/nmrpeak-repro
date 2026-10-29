"""Keep partial startup and independent runner cleanup with the composition root."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import signal
import ssl
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from provider_core.api_config import ApiSettings, HttpsSettings
from nmrpeak_provider import provider_main
from nmrpeak_provider.attempt_journal_store import AttemptJournalStateRejected, AttemptJournalStore
from nmrpeak_provider.frozen_generation import FrozenGeneration
from nmrpeak_provider.provider_config import decode_provider_runtime_config
from nmrpeak_provider.provider_startup import ProviderStartup
from tests.unit.test_attempt_journal_store import journal_directory, start_pending
from tests.unit.test_frozen_generation import FILES, runtime
from tests.fakes.shared_api import SharedApi
from tests.unit.test_runtime_config_v2 import RUNTIME


def runner(name, events, *, connect_failure=None, retire_failure=None):
    owner = SimpleNamespace(retirement_confirmed=False)

    async def connect():
        events.append(name + ":connect")
        if connect_failure is not None:
            raise connect_failure

    async def retire():
        events.append(name + ":retire")
        if retire_failure is not None:
            raise retire_failure
        owner.retirement_confirmed = True

    owner.connect, owner.retire = connect, retire
    return owner


class ProviderEntrypointTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_effects_require_the_identity_lock_and_empty_legacy_guard(self):
        frozen = FrozenGeneration("sha256:" + "1" * 64, runtime(), FILES)
        settings = ApiSettings(SharedApi({}).identity,
                               HttpsSettings(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), 30, 20), "web")
        binding = SimpleNamespace(api={"provider_ref": "provider:nmrpeak"})
        definition = SimpleNamespace(selected=Mock(return_value=binding), request_seconds=5)
        readiness = Mock()
        held = False

        def close_lock():
            nonlocal held
            held = False

        def acquire_lock(*_arguments):
            nonlocal held
            held = True
            return SimpleNamespace(close=close_lock)

        def guard(_path):
            self.assertTrue(held)

        async def serve(**arguments):
            self.assertTrue(held)
            guarded.assert_called_once()
            self.assertIs(arguments["binding"], binding)
            self.assertEqual(arguments["settings"].http.proxy_socket, "/routes/selected/proxy.sock")
            self.assertIsNone(settings.http.proxy_socket)
            arguments["identity_lock"].close()

        guarded, serving = Mock(side_effect=guard), AsyncMock(side_effect=serve)
        replacements = {
            "load_provider_startup": Mock(return_value=ProviderStartup(
                decode_provider_runtime_config(RUNTIME), definition, binding,
                replace(settings, http=replace(settings.http, proxy_socket="/routes/selected/proxy.sock")),
            )),
            "load_frozen_generation": Mock(return_value=frozen),
            "load_openai_chat_endpoint_specs": Mock(return_value=()),
            "InputInterpreter": Mock(), "_require_empty_legacy_journal": guarded,
            "_serve": serving,
        }
        with ExitStack() as stack:
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(provider_main, name, replacement))
            stack.enter_context(patch.object(provider_main.ProviderReadiness, "begin", return_value=readiness))
            stack.enter_context(patch.object(provider_main.ProviderIdentityLock, "acquire", side_effect=acquire_lock))
            provider_main.run_provider()
            self.assertFalse(held)
            blocked = RuntimeError("Retained legacy work blocks startup")
            guarded.side_effect = blocked
            with self.assertRaises(RuntimeError) as caught:
                provider_main.run_provider()
            self.assertIs(caught.exception, blocked)
            serving.assert_awaited_once()
        self.assertFalse(held)
        self.assertEqual(readiness.close.call_count, 2)

    async def test_unusable_scientific_state_cannot_skip_native_retirement(self):
        events = []
        owner = runner("hf", events)
        owner.retired = True
        self.assertEqual(await provider_main._retire_runners(owner, None), [])
        self.assertEqual(events, ["hf:retire"])
        self.assertTrue(owner.retirement_confirmed)

    def test_hello_uses_only_authenticated_frozen_descriptions(self):
        frozen = FrozenGeneration("sha256:" + "1" * 64, runtime(), FILES)
        announcement = provider_main._prepare_hello(frozen)
        self.assertEqual([item.description for item in announcement.analysis_offerings],
                         [file.content.decode() for file in FILES[:2]])
        self.assertEqual(announcement.description,
                         "Generates candidate molecular structures from molecular formula and "
                         "NMR peak-list input. License, attribution, and source details: "
                         "https://numpde.github.io/nmrpeak-repro/")

    def test_entrypoint_logs_application_info_with_utc_timestamps(self):
        program = '''
import logging
from unittest.mock import patch
from nmrpeak_provider.provider_main import main
def run():
    logging.getLogger('nmrpeak_provider.provider_process').info('Hello receipt visible')
    logging.getLogger('nmrpeak_runner.worker').info('Runner progress visible')
    logging.getLogger('httpx').info('Dependency chatter')
with patch('nmrpeak_provider.provider_main.run_provider', run):
    raise SystemExit(main())
'''
        result = subprocess.run([sys.executable, "-P", "-c", program],
                                capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO")
        for message in ("Hello receipt visible", "Runner progress visible", "Provider stopped"):
            self.assertIn(message, result.stderr)
        self.assertNotIn("Dependency chatter", result.stderr)

    async def _serve(self, hf, chf, *, service=None, handlers=None):
        policy = decode_provider_runtime_config(RUNTIME).policy
        lane = SimpleNamespace(result_facts=object(), runner_codec=object())
        frozen = SimpleNamespace(runtime=SimpleNamespace(hf=lane, chf=lane))
        process = service or SimpleNamespace(run=AsyncMock(), request_stop=Mock(), shutdown_ready=True)
        handlers = {} if handlers is None else handlers

        def install(number, handler):
            previous = handlers.get(number, signal.SIG_DFL)
            handlers[number] = handler
            return previous

        with patch.object(provider_main, "prepare_runner_session", side_effect=[hf, chf]), \
                patch.object(provider_main, "ProviderProcess", return_value=process), \
                patch.object(provider_main.signal, "signal", side_effect=install):
            await provider_main._serve(
                policy=policy, frozen=frozen, interpreter=object(), settings=object(),
                binding=object(), delivery_seconds=5, announcement=object(),
                readiness=SimpleNamespace(publish=Mock()), identity_lock=Mock(),
            )
        return process

    async def test_signal_during_startup_retires_the_retained_owner_without_admission(self):
        events, handlers = [], {}
        hf, chf = runner("hf", events), runner("chf", events)

        async def interrupted_connect():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            await asyncio.sleep(0)

        hf.connect = interrupted_connect
        service = await asyncio.create_task(self._serve(hf, chf, handlers=handlers))
        service.run.assert_not_awaited()
        self.assertTrue(hf.retirement_confirmed)
        self.assertFalse(chf.retirement_confirmed)
        self.assertEqual(events, ["hf:retire"])
        self.assertEqual(handlers, {signal.SIGINT: signal.SIG_DFL, signal.SIGTERM: signal.SIG_DFL})

    async def test_signal_during_service_requests_admission_stop_then_retires_runners(self):
        events, handlers = [], {}
        hf, chf = runner("hf", events), runner("chf", events)

        async def run(*, on_ready):
            on_ready()
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        service = SimpleNamespace(run=AsyncMock(side_effect=run), request_stop=Mock(), shutdown_ready=True)
        await self._serve(hf, chf, service=service, handlers=handlers)
        service.request_stop.assert_called_once()
        self.assertTrue(hf.retirement_confirmed and chf.retirement_confirmed)

    async def test_partial_startup_retires_both_original_owners_before_raising(self):
        events = []
        failure = RuntimeError("CHF READY failed")
        hf, chf = runner("hf", events), runner("chf", events, connect_failure=failure)
        with self.assertRaises(RuntimeError) as caught:
            await self._serve(hf, chf)
        self.assertIs(caught.exception, failure)
        self.assertEqual(events, ["hf:connect", "chf:connect", "hf:retire", "chf:retire"])
        self.assertTrue(hf.retirement_confirmed and chf.retirement_confirmed)

    async def test_independent_retirement_failure_cannot_hide_primary_or_skip_sibling(self):
        events = []
        primary = RuntimeError("CHF admission failed")
        cleanup = BaseExceptionGroup("HF retirement failed", [SystemExit(7)])
        hf = runner("hf", events, retire_failure=cleanup)
        chf = runner("chf", events, connect_failure=primary)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await self._serve(hf, chf)
        self.assertEqual(caught.exception.exceptions, (primary, cleanup))
        self.assertTrue(chf.retirement_confirmed)

    async def test_background_fatal_failure_retires_loaded_runners(self):
        events = []
        failure = BaseExceptionGroup("Background failure", [SystemExit(0)])
        service = SimpleNamespace(run=AsyncMock(side_effect=failure), request_stop=Mock(), shutdown_ready=True)
        hf, chf = runner("hf", events), runner("chf", events)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await self._serve(hf, chf, service=service)
        self.assertIs(caught.exception, failure)
        self.assertTrue(hf.retirement_confirmed and chf.retirement_confirmed)

    async def test_successful_service_run_still_retires_both_loaded_runners(self):
        events = []
        hf, chf = runner("hf", events), runner("chf", events)
        service = await self._serve(hf, chf)
        service.run.assert_awaited_once()
        self.assertTrue(hf.retirement_confirmed and chf.retirement_confirmed)

    def test_legacy_guard_refuses_original_records_without_mutating_them(self):
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as store:
                store.admit(start_pending("1"))
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with self.assertRaisesRegex(RuntimeError, "legacy Attempt records"):
                provider_main._require_empty_legacy_journal(root)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_unreadable_legacy_state_cannot_be_treated_as_empty(self):
        with journal_directory() as root:
            (root / "unexpected").write_bytes(b"unrecognized state")
            with self.assertRaises(AttemptJournalStateRejected):
                provider_main._require_empty_legacy_journal(root)
            self.assertEqual((root / "unexpected").read_bytes(), b"unrecognized state")

    def test_background_exit_zero_is_a_failure_at_the_cli_boundary(self):
        with patch.object(provider_main, "run_provider", side_effect=SystemExit(0)), \
                patch.object(provider_main, "configure_process_logging"), \
                self.assertLogs(provider_main._LOG, level="ERROR"):
            self.assertEqual(provider_main.main(), 1)

    def test_diagnostic_failure_retains_the_original_process_failure(self):
        primary = RuntimeError("Private runtime failure")
        diagnostic = SystemExit(0)
        with patch.object(provider_main, "run_provider", side_effect=primary), \
                patch.object(provider_main, "configure_process_logging"), \
                patch.object(provider_main._LOG, "error", side_effect=diagnostic):
            with self.assertRaises(BaseExceptionGroup) as caught:
                provider_main.main()
        self.assertEqual(caught.exception.exceptions, (primary, diagnostic))

    def test_cli_diagnostic_exit_zero_cannot_report_success_or_print_private_traceback(self):
        program = '''
import runpy
from unittest.mock import Mock, patch
readiness = Mock()
with patch('nmrpeak_provider.provider_readiness.ProviderReadiness.begin', return_value=readiness), \\
     patch('nmrpeak_provider.provider_startup.read_startup_input', side_effect=RuntimeError('private-runtime-sentinel')), \\
     patch('logging.Logger.error', side_effect=SystemExit(0)):
    try:
        runpy.run_module('nmrpeak_provider.provider_main', run_name='__main__')
    finally:
        readiness.close.assert_called_once()
'''
        result = subprocess.run([sys.executable, "-P", "-c", program], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"private-runtime-sentinel", result.stdout + result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
