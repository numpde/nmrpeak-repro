"""Add reviewed NMR refusal reasons to the SDK's protected failure renderer."""

from provider_core.diagnostics import describe_core_failure

from .attempt_journal_store import (
    AttemptJournalAdmissionRejected, AttemptJournalConflict,
    AttemptJournalStateRejected, AttemptJournalWriteFailed,
)
from .provider_config import ProviderConfigurationRejected


def describe_nmrpeak_failure(error, text, diagnostic_window):
    if type(error) in (
        ProviderConfigurationRejected, AttemptJournalAdmissionRejected,
        AttemptJournalConflict, AttemptJournalStateRejected, AttemptJournalWriteFailed,
    ):
        return {"message": text(str(error), 4096)}
    return describe_core_failure(error, text, diagnostic_window)
