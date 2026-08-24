"""Durable idempotency identity for one logical provider Attempt."""

from __future__ import annotations

from hashlib import sha256
import re

from .canonical_json import JsonValue, canonical_json_bytes


_ATTEMPT_DOMAIN = b"nmrpeak.provider_attempt.v1\0"
_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")
_JOB_REF = re.compile(r"job:[A-Za-z0-9_.-]{1,124}")
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")


def derive_provider_attempt_key(
    *,
    provider_ref: str,
    run_generation_fingerprint: str,
    job_ref: str,
    input_fingerprint: str,
) -> str:
    """Hash only the immutable facts that define one logical start."""

    _require_string_match(provider_ref, _PROVIDER_REF, "provider_ref")
    _require_string_match(
        run_generation_fingerprint,
        _SHA256_REF,
        "run_generation_fingerprint",
    )
    _require_string_match(job_ref, _JOB_REF, "job_ref")
    _require_string_match(input_fingerprint, _SHA256_REF, "input_fingerprint")
    material: dict[str, JsonValue] = {
        "v": 1,
        "provider_ref": provider_ref,
        "run_generation_fingerprint": run_generation_fingerprint,
        "job_ref": job_ref,
        "input_fingerprint": input_fingerprint,
    }
    digest = sha256(
        _ATTEMPT_DOMAIN + canonical_json_bytes(material)
    ).hexdigest()
    return f"nmrpeak-provider.v1:{digest}"


def _require_string_match(
    value: object,
    pattern: re.Pattern[str],
    field: str,
) -> None:
    if type(value) is not str:
        raise TypeError(f"Provider Attempt {field} must be a string")
    if pattern.fullmatch(value) is None:
        raise ValueError(f"Provider Attempt {field} has an invalid format")
