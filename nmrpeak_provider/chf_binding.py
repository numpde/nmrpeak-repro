"""Project admitted CHF science across the private runner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

from .canonical_json import JsonValue, canonical_json_bytes
from .product_input import ChfModelInput


_CANONICAL_DECIMAL = re.compile(r"(?:0|-?[1-9][0-9]*)(?:\.[0-9]*[1-9])?")


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


def parse_chf_runner_input(value: object) -> ChfRunnerInput:
    """Parse the exact private CHF payload before runner materialization."""

    document = _object_with_fields(
        value,
        {"c_nmr_peaks", "h_nmr_peaks", "molecular_formula"},
    )
    formula = document["molecular_formula"]
    if type(formula) is not str or not formula:
        raise ValueError("Cannot parse CHF runner input: formula must be text")

    proton_peaks = tuple(
        _parse_runner_proton_peak(peak)
        for peak in _array(document["h_nmr_peaks"], "proton peaks")
    )
    carbon_peaks = tuple(
        _parse_runner_carbon_peak(peak)
        for peak in _array(document["c_nmr_peaks"], "carbon peaks")
    )
    if not proton_peaks or not carbon_peaks:
        raise ValueError(
            "Cannot parse CHF runner input: both peak arrays must be non-empty"
        )
    return ChfRunnerInput(formula, proton_peaks, carbon_peaks)


def _parse_runner_proton_peak(value: object) -> ChfRunnerProtonPeak:
    peak = _object_with_fields(value, {"centroid", "nH", "category", "j_values"})
    centroid = _canonical_decimal(peak["centroid"], "proton centroid")
    n_h = peak["nH"]
    if type(n_h) is not int or n_h <= 0:
        raise ValueError("Cannot parse CHF runner input: nH must be positive")
    category = peak["category"]
    if type(category) is not str or not category:
        raise ValueError("Cannot parse CHF runner input: category must be text")
    j_values = peak["j_values"]
    if type(j_values) is not str or not _canonical_couplings(j_values):
        raise ValueError(
            "Cannot parse CHF runner input: j_values is not canonical"
        )
    return ChfRunnerProtonPeak(centroid, n_h, category, j_values)


def _parse_runner_carbon_peak(value: object) -> ChfRunnerCarbonPeak:
    peak = _object_with_fields(value, {"delta (ppm)"})
    return ChfRunnerCarbonPeak(
        _canonical_decimal(peak["delta (ppm)"], "carbon shift")
    )


def _object_with_fields(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("Cannot parse CHF runner input: object fields are not exact")
    return value


def _array(value: object, name: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"Cannot parse CHF runner input: {name} must be an array")
    return value


def _canonical_decimal(value: object, name: str) -> str:
    if type(value) is not str or _CANONICAL_DECIMAL.fullmatch(value) is None:
        raise ValueError(f"Cannot parse CHF runner input: {name} is not canonical")
    return value


def _canonical_couplings(value: str) -> bool:
    if value == "_":
        return True
    components = value.split("_")
    return (
        components[-1] == ""
        and all(
            component and _CANONICAL_DECIMAL.fullmatch(component) is not None
            for component in components[:-1]
        )
    )


def _coupling_text(couplings: tuple[Decimal, ...]) -> str:
    if not couplings:
        return "_"
    return "_".join(_decimal_text(coupling) for coupling in couplings) + "_"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text
