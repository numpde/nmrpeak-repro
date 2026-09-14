from base64 import b64decode
from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import patch
from nmrpeak_provider.attempt_journal import journal_record_name
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from tests.unit.test_attempt_lifecycle import (journal_directory, terminal_pending, TerminalOperation, persist_terminal, generation_runtime, CapturingApi, success_response, attempt_snapshot)

class ArchiveTests(unittest.TestCase):
    def seed(self, store):
        record = replace(terminal_pending(TerminalOperation.FAIL), terminal_hold_action='do_not_resend', terminal_hold_description='Do not resend.', terminal_observed_state='expired')
        persist_terminal(store, record)
        return record

    def call(self, store, record, digest, state='expired'):
        from nmrpeak_provider.journal_archive import archive_closed
        api = CapturingApi(success_response(attempt_snapshot(execution_attempt_ref=record.execution_attempt_ref, job_ref=record.job_ref, state=state, job_state='closed')))
        from types import SimpleNamespace
        api.endpoint = SimpleNamespace(origin='https://api.example', expected_topology='fixture')
        self.last_api = api
        result = archive_closed(journal=store, api=api, runtime=generation_runtime(), owner={'origin':'https://api.example', 'topology':'fixture', 'authority_id':'sha256:'+'a'*64, 'provider_ref':'provider:nmrpeak'}, execution_attempt_ref=record.execution_attempt_ref, expected_record_digest=digest, reason='Investigated retained failure.')
        self.assertEqual(len(api.requests), 1)
        return result

    def test_exact_original_is_durable_before_retirement(self):
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as store:
                record = self.seed(store)
                raw = (root / journal_record_name(record)).read_bytes()
                digest = 'sha256:' + sha256(raw).hexdigest()
                result = self.call(store, record, digest)
                artifact = json.loads(__import__('pathlib').Path(result['archive_path']).read_bytes())
                self.assertEqual(b64decode(artifact['original_record_base64']), raw)
                self.assertEqual(artifact['delivery'], 'unconfirmed')
                self.assertEqual(store.records(), ())
            with AttemptJournalStore(root, maximum_records=2) as reopened:
                self.assertEqual(reopened.records(), ())

    def test_live_attempt_and_stale_selection_preserve_work(self):
        for state, stale in [('in_progress',False), ('expired',True)]:
            with self.subTest(state=state, stale=stale), journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=2) as store:
                    record = self.seed(store)
                    path = root / journal_record_name(record)
                    raw = path.read_bytes()
                    digest = 'sha256:' + ('0'*64 if stale else sha256(raw).hexdigest())
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.call(store, record, digest, state)
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(list(root.glob('*.archive.json')), [])
                    self.assertEqual(len(self.last_api.requests), 0 if stale else 1)

    def test_archive_publication_failure_keeps_active_and_retry_preserves_first_evidence(self):
        from nmrpeak_provider.attempt_journal_store import AttemptJournalWriteFailed
        import os
        for failed_call in (1, 2, 3):
            with self.subTest(failed_call=failed_call), journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=2) as store:
                    record = self.seed(store)
                    path = root / journal_record_name(record)
                    raw = path.read_bytes()
                    digest = 'sha256:' + sha256(raw).hexdigest()
                    real_sync = os.fsync
                    count = 0
                    def failing_sync(fd):
                        nonlocal count
                        count += 1
                        if count == failed_call:
                            raise OSError('fixture storage failure')
                        return real_sync(fd)
                    with patch('nmrpeak_provider.attempt_journal_store.os.fsync', side_effect=failing_sync):
                        with self.assertRaises((OSError, AttemptJournalWriteFailed)):
                            self.call(store, record, digest)
                    if failed_call < 3:
                        self.assertEqual(path.read_bytes(), raw)
                with AttemptJournalStore(root, maximum_records=2) as reopened:
                    if reopened.records():
                        self.call(reopened, record, digest)
                    self.assertEqual(reopened.records(), ())
                    artifact, = root.glob('*.archive.json')
                    self.assertEqual(b64decode(json.loads(artifact.read_bytes())['original_record_base64']), raw)

    def test_existing_different_archive_is_not_overwritten_or_retired(self):
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as store:
                record = self.seed(store)
                path = root / journal_record_name(record)
                raw = path.read_bytes()
                digest = 'sha256:' + sha256(raw).hexdigest()
                archive = root / (digest[7:] + '.archive.json')
                archive.write_bytes(b'{}')
                archive.chmod(0o600)
                with self.assertRaises(ValueError):
                    self.call(store, record, digest)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(archive.read_bytes(), b'{}')

    def test_pending_reconciling_and_changed_generation_are_not_archived(self):
        for changes in ({'terminal_hold_action':None, 'terminal_hold_description':None, 'terminal_observed_state':None},
                        {'terminal_hold_action':'reconcile_state', 'terminal_observed_state':None},
                        {'frozen_generation_id':'sha256:'+'0'*64}):
            with self.subTest(changes=changes), journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=2) as store:
                    record = self.seed(store)
                    changed = replace(record, **changes)
                    store.replace(record, changed)
                    raw = store.record_bytes(changed)
                    with self.assertRaises(ValueError):
                        self.call(store, changed, 'sha256:'+sha256(raw).hexdigest())
                    self.assertEqual(store.records(), (changed,))
                    self.assertEqual(self.last_api.requests, [])

    def test_unsafe_reason_read_only_and_unavailable_reads_never_retire(self):
        from nmrpeak_provider.journal_archive import archive_closed
        from types import SimpleNamespace
        from unittest.mock import Mock
        for reason, readonly in [('', False), ('line\nbreak',False), ('x'*2049,False), ('investigated',True), ('investigated',False)]:
            with self.subTest(reason=reason[:20], readonly=readonly), journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=2) as store:
                    record = self.seed(store)
                    raw = store.record_bytes(record)
                api = Mock(endpoint=SimpleNamespace(origin='https://api.example', expected_topology='fixture'))
                api.send.side_effect = OSError('API unavailable')
                with AttemptJournalStore(root, maximum_records=2, read_only=readonly) as store:
                    with self.assertRaises((ValueError, RuntimeError, OSError)):
                        archive_closed(journal=store, api=api, runtime=generation_runtime(),
                            owner={'origin':'https://api.example', 'topology':'fixture', 'authority_id':'sha256:'+'a'*64, 'provider_ref':'provider:nmrpeak'},
                            execution_attempt_ref=record.execution_attempt_ref,
                            expected_record_digest='sha256:'+sha256(raw).hexdigest(), reason=reason)
                    if reason != 'investigated' or readonly:
                        api.send.assert_not_called()
                    self.assertEqual(store.record_bytes(record), raw)

    def test_malformed_wrong_identity_and_unknown_snapshot_preserve_obligation(self):
        from nmrpeak_provider.journal_archive import archive_closed
        from types import SimpleNamespace
        for changes in ({'execution_attempt_ref':'execution_attempt:sha256:'+'0'*64},
                        {'job_ref':'job:wrong'}, {'state':'invented'}, {'schema_id':'invented'}):
            with self.subTest(changes=changes), journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=2) as store:
                    record = self.seed(store)
                    raw = store.record_bytes(record)
                    snapshot = attempt_snapshot(execution_attempt_ref=record.execution_attempt_ref,
                                                job_ref=record.job_ref, state='expired', job_state='closed')
                    snapshot.update(changes)
                    api = CapturingApi(success_response(snapshot))
                    api.endpoint = SimpleNamespace(origin='https://api.example', expected_topology='fixture')
                    with self.assertRaises(ValueError):
                        archive_closed(journal=store, api=api, runtime=generation_runtime(),
                            owner={'origin':'https://api.example', 'topology':'fixture', 'authority_id':'sha256:'+'a'*64, 'provider_ref':'provider:nmrpeak'},
                            execution_attempt_ref=record.execution_attempt_ref,
                            expected_record_digest='sha256:'+sha256(raw).hexdigest(), reason='Investigated.')
                    self.assertEqual(len(api.requests), 1)
                    self.assertEqual(store.record_bytes(record), raw)

    def test_cli_rejects_authority_and_provider_drift_before_api_construction(self):
        from nmrpeak_provider.journal_archive import main
        from nmrpeak_provider.provider_config import ProviderEndpointConfig, server_a_authority_id
        from types import SimpleNamespace
        from contextlib import ExitStack
        endpoint = ProviderEndpointConfig('https://api.example', 'web', 1.0, 1.0, None)
        # Use the already validated endpoint type; topology validation is authoritative.
        for authority, provider in [('sha256:'+'0'*64, 'provider:nmrpeak'),
                                    (server_a_authority_id(endpoint), 'provider:wrong')]:
            with self.subTest(authority=authority, provider=provider), ExitStack() as stack:
                stack.enter_context(patch('nmrpeak_provider.provider_main._read_regular_file', return_value=b'fixture'))
                stack.enter_context(patch('nmrpeak_provider.provider_config.decode_provider_runtime_config', return_value=SimpleNamespace(endpoint=endpoint)))
                stack.enter_context(patch('nmrpeak_provider.frozen_generation.load_frozen_generation', return_value=SimpleNamespace(runtime=generation_runtime())))
                stack.enter_context(patch('nmrpeak_provider.provider_credential.parse_provider_signing_credential', return_value=SimpleNamespace(provider_ref='provider:nmrpeak')))
                api_constructor = stack.enter_context(patch('nmrpeak_provider.provider_api.ProviderApiClient'))
                stack.enter_context(patch('sys.stderr'))
                with self.assertRaises(SystemExit) as raised:
                    main(['--execution-attempt-ref','execution_attempt:sha256:'+'a'*64,
                          '--record-digest','sha256:'+'b'*64, '--reason','Investigated.',
                          '--frozen-generation',generation_runtime().frozen_generation_id,
                          '--expected-authority-id',authority,'--expected-provider-ref',provider])
                self.assertEqual(raised.exception.code, 2)
                api_constructor.assert_not_called()

    def test_archive_keeps_generation_reference_without_active_replay(self):
        from nmrpeak_provider.journal_inventory import journal_generation_inventory
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as store:
                record = self.seed(store)
                raw = store.record_bytes(record)
                self.call(store, record, 'sha256:'+sha256(raw).hexdigest())
                self.assertEqual(store.records(), ())
            inventory = json.loads(journal_generation_inventory(root))
            self.assertEqual(inventory['frozen_generation_ids'], [record.frozen_generation_id])

    def test_malformed_archive_blocks_generation_cleanup(self):
        from nmrpeak_provider.journal_inventory import journal_generation_inventory
        with journal_directory() as root:
            path = root / ('a'*64+'.archive.json')
            path.write_bytes(b'{}')
            path.chmod(0o600)
            with self.assertRaises((ValueError, RuntimeError)):
                journal_generation_inventory(root)

    def test_failed_close_is_not_retried_on_a_possibly_reused_descriptor(self):
        import os
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as store:
                record = self.seed(store)
                raw = store.record_bytes(record)
                real_close, real_sync = os.close, os.fsync
                staging = []
                closed = []
                def note_sync(fd):
                    if not staging:
                        staging.append(fd)
                    return real_sync(fd)
                def fail_after_close(fd):
                    if staging and fd == staging[0]:
                        closed.append(fd)
                        real_close(fd)
                        raise OSError('fixture close failure after descriptor release')
                    return real_close(fd)
                with patch('nmrpeak_provider.attempt_journal_store.os.fsync', side_effect=note_sync), patch('nmrpeak_provider.attempt_journal_store.os.close', side_effect=fail_after_close):
                    with self.assertRaises(OSError) as raised:
                        self.call(store, record, 'sha256:'+sha256(raw).hexdigest())
                self.assertIn('fixture close failure', str(raised.exception))
                self.assertEqual(len(closed), 1)
                self.assertEqual((root/journal_record_name(record)).read_bytes(), raw)
