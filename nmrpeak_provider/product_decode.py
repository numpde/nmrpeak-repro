"""Fixed generation choices for the HF and CHF NMRPeak runners."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DecodePolicy:
    """Scientifically material generation arguments owned by one model lane."""

    decode_policy_id: str
    beam_size: int
    maximum_generated_tokens: int
    temperature: Decimal
    seed: int


HF_DECODE_POLICY = DecodePolicy(
    decode_policy_id="nmrpeak_hf_decode_v1",
    beam_size=10,
    maximum_generated_tokens=160,
    temperature=Decimal("3.0"),
    seed=1,
)
CHF_DECODE_POLICY = DecodePolicy(
    decode_policy_id="nmrpeak_chf_decode_v1",
    beam_size=10,
    maximum_generated_tokens=160,
    temperature=Decimal("3.0"),
    seed=1,
)
