"""Bounded archival explanation shared by the installed command and deployment host."""
import re
from .canonical_json import canonical_json_bytes

_FIELDS = {'schema_id', 'archive_path', 'record_digest', 'execution_attempt_ref',
           'delivery', 'retained', 'api_effect', 'automatic', 'next_actor', 'next_action'}


def validate_archive_document(document):
    def reject():
        raise ValueError('Invalid journal archive result; inspect retained state before retrying')
    if type(document) is not dict or set(document) != _FIELDS:
        reject()
    for value in document.values():
        if type(value) is not str or not value or not value.isprintable():
            reject()
    if (document['schema_id'] != 'nmrpeak.journal_archive_result.v1'
            or document['delivery'] != 'unconfirmed' or document['next_actor'] != 'provider_operator'
            or re.fullmatch(r'sha256:[0-9a-f]{64}', document['record_digest']) is None
            or re.fullmatch(r'execution_attempt:sha256:[0-9a-f]{64}', document['execution_attempt_ref']) is None
            or not document['archive_path'].endswith('/' + document['record_digest'][7:] + '.archive.json')):
        reject()
    if len(canonical_json_bytes(document)) > 16384:
        reject()
    return document
