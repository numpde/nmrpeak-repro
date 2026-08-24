"""Project admitted CHF science across the private runner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .canonical_json import JsonValue, canonical_json_bytes
from .product_input import ChfModelInput


@dataclass(frozen=True, slots=True)
class ChfRunnerProtonPeak:
    """One proton observation in the runner's stable wire representation."""

    centroid: str
    n_h: int
    category: str
    j_values: str


@dataclass(frozen=True, slots=True)
class ChfRunnerCarbonPeak:
    """One carbon observation in the runner's stable wire representation."""

    shift: str


@dataclass(frozen=True, slots=True)
class ChfRunnerInput:
    """The immutable scientific payload sent to the CHF runner."""

    molecular_formula: str
    proton_peaks: tuple[ChfRunnerProtonPeak, ...]
    carbon_peaks: tuple[ChfRunnerCarbonPeak, ...]

    def canonical_bytes(self) -> bytes:
        """Render canonical wire bytes without JSON floating-point ambiguity."""

        return canonical_json_bytes(self._document())

    def _document(self) -> dict[str, JsonValue]:
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
        proton_peaks=tuple(
            ChfRunnerProtonPeak(
                centroid=_decimal_text(peak.centroid),
                n_h=peak.integral,
                category=peak.multiplicity,
                j_values=_coupling_text(peak.couplings_hz),
            )
            for peak in model_input.proton_peaks
        ),
        carbon_peaks=tuple(
            ChfRunnerCarbonPeak(shift=_decimal_text(peak.shift))
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
        "h_nmr_peaks": [
            {
                "category": peak.category,
                "centroid": float(peak.centroid),
                "j_values": peak.j_values,
                "nH": peak.n_h,
            }
            for peak in runner_input.proton_peaks
        ],
        "molecular_formula": runner_input.molecular_formula,
    }


def _coupling_text(couplings: tuple[Decimal, ...]) -> str:
    if not couplings:
        return "_"
    return "_".join(_decimal_text(coupling) for coupling in couplings) + "_"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text
