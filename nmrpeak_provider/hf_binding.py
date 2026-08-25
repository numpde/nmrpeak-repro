"""Project admitted HF science across the private runner boundary."""

from __future__ import annotations

from dataclasses import dataclass

from .canonical_json import JsonValue, canonical_json_bytes
from .nmrpeak_binding import (
    RunnerProtonPeak,
    parse_runner_proton_peaks,
    project_proton_peaks,
    proton_peak_documents,
)
from .product_input import HfModelInput


@dataclass(frozen=True, slots=True)
class HfRunnerInput:
    """The immutable formula and proton payload sent to the HF runner."""

    molecular_formula: str
    proton_peaks: tuple[RunnerProtonPeak, ...]

    def canonical_bytes(self) -> bytes:
        """Render canonical wire bytes without JSON floating-point ambiguity."""

        return canonical_json_bytes(self.wire_document())

    def wire_document(self) -> dict[str, JsonValue]:
        """Render a fresh JSON value for the enclosing protocol frame."""

        return {
            "h_nmr_peaks": [
                {
                    "category": peak.category,
                    "centroid": peak.centroid,
                    "j_values": peak.j_values,
                    "nH": peak.n_h,
                }
                for peak in self.proton_peaks
            ],
            "molecular_formula": self.molecular_formula,
        }


def bind_hf_runner_input(model_input: HfModelInput) -> HfRunnerInput:
    """Convert the parser-owned HF model into its one runner projection."""

    if type(model_input) is not HfModelInput:
        raise TypeError("HF runner binding requires an admitted HF model input")
    return HfRunnerInput(
        molecular_formula=model_input.formula,
        proton_peaks=project_proton_peaks(model_input),
    )


def materialize_hf_nmrpeak_document(
    runner_input: HfRunnerInput,
) -> dict[str, object]:
    """Convert wire decimals to the numeric document consumed inside the runner."""

    return {
        "h_nmr_peaks": proton_peak_documents(runner_input.proton_peaks),
        "molecular_formula": runner_input.molecular_formula,
    }


def parse_hf_runner_input(value: object) -> HfRunnerInput:
    """Parse the exact private HF payload before runner materialization."""

    fields = {"h_nmr_peaks", "molecular_formula"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("Cannot parse HF runner input: object fields are not exact")
    formula = value["molecular_formula"]
    if type(formula) is not str or not formula:
        raise ValueError("Cannot parse HF runner input: formula must be text")
    return HfRunnerInput(
        molecular_formula=formula,
        proton_peaks=parse_runner_proton_peaks(value["h_nmr_peaks"]),
    )
