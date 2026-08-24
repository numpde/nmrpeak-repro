"""Bounded canonical success results for the fixed NMRPeak product."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .canonical_json import JsonValue, canonical_json_bytes
from .product_decode import CHF_DECODE_POLICY, HF_DECODE_POLICY, DecodePolicy


RESULT_SCHEMA_ID = "nmrpeak.structure_candidates.result.v1"
_DECODE_POLICIES = (HF_DECODE_POLICY, CHF_DECODE_POLICY)
MAX_CANDIDATES = max(policy.beam_size for policy in _DECODE_POLICIES)
MAX_GENERATED_TOKENS = max(
    policy.maximum_generated_tokens for policy in _DECODE_POLICIES
)
MAX_DECODER_SYMBOL_CHARACTERS = 22
MAX_DECODER_SYMBOL_BYTES = 22
MAX_GENERATED_CHARACTERS = (
    (MAX_GENERATED_TOKENS - 1) * MAX_DECODER_SYMBOL_CHARACTERS
)
MAX_GENERATED_BYTES = (
    (MAX_GENERATED_TOKENS - 1) * MAX_DECODER_SYMBOL_BYTES
)
MAX_RESULT_BYTES = 65_536
NMRPEAK_SOURCE_CLOSURE_REF = (
    "sha256:94c255c72e7791f6566bf9b5b2f6a80c9446e6f622fa7a5259d15b79c6234bc6"
)

SUPPORTED_GENERATED_CHARACTERS = frozenset(
    "#$%'()*+-./0123456789:=@ABCDEFGHIKLMNOPRSTUVWXYZ[\\]"
    "_abcdefghijklmnopqrstuvxy∞"
)
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ResultLaneIdentity:
    """The fixed runner and decode policy behind one product lane."""

    runner_ref: str
    decode_policy: DecodePolicy


HF_RESULT_IDENTITY = ResultLaneIdentity(
    runner_ref="nmrpeak_hf_v1",
    decode_policy=HF_DECODE_POLICY,
)
CHF_RESULT_IDENTITY = ResultLaneIdentity(
    runner_ref="nmrpeak_chf_v1",
    decode_policy=CHF_DECODE_POLICY,
)
_RESULT_IDENTITIES = (HF_RESULT_IDENTITY, CHF_RESULT_IDENTITY)


@dataclass(frozen=True, slots=True)
class ProviderResultFacts:
    """Provider-admitted immutable facts attached to one model result."""

    identity: ResultLaneIdentity
    runner_contract_id: str
    checkpoint_ref: str
    image_input_ref: str


class RunnerResultRejected(ValueError):
    """Hostile runner output cannot be journaled as a success."""


def canonical_result_bytes(
    candidates: object,
    facts: ProviderResultFacts,
) -> bytes:
    """Validate runner candidates and attach only provider-owned provenance."""

    _validate_result_facts(facts)
    generated_smiles = _validate_candidates(candidates)
    result: dict[str, JsonValue] = {
        "schema_id": RESULT_SCHEMA_ID,
        "candidates": [
            {"generated_smiles": candidate} for candidate in generated_smiles
        ],
        "provenance": {
            "runner_ref": facts.identity.runner_ref,
            "runner_contract_id": facts.runner_contract_id,
            "checkpoint_sha256": facts.checkpoint_ref,
            "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
            "image_input_id": facts.image_input_ref,
            "target": "cpu-x86_64",
            "device": "cpu",
            "decode_policy_id": facts.identity.decode_policy.decode_policy_id,
        },
    }
    encoded = canonical_json_bytes(result)
    if len(encoded) > MAX_RESULT_BYTES:
        raise RunnerResultRejected("Runner result exceeds its canonical byte limit")
    return encoded


def source_closure_ref(manifest: bytes) -> str:
    """Derive the portable closure identity from its exact committed manifest."""

    if type(manifest) is not bytes:
        raise TypeError("NMRPeak source manifest must be supplied as exact bytes")
    return f"sha256:{sha256(manifest).hexdigest()}"


def _validate_result_facts(facts: ProviderResultFacts) -> None:
    if type(facts) is not ProviderResultFacts:
        raise TypeError("NMRPeak result provenance must be provider-owned facts")
    if not any(facts.identity is identity for identity in _RESULT_IDENTITIES):
        raise AssertionError("NMRPeak result uses an unowned runner identity")
    if (
        type(facts.runner_contract_id) is not str
        or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", facts.runner_contract_id) is None
    ):
        raise ValueError("NMRPeak result provenance requires a runner contract identity")
    for reference in (facts.checkpoint_ref, facts.image_input_ref):
        if type(reference) is not str or _SHA256_REF.fullmatch(reference) is None:
            raise ValueError(
                "NMRPeak result provenance requires exact SHA-256 identities"
            )


def _validate_candidates(candidates: object) -> tuple[str, ...]:
    if type(candidates) is not list:
        raise RunnerResultRejected("Runner result candidates must be a JSON array")
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise RunnerResultRejected(
            f"Runner result must contain between one and {MAX_CANDIDATES} candidates"
        )
    validated: list[str] = []
    for candidate in candidates:
        if type(candidate) is not str or not candidate:
            raise RunnerResultRejected(
                "Runner result candidates must be non-empty strings"
            )
        if (
            len(candidate) > MAX_GENERATED_CHARACTERS
            or len(candidate.encode("utf-8")) > MAX_GENERATED_BYTES
        ):
            raise RunnerResultRejected("Runner result candidate exceeds its size limit")
        outside_decoder_vocabulary = any(
            character not in SUPPORTED_GENERATED_CHARACTERS
            for character in candidate
        )
        if outside_decoder_vocabulary:
            raise RunnerResultRejected(
                "Runner result candidate contains text outside the decoder vocabulary"
            )
        validated.append(candidate)
    return tuple(validated)
