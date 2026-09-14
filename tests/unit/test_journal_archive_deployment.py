"""Archive admission preserves deployment, API and generation ownership."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

from deployment import provider_deployment as deployment
from nmrpeak_provider.canonical_json import canonical_json_bytes
from tests.unit.test_provider_deployment import render_repository, test_plan, IMAGES


ATTEMPT = 'execution_attempt:sha256:' + 'a' * 64
DIGEST = 'sha256:' + 'b' * 64
RESULT = {'schema_id': 'nmrpeak.journal_archive_result.v1',
          'archive_path': '/var/lib/nmrpeak-provider/journal/' + 'b' * 64 + '.archive.json',
          'record_digest': DIGEST, 'execution_attempt_ref': ATTEMPT, 'delivery': 'unconfirmed',
          'retained': 'Exact retained work is archived.', 'api_effect': 'API read confirmed expiry; no mutation.',
          'automatic': 'Provider remains stopped.', 'next_actor': 'provider_operator',
          'next_action': 'Restart to admit eligible work; archived report will not be resent.'}


class ArchiveDeploymentTests(unittest.TestCase):
    def setup_plan(self, stack, repository, *, running=False, attachments=()):
        plan = test_plan(repository)
        deployment.materialize_deployment_plan(repository, 'production', plan)
        stack.enter_context(patch.object(deployment, '_inspect_project_containers', return_value={
            'provider': {'Id': 'owned', 'State': {'Running': running}}}))
        stack.enter_context(patch.object(deployment, 'inspect_provider_journal_volume', return_value=('journal-volume', attachments)))
        stack.enter_context(patch.object(deployment, 'inspect_provider_identity_lock_volume', return_value='lock-volume'))
        stack.enter_context(patch.object(deployment, '_read_owned_private_credential', return_value=(b'', SimpleNamespace(provider_ref=plan.provider_ref))))
        stack.enter_context(patch.object(deployment, '_resolve_provider_image', return_value=IMAGES['provider']))
        command = stack.enter_context(patch.object(deployment, '_docker_command', return_value=subprocess.CompletedProcess((), 0, canonical_json_bytes(RESULT) + b'\n', b'')))
        return plan, command

    def archive(self, repository, plan):
        from deployment.provider_deployment import archive_provider_journal
        return archive_provider_journal(repository, 'production', execution_attempt_ref=ATTEMPT,
                                        record_digest=DIGEST, reason='Investigated expired Attempt.',
                                        frozen_generation=plan.generation.frozen_generation_id)

    def test_running_or_foreign_attachment_is_rejected_before_archiver(self):
        for running, attachments in ((True, ()), (False, ('foreign',))):
            with self.subTest(running=running), render_repository() as repository, ExitStack() as stack:
                plan, command = self.setup_plan(stack, repository, running=running, attachments=attachments)
                with self.assertRaises(deployment.DeploymentOperationRejected):
                    self.archive(repository, plan)
                command.assert_not_called()

    def test_selected_generation_and_only_required_inputs_reach_archiver(self):
        with render_repository() as repository, ExitStack() as stack:
            plan, command = self.setup_plan(stack, repository)
            self.assertEqual(self.archive(repository, plan), canonical_json_bytes(RESULT) + b'\n')
            args = command.call_args.args[1]
            self.assertIn('nmrpeak_provider.journal_archive', args)
            self.assertIn(plan.generation.frozen_generation_id, args)
            self.assertIn('type=volume,src=journal-volume,dst=/var/lib/nmrpeak-provider', args)
            self.assertIn('type=volume,src=lock-volume,dst=/run/nmrpeak-provider-lock,readonly', args)
            self.assertIn('--read-only', args)
            self.assertNotIn('checkpoint', ' '.join(args))
            self.assertNotIn('openai-chat', ' '.join(args))
            self.assertNotIn('session.sock', ' '.join(args))

    def test_wrong_generation_or_output_identity_never_reports_success(self):
        with render_repository() as repository, ExitStack() as stack:
            plan, command = self.setup_plan(stack, repository)
            for result in (RESULT | {'body': 'PRIVATE_RESULT'}, RESULT | {'execution_attempt_ref': 'execution_attempt:sha256:' + 'c'*64}):
                command.return_value = subprocess.CompletedProcess((), 0, canonical_json_bytes(result) + b'\n', b'')
                with self.assertRaises(deployment.DeploymentOperationRejected) as caught:
                    self.archive(repository, plan)
                self.assertNotIn('PRIVATE_RESULT', str(caught.exception))
            frozen = repository / 'secrets/deployments/production/generations' / plan.generation.frozen_generation_id[7:] / 'frozen/manifest.json'
            frozen.chmod(0o600)
            frozen.write_bytes(b'{}')
            command.reset_mock()
            with self.assertRaises((deployment.DeploymentOperationRejected, ValueError)):
                self.archive(repository, plan)
            command.assert_not_called()

    def test_timed_out_archiver_cleanup_requires_exact_invocation_identity(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign), render_repository() as repository, ExitStack() as stack:
                plan, command = self.setup_plan(stack, repository)
                invocation = {}
                identifier = 'd' * 64
                def docker(_, args, **kwargs):
                    if args[0] == 'run':
                        invocation['name'] = args[args.index('--name') + 1]
                        invocation['label'] = args[args.index('--label') + 1]
                        raise deployment.DeploymentOperationRejected('Archive observation timed out')
                    if args[0] == 'ps':
                        raw = (identifier + '\n').encode()
                    elif args[0] == 'inspect':
                        key, value = invocation['label'].split('=', 1)
                        raw = canonical_json_bytes([{'Id': identifier, 'Name': '/' + invocation['name'],
                                'Config': {'Labels': {key: 'foreign' if foreign else value}}}])
                    else:
                        self.assertEqual(args, ('rm', '-f', identifier))
                        raw = b''
                    return subprocess.CompletedProcess((), 0, raw, b'')
                command.side_effect = docker
                with self.assertRaises(deployment.DeploymentOperationRejected) as caught:
                    self.archive(repository, plan)
                self.assertIn('unconfirmed', str(caught.exception))
                self.assertIn('timed out', str(caught.exception))
                self.assertIn('inspect', str(caught.exception).lower())
                removals = [c for c in command.call_args_list if c.args[1][0] == 'rm']
                self.assertEqual(len(removals), 0 if foreign else 1)

    def test_public_make_passes_literal_selection_and_optional_ca_path(self):
        import json
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            fake_python = Path(temporary) / 'python'
            fake_python.write_text('#!/usr/bin/python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            fake_python.chmod(0o700)
            repository = Path(__file__).resolve().parents[2]
            for ca in ('', '/tmp/private CA.pem'):
                result = subprocess.run(['make', '--no-print-directory', '-s', 'provider/deployment/journal/archive-closed',
                    'PYTHON=' + str(fake_python), 'DEPLOYMENT=production', 'ATTEMPT_REF=' + ATTEMPT,
                    'RECORD_DIGEST=' + DIGEST, 'FROZEN_GENERATION=sha256:'+'e'*64,
                    'REASON=Reviewed; $(literal) `literal` remains text', 'LOCALHOST_CA_CERTIFICATE=' + ca],
                    cwd=repository, capture_output=True, check=True)
                args = json.loads(result.stdout)
                self.assertEqual(args[args.index('--reason')+1], 'Reviewed; $(literal) `literal` remains text')
                if ca:
                    self.assertEqual(args[args.index('--localhost-ca-certificate')+1], ca)
                else:
                    self.assertNotIn('--localhost-ca-certificate', args)
