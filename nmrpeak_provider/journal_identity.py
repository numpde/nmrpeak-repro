"""Preserve historical API namespace labels on existing NMR journal volumes."""

from hashlib import sha256
from .canonical_json import canonical_json_bytes


def journal_api_authority_id(*, origin, topology):
    material = canonical_json_bytes({'origin': origin, 'topology': topology})
    return 'sha256:' + sha256(b'nmrpeak.server_a_authority.v1\0' + material).hexdigest()
