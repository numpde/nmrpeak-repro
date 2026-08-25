"""Project admitted CHF science across the private runner boundary."""

from __future__ import annotations

from dataclasses import dataclass

from .canonical_json import JsonValue, canonical_json_bytes
from .nmrpeak_binding import (
    RunnerProtonPeak,
    canonical_decimal,
    canonical_decimal_text,
    parse_runner_proton_peaks,
    project_proton_peaks,
    proton_peak_documents,
)
from .product_input import ChfModelInput


@dataclass(frozen=True, slots=True)
class ChfRunnerCarbonPeak:
    """One carbon observation in the runner's stable wire representation."""

    shift: str


@dataclass(frozen=True, slots=True)
class ChfRunnerInput:
    """The immutable scientific payload sent to the CHF runner."""

    molecular_formula: str
    proton_peaks: tuple[RunnerProtonPeak, ...]
    carbon_peaks: tuple[ChfRunnerCarbonPeak, ...]

    def canonical_bytes(self) -> bytes:
        """Render canonical wire bytes without JSON floating-point ambiguity."""

        return canonical_json_bytes(self.wire_document())

    def wire_document(self) -> dict[str, JsonValue]:
        """Render a fresh JSON value for the enclosing protocol frame."""

        return {
            "c_nmr_peaks": [
                {"delta (ppm)": peak.shift} for peak in self.carbon_peaks
            ],
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


def bind_chf_runner_input(model_input: ChfModelInput) -> ChfRunnerInput:
    """Convert the parser-owned CHF model into its one runner projection."""

    return ChfRunnerInput(
        molecular_formula=model_input.formula,
        proton_peaks=project_proton_peaks(model_input),
        carbon_peaks=tuple(
            ChfRunnerCarbonPeak(shift=canonical_decimal_text(peak.shift))
            for peak in model_input.carbon_peaks
        ),
    )


def materialize_chf_nmrpeak_document(
    runner_input: ChfRunnerInput,
) -> dict[str, object]:
    """Convert wire decimals to the numeric document consumed inside the runner."""

    return {
        "c_nmr_peaks": [
            {"delta (ppm)": float(peak.shift)} for peak in runner_input.carbon_peaks
        ],
        "h_nmr_peaks": proton_peak_documents(runner_input.proton_peaks),
        "molecular_formula": runner_input.molecular_formula,
    }


def parse_chf_runner_input(value: object) -> ChfRunnerInput:
    """Parse the exact private CHF payload before runner materialization."""

    document = _object_with_fields(
        value,
        {"c_nmr_peaks", "h_nmr_peaks", "molecular_formula"},
    )
    formula = document["molecular_formula"]
    if type(formula) is not str or not formula:
        raise ValueError("Cannot parse CHF runner input: formula must be text")

    proton_peaks = parse_runner_proton_peaks(document["h_nmr_peaks"])
    carbon_peaks = tuple(
        _parse_runner_carbon_peak(peak)
        for peak in _array(document["c_nmr_peaks"], "carbon peaks")
    )
    if not proton_peaks or not carbon_peaks:
        raise ValueError(
            "Cannot parse CHF runner input: both peak arrays must be non-empty"
        )
    return ChfRunnerInput(formula, proton_peaks, carbon_peaks)


def _parse_runner_carbon_peak(value: object) -> ChfRunnerCarbonPeak:
    peak = _object_with_fields(value, {"delta (ppm)"})
    return ChfRunnerCarbonPeak(
        canonical_decimal(peak["delta (ppm)"], "carbon shift")
    )


def _object_with_fields(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("Cannot parse CHF runner input: object fields are not exact")
    return value


def _array(value: object, name: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"Cannot parse CHF runner input: {name} must be an array")
    return value
