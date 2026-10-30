"""Keep stopped journal API effects with their original SDK owner."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nmrpeak_provider.attempt_journal_store import AttemptJournalConflict
from nmrpeak_provider.journal_api import load_journal_api, settled_journal_api
from nmrpeak_provider.journal_identity import journal_api_authority_id


class JournalApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_and_independent_retirement_failure_both_survive(self):
        primary, cleanup = SystemExit(0), OSError('retirement failed')
        api = SimpleNamespace(retire=AsyncMock(side_effect=cleanup))
        with self.assertRaises(BaseExceptionGroup) as caught:
            async with settled_journal_api(api):
                raise primary
        self.assertEqual(caught.exception.exceptions, (primary, cleanup))
        api.retire.assert_awaited_once_with()

    async def test_retirement_failure_never_returns_a_successful_operation(self):
        cleanup = RuntimeError('retirement remains unconfirmed')
        api = SimpleNamespace(retire=AsyncMock(side_effect=cleanup))
        with self.assertRaises(RuntimeError) as caught:
            async with settled_journal_api(api):
                pass
        self.assertIs(caught.exception, cleanup)

    async def test_caller_cancellation_keeps_identity_after_successful_retirement(self):
        import asyncio
        cancelled = asyncio.CancelledError('original caller')
        api = SimpleNamespace(retire=AsyncMock())
        with self.assertRaises(asyncio.CancelledError) as caught:
            async with settled_journal_api(api):
                raise cancelled
        self.assertIs(caught.exception, cancelled)
        api.retire.assert_awaited_once_with()

    def test_binding_identity_is_checked_before_constructing_the_http_owner(self):
        binding = SimpleNamespace(api={'provider_ref': 'provider:nmrpeak',
                                       'origin': 'https://api.example', 'topology': 'web'})
        transport = Mock()
        settings = SimpleNamespace(identity=SimpleNamespace(origin='https://api.example'), expected_topology='web',
                                   http=SimpleNamespace(new_transport=transport))
        authority = journal_api_authority_id(origin=binding.api['origin'], topology='web')
        for provider, namespace in [('provider:other', authority), ('provider:nmrpeak', 'sha256:' + '0'*64)]:
            with self.subTest(provider=provider), \
                 patch('nmrpeak_provider.journal_api.load_maintenance_api',
                       return_value=(settings, 'provider:nmrpeak', None)), \
                 self.assertRaises(AttemptJournalConflict):
                load_journal_api(proxy_socket='/route/api.sock',
                                 expected_provider_ref=provider, expected_authority_id=namespace)
        transport.assert_not_called()


class ArchiveIdentityLifetimeTests(unittest.TestCase):
    def test_cli_releases_real_exclusion_only_after_api_retirement_is_confirmed(self):
        import fcntl
        import os
        from pathlib import Path
        import tempfile
        from nmrpeak_provider import journal_archive
        from nmrpeak_provider.provider_identity_lock import ProviderIdentityLock
        from tests.fakes.retained_journal import generation_runtime

        for settled in (False, True):
            with self.subTest(settled=settled), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'identity.lock'
                path.touch()
                descriptor = os.open(path, os.O_RDONLY)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = ProviderIdentityLock(descriptor, 'provider:nmrpeak')
                failure = OSError('HTTP owner did not retire')
                api = SimpleNamespace(retirement_confirmed=settled, last_failure=None,
                                      origin='https://api.example', expected_topology='web',
                                      retire=AsyncMock(side_effect=None if settled else failure))
                store = Mock()
                store.__enter__ = Mock(return_value=store)
                store.__exit__ = Mock(return_value=False)
                arguments = ['--execution-attempt-ref', 'execution_attempt:sha256:' + 'a'*64,
                             '--record-digest', 'sha256:' + 'b'*64, '--reason', 'Investigated.',
                             '--frozen-generation', generation_runtime().frozen_generation_id,
                             '--expected-authority-id', 'sha256:' + 'c'*64,
                             '--expected-provider-ref', 'provider:nmrpeak',
                             '--proxy-socket', '/route/api.sock']
                try:
                    with patch('nmrpeak_provider.frozen_generation.load_frozen_generation',
                               return_value=SimpleNamespace(runtime=generation_runtime())), \
                         patch('nmrpeak_provider.journal_api.load_journal_api', return_value=api), \
                         patch.object(ProviderIdentityLock, 'acquire', return_value=held), \
                         patch.object(journal_archive, 'AttemptJournalStore', return_value=store), \
                         patch.object(journal_archive, 'archive_closed', new=AsyncMock(return_value={'delivery': 'unconfirmed'})) as archive, \
                         patch('sys.stdout'), patch('sys.stderr'):
                        status = journal_archive.main(arguments)
                    self.assertEqual(status, 0 if settled else 2)
                    self.assertIs(archive.await_args.kwargs['api'], api)
                    api.retire.assert_awaited_once_with()
                    with path.open('rb') as competitor:
                        if settled:
                            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        else:
                            os.fstat(descriptor)
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    held.close()


class EmptyLiveInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_empty_page_must_reach_an_actual_end(self):
        from nmrpeak_provider.journal_api import require_empty_live_inventory
        pages = [SimpleNamespace(attempts=(), next_cursor=value) for value in ('first', 'second', None)]
        api = SimpleNamespace(execution_attempts=AsyncMock(side_effect=pages))
        await require_empty_live_inventory(api, maximum_pages=3)
        self.assertEqual([call.kwargs for call in api.execution_attempts.await_args_list],
                         [dict(state='in_progress', limit=50, cursor=value) for value in (None, 'first', 'second')])

    async def test_live_rows_repeated_cursor_and_page_exhaustion_never_authorize_deletion(self):
        from nmrpeak_provider.journal_api import require_empty_live_inventory
        for pages, maximum, calls in [
            ([SimpleNamespace(attempts=(object(),), next_cursor=None)], 2, 1),
            ([SimpleNamespace(attempts=(), next_cursor='same')]*2, 3, 2),
            ([SimpleNamespace(attempts=(), next_cursor='more')], 1, 1),
        ]:
            with self.subTest(maximum=maximum, calls=calls):
                api = SimpleNamespace(execution_attempts=AsyncMock(side_effect=pages))
                with self.assertRaises(AttemptJournalConflict):
                    await require_empty_live_inventory(api, maximum_pages=maximum)
                self.assertEqual(api.execution_attempts.await_count, calls)

    def test_cli_emits_no_complete_empty_marker_if_original_api_retirement_fails(self):
        from io import StringIO
        from nmrpeak_provider.journal_api import main
        api = SimpleNamespace(execution_attempts=AsyncMock(return_value=SimpleNamespace(attempts=(), next_cursor=None)),
                              retire=AsyncMock(side_effect=OSError('retirement unconfirmed')), last_failure=None)
        output = StringIO()
        with patch('nmrpeak_provider.journal_api.load_journal_api', return_value=api), \
             patch('sys.stdout', output), patch('sys.stderr'):
            status = main(['--proxy-socket', '/route/api.sock',
                           '--expected-provider-ref', 'provider:nmrpeak',
                           '--expected-authority-id', 'sha256:'+'a'*64, '--maximum-pages', '2'])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), '')
        api.retire.assert_awaited_once_with()
