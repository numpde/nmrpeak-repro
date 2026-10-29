"""Archive a stopped, held report after an authenticated read proves Attempt closure.

Closure does not prove delivery. The exact original obligation remains private
and durable before its active slot is retired; this command never publishes it.
"""
import argparse
import asyncio
from base64 import b64encode
from hashlib import sha256

from .attempt_journal import TerminalPending
from .attempt_journal_store import AttemptJournalStore
from .canonical_json import canonical_json_bytes
from .journal_archive_record import archived_original, require_archive_reason



async def archive_closed(*, journal, api, runtime, owner, execution_attempt_ref,
                   expected_record_digest, reason):
    """Caller holds deployment and provider locks and validates API volume binding."""
    require_archive_reason(reason)
    if (type(owner) is not dict or set(owner) != {'origin', 'topology', 'authority_id', 'provider_ref'}
            or owner['origin'] != api.origin or owner['topology'] != api.expected_topology
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
    snapshot = await api.read_attempt(record.execution_attempt_ref, job_ref=record.job_ref)
    if snapshot.state not in ('succeeded', 'failed', 'expired'):
        raise ValueError('API read did not prove Attempt closure; retained work remains held')
    facts = {'execution_attempt_ref': snapshot.execution_attempt_ref, 'job_ref': snapshot.job_ref,
             'state': snapshot.state, 'job_state': snapshot.job_state}
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
              'api_effect': f'The API read confirmed Attempt state {snapshot.state}. No API mutation occurred; closure does not prove report delivery.',
              'automatic': 'This held record will no longer be retried; the provider remains stopped.',
              'next_actor': 'provider_operator',
              'next_action': 'Inspect the journal and resolve every remaining legacy obligation before restarting the owning provider; restart does not resend archived reports.'}
    validate_archive_document(result)
    return result


async def _archive_with_api(api, frozen, args):
    from .journal_api import settled_journal_api
    from .attempt_journal_store import JOURNAL_MAXIMUM_RECORDS
    from .provider_config import JOURNAL_PATH

    async with settled_journal_api(api):
        with AttemptJournalStore(JOURNAL_PATH, maximum_records=JOURNAL_MAXIMUM_RECORDS) as journal:
            return await archive_closed(
                journal=journal, api=api, runtime=frozen.runtime,
                owner={'origin': api.origin, 'topology': api.expected_topology,
                       'authority_id': args.expected_authority_id,
                       'provider_ref': args.expected_provider_ref},
                execution_attempt_ref=args.execution_attempt_ref,
                expected_record_digest=args.record_digest, reason=args.reason,
            )


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution-attempt-ref', required=True)
    parser.add_argument('--record-digest', required=True)
    parser.add_argument('--frozen-generation', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--expected-authority-id', required=True)
    parser.add_argument('--expected-provider-ref', required=True)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--proxy-socket', required=True)
    args = parser.parse_args(arguments)
    api = None
    try:
        from .provider_config import FROZEN_ROOT, IDENTITY_LOCK_PATH
        from .frozen_generation import load_frozen_generation
        from .provider_identity_lock import ProviderIdentityLock
        from .journal_api import load_journal_api

        frozen = load_frozen_generation(FROZEN_ROOT, expected_frozen_generation_id=args.frozen_generation)
        if frozen.runtime.hf.generation.provider_ref != args.expected_provider_ref:
            raise ValueError('Frozen generation belongs to another provider; preserve the retained journal')
        api = load_journal_api(
            binding_name=args.binding, proxy_socket=args.proxy_socket,
            expected_provider_ref=args.expected_provider_ref,
            expected_authority_id=args.expected_authority_id,
        )
        identity_lock = ProviderIdentityLock.acquire(IDENTITY_LOCK_PATH, args.expected_provider_ref)
        primary = None
        try:
            result = asyncio.run(_archive_with_api(api, frozen, args))
        except BaseException as cause:
            primary = cause
            raise
        finally:
            # An unsettled HTTP owner retains exclusion until process teardown.
            if api.retirement_confirmed:
                try:
                    identity_lock.close()
                except BaseException as cleanup:
                    if primary is not None and cleanup is not primary:
                        raise BaseExceptionGroup('Archive operation and identity cleanup failed', [primary, cleanup]) from None
                    raise
        print(canonical_json_bytes(result).decode('utf-8'))
    except BaseException as error:
        import json
        import sys
        from .journal_api import journal_api_failure
        evidence = {'operation': 'archive one closed Attempt',
                    **journal_api_failure(error, api)}
        print(json.dumps(evidence), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
