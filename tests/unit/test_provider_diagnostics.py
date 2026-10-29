"""Render reviewed refusal reasons while SDK code owns causal graph traversal."""

import json
import unittest
from provider_core.deployment_binding import BindingDefinitionRejected
from provider_core.diagnostics import failure_evidence
from nmrpeak_provider.attempt_journal_store import AttemptJournalStateRejected
from nmrpeak_provider.provider_config import ProviderConfigurationRejected
from nmrpeak_provider.provider_diagnostics import describe_nmrpeak_failure


class ProviderDiagnosticsTests(unittest.TestCase):
    def evidence(self, error):
        return failure_evidence(error, secrets=('fixture-key',), describe_owner=describe_nmrpeak_failure)

    def test_owned_reason_and_original_native_cause_survive_redaction(self):
        native = FileNotFoundError(2, 'missing')
        try:
            raise ProviderConfigurationRejected('Cannot read the selected binding; api_key=fixture-key') from native
        except ProviderConfigurationRejected as error:
            evidence = self.evidence(error)
        rendered = json.dumps(evidence)
        self.assertIn('Cannot read the selected binding', rendered)
        self.assertNotIn('fixture-key', rendered)
        self.assertIn('FileNotFoundError', rendered)
        self.assertIn('errno', rendered)

    def test_legacy_journal_operator_instructions_are_visible(self):
        evidence = self.evidence(AttemptJournalStateRejected('Preserve the legacy journal; inspect it before restart.'))
        self.assertIn('inspect it before restart', evidence['message'])

    def test_sdk_owned_reason_is_delegated_without_replacing_the_renderer(self):
        evidence = self.evidence(BindingDefinitionRejected('Selected binding does not exist.'))
        self.assertEqual(evidence['message'], 'Selected binding does not exist.')

    def test_unknown_local_exceptions_do_not_disclose_their_text(self):
        for error in (ValueError('private model text'), RuntimeError('private key material')):
            self.assertNotIn('private', json.dumps(self.evidence(error)))
