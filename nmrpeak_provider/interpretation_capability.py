"""Own NMRPeak's scientific interpretation schema and candidate construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .canonical_json import canonical_json_bytes
from .input_issue_message import render_candidate_correction
from .interpreter import InterpreterCandidateConstructionRejected, InterpreterProtocolError
from .product import CHF_OFFERING, HF_OFFERING
from .product_input import INPUT_SCHEMA_ID, InputRejected, parse_job_input

if TYPE_CHECKING:
    from .lifecycle_lane import LifecycleLane


_PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_PROMPT_PATHS = {
    HF_OFFERING: _PROMPT_DIRECTORY / "hf_interpreter.md",
    CHF_OFFERING: _PROMPT_DIRECTORY / "chf_interpreter.md",
}
_SUBMIT_DESCRIPTION = (
    "Submit the complete HF or CHF NMRPeak request, copying every reported "
    "scientific value from the source without correction or inference."
)
_REPORT_DESCRIPTION = (
    "Report difficulty producing a complete, source-faithful interpretation. "
    "Cite source evidence when available; a rejected candidate alone does not prove the source is wrong."
)


def _value_schema_for(lane: LifecycleLane) -> dict[str, object]:
    """Describe the selected product shape; the typed parser remains authoritative."""

    proton_peak = {
        "type": "object",
        "properties": {
            "shift_lo": {"type": "string"},
            "shift_hi": {"type": "string"},
            "integral": {"type": "string"},
            # Do not enum the vocabulary: unsupported source labels must not
            # be silently substituted before the product parser can reject them.
            "multiplicity": {"type": "string"},
            "j_hz": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["shift_lo", "shift_hi", "integral", "multiplicity", "j_hz"],
        "additionalProperties": False,
    }
    proton_spectrum = {
        "type": "object",
        "properties": {
            "peaks": {"type": "array", "minItems": 1, "items": proton_peak}
        },
        "required": ["peaks"],
        "additionalProperties": False,
    }
    spectra: dict[str, object] = {"1H": proton_spectrum}
    if lane.offering is CHF_OFFERING:
        spectra["13C"] = {
            "type": "object",
            "properties": {
                "peaks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {"shift": {"type": "string"}},
                        "required": ["shift"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["peaks"],
            "additionalProperties": False,
        }
    elif lane.offering is not HF_OFFERING:
        raise AssertionError("NMRPeak tool schema requires a product-owned lane")
    return {
        "type": "object",
        "properties": {
            "schema_id": {"const": INPUT_SCHEMA_ID},
            "model_input": {
                "type": "object",
                "properties": {
                    "formula": {"type": "string"},
                    "spectra": {
                        "type": "object",
                        "properties": spectra,
                        "required": list(spectra),
                        "additionalProperties": False,
                    },
                },
                "required": ["formula", "spectra"],
                "additionalProperties": False,
            },
        },
        "required": ["schema_id", "model_input"],
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class _InterpretationCapability:
    lane: LifecycleLane
    diagnostic_secrets: tuple[str, ...] = ()

    @property
    def interpreter_prompt_path(self) -> Path:
        return _PROMPT_PATHS[self.lane.offering]

    def construct_interpretation(self, value: object, /) -> object:
        try:
            encoded = canonical_json_bytes(value)
        except (TypeError, ValueError, UnicodeError) as error:
            raise InterpreterProtocolError(str(error)) from error
        try:
            return parse_job_input(encoded, self.lane.offering, diagnostic_secrets=self.diagnostic_secrets)
        except InputRejected as error:
            raise InterpreterCandidateConstructionRejected(
                render_candidate_correction(error.issue),
                error.issue,
            ) from error
