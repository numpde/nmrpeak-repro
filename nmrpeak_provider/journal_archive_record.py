"""Admit private historical archive bytes without acquiring API authority."""

from base64 import b64decode, b64encode
from hashlib import sha256
import re

from .attempt_journal import TerminalPending, parse_journal_record


def require_archive_reason(reason):
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
    require_archive_reason(document['reason'])
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
            or previous['state'] not in ('succeeded', 'failed', 'expired')):
        raise ValueError('Archive does not prove this Attempt closed')
    return original
