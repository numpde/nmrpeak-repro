"""Archive a stopped, held report after an authenticated read proves Attempt closure.

Closure does not prove delivery. The exact original obligation remains private
and durable before its active slot is retired; this command never publishes it.
"""
import argparse
from base64 import b64encode, b64decode
from hashlib import sha256
import re

from ._nmr_api_failures import attempt_is_closed
from .attempt_journal import TerminalPending, parse_journal_record
from .attempt_journal_store import AttemptJournalStore
from .attempt_lifecycle import observe_attempt, AttemptObserved
from .canonical_json import canonical_json_bytes


def _reason(reason):
    if (type(reason) is not str or not reason.strip() or not reason.isprintable()
            or len(reason.encode('utf-8')) > 2048):
        raise ValueError('Archive reason must be nonempty printable text within 2048 UTF-8 bytes')


def archived_original(document, *, expected_digest):
    """Admit private archival evidence for retention and safe idempotent reuse."""
    fields = {'schema_id', 'record_digest', 'execution_attempt_ref', 'owner',
              'original_record_base64', 'reason', 'snapshot', 'delivery'}
    if (type(document) is not dict or set(document) != fields
            or document['schema_id'] != 'nmrpeak.closed_attempt_archive.v1'
            or document['delivery'] != 'unconfirmed'
            or document['record_digest'] != expected_digest):
        raise ValueError('Invalid closed-Attempt archive envelope')
    _reason(document['reason'])
    encoded = document['original_record_base64']
    if type(encoded) is not str:
        raise ValueError('Invalid archived original bytes')
    raw = b64decode(encoded, validate=True)
    if b64encode(raw).decode('ascii') != encoded or 'sha256:' + sha256(raw).hexdigest() != expected_digest:
        raise ValueError('Archived original digest differs from its identity')
    original = parse_journal_record(raw)
    if (type(original) is not TerminalPending or original.terminal_hold_action is None
            or original.terminal_reconciling
            or document['execution_attempt_ref'] != original.execution_attempt_ref):
        raise ValueError('Archive does not retain a held Attempt')
    owner = document['owner']
    if (type(owner) is not dict or set(owner) != {'origin', 'topology', 'authority_id', 'provider_ref'}
            or any(type(value) is not str or not value or not value.isprintable() for value in owner.values())
            or re.fullmatch(r'sha256:[0-9a-f]{64}', owner['authority_id']) is None
            or re.fullmatch(r'provider:[A-Za-z0-9_.-]{1,119}', owner['provider_ref']) is None):
        raise ValueError('Archive ownership is invalid')
    previous = document['snapshot']
    if (type(previous) is not dict or set(previous) != {'execution_attempt_ref', 'job_ref', 'state', 'job_state'}
            or previous['execution_attempt_ref'] != original.execution_attempt_ref
            or previous['job_ref'] != original.job_ref
            or previous['job_state'] not in ('open', 'closed', 'cancelled')
            or not attempt_is_closed(previous['state'])):
        raise ValueError('Archive does not prove this Attempt closed')
    return original


def archive_closed(*, journal, api, runtime, owner, execution_attempt_ref,
                   expected_record_digest, reason):
    """Caller holds deployment and provider locks and validates API volume binding."""
    _reason(reason)
    if (type(owner) is not dict or set(owner) != {'origin', 'topology', 'authority_id', 'provider_ref'}
            or owner['origin'] != api.endpoint.origin or owner['topology'] != api.endpoint.expected_topology
            or owner['provider_ref'] != runtime.hf.generation.provider_ref):
        raise ValueError('Authenticated API or provider differs from retained journal ownership')
    selected = [record for record in journal.records()
                if getattr(record, 'execution_attempt_ref', None) == execution_attempt_ref]
    if len(selected) != 1:
        raise ValueError('Select exactly one current retained Attempt; inspect again')
    record, = selected
    journal.require_current(record)
    raw = journal.record_bytes(record)
    digest = 'sha256:' + sha256(raw).hexdigest()
    if (type(record) is not TerminalPending or record.terminal_hold_action is None
            or record.terminal_reconciling or digest != expected_record_digest):
        raise ValueError('Selection does not match a held record; inspect again')
    runtime.resolve(record)
    observed = observe_attempt(api=api, record=record)
    if type(observed) is not AttemptObserved or not attempt_is_closed(observed.snapshot.state.value):
        raise ValueError('API read did not prove Attempt closure; retained work remains held')
    snapshot = observed.snapshot
    facts = {'execution_attempt_ref': snapshot.execution_attempt_ref, 'job_ref': snapshot.job_ref,
             'state': snapshot.state.value, 'job_state': snapshot.job_state.value}
    document = {'schema_id': 'nmrpeak.closed_attempt_archive.v1', 'record_digest': digest,
                'execution_attempt_ref': execution_attempt_ref, 'owner': owner,
                'original_record_base64': b64encode(raw).decode('ascii'), 'reason': reason,
                'snapshot': facts, 'delivery': 'unconfirmed'}

    def validate_existing(existing):
        archived_original(existing, expected_digest=digest)
        if any(existing[key] != value for key, value in document.items() if key not in {'reason', 'snapshot'}):
            raise ValueError('Existing archive differs; preserve both and investigate')

    path = journal.archive_record(record, raw, document, validate_existing)
    from .archive_document import validate_archive_document
    result = {'schema_id': 'nmrpeak.journal_archive_result.v1', 'archive_path': str(path),
              'record_digest': digest, 'execution_attempt_ref': execution_attempt_ref,
              'delivery': 'unconfirmed',
              'retained': 'Exact original work, identity, hold and evidence remain in the private archive.',
              'api_effect': f'The API read confirmed Attempt state {snapshot.state.value}. No API mutation occurred; closure does not prove report delivery.',
              'automatic': 'This held record will no longer be retried; the provider remains stopped.',
              'next_actor': 'provider_operator',
              'next_action': 'Restart the owning provider to admit eligible work; this does not resend the archived report.'}
    validate_archive_document(result)
    return result


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution-attempt-ref', required=True)
    parser.add_argument('--record-digest', required=True)
    parser.add_argument('--frozen-generation', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--expected-authority-id', required=True)
    parser.add_argument('--expected-provider-ref', required=True)
    args = parser.parse_args(arguments)
    try:
        from .provider_config import CONFIG_PATH, CREDENTIAL_PATH, FROZEN_ROOT, IDENTITY_LOCK_PATH, JOURNAL_PATH, decode_provider_runtime_config, server_a_authority_id
        from .provider_main import _read_regular_file, _CONFIG_MAX_BYTES
        from .provider_credential import parse_provider_signing_credential, PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES
        from .frozen_generation import load_frozen_generation
        from .provider_identity_lock import ProviderIdentityLock
        from .provider_api import ProviderApiClient
        config = decode_provider_runtime_config(_read_regular_file(CONFIG_PATH, _CONFIG_MAX_BYTES))
        frozen = load_frozen_generation(FROZEN_ROOT, expected_frozen_generation_id=args.frozen_generation)
        credential = parse_provider_signing_credential(_read_regular_file(CREDENTIAL_PATH, PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES))
        if (server_a_authority_id(config.endpoint) != args.expected_authority_id
                or credential.provider_ref != args.expected_provider_ref
                or credential.provider_ref != frozen.runtime.hf.generation.provider_ref):
            raise ValueError('API namespace or provider differs from the retained journal binding; inspect the matching deployment configuration')
        with ProviderIdentityLock.acquire(IDENTITY_LOCK_PATH, credential.provider_ref):
            with AttemptJournalStore(JOURNAL_PATH, maximum_records=config.journal_maximum_records,
                                     filesystem_reserve_bytes=config.journal_filesystem_reserve_bytes) as journal:
                api = ProviderApiClient(config.endpoint.materialize(), credential.credential_ref, credential.private_key)
                result = archive_closed(journal=journal, api=api, runtime=frozen.runtime,
                    owner={'origin': config.endpoint.origin, 'topology': config.endpoint.expected_topology,
                           'authority_id': args.expected_authority_id, 'provider_ref': credential.provider_ref},
                    execution_attempt_ref=args.execution_attempt_ref,
                    expected_record_digest=args.record_digest, reason=args.reason)
        print(canonical_json_bytes(result).decode('utf-8'))
    except (RuntimeError, OSError, ValueError, TypeError) as error:
        parser.exit(2, f'Cannot confirm closed-Attempt archival: {error}. Preserve the journal and any archive; inspect before retrying. No report was sent.\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
