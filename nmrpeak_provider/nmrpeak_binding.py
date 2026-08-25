"""Scientific values shared by the fixed NMRPeak runner projections."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

from .product_input import NmrpeakModelInput


_CANONICAL_DECIMAL = re.compile(r"(?:0|-?[1-9][0-9]*)(?:\.[0-9]*[1-9])?")


@dataclass(frozen=True, slots=True)
class RunnerProtonPeak:
    """One proton observation in NMRPeak's stable wire representation."""

    centroid: str
    n_h: int
    category: str
    j_values: str


def project_proton_peaks(
    model_input: NmrpeakModelInput,
) -> tuple[RunnerProtonPeak, ...]:
    """Project admitted proton values without lane-specific reinterpretation."""

    return tuple(
        RunnerProtonPeak(
            centroid=canonical_decimal_text(peak.centroid),
            n_h=peak.integral,
            category=peak.multiplicity,
            j_values=_coupling_text(peak.couplings_hz),
        )
        for peak in model_input.proton_peaks
    )


def proton_peak_documents(
    peaks: tuple[RunnerProtonPeak, ...],
) -> list[dict[str, object]]:
    """Materialize the exact proton objects consumed by NMRPeak."""

    return [
        {
            "category": peak.category,
            "centroid": float(peak.centroid),
            "j_values": peak.j_values,
            "nH": peak.n_h,
        }
        for peak in peaks
    ]


def parse_runner_proton_peaks(value: object) -> tuple[RunnerProtonPeak, ...]:
    """Parse the complete private proton array before model materialization."""

    if type(value) is not list:
        raise ValueError("NMRPeak runner proton peaks must be an array")
    peaks = tuple(_parse_runner_proton_peak(peak) for peak in value)
    if not peaks:
        raise ValueError("NMRPeak runner proton peaks must not be empty")
    return peaks


def canonical_decimal(value: object, name: str) -> str:
    """Admit one normalized non-exponent decimal from a private frame."""

    if type(value) is not str or _CANONICAL_DECIMAL.fullmatch(value) is None:
        raise ValueError(f"NMRPeak runner {name} is not a canonical decimal")
    return value


def canonical_decimal_text(value: Decimal) -> str:
    """Render an admitted decimal in the one private wire representation."""

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _parse_runner_proton_peak(value: object) -> RunnerProtonPeak:
    fields = {"centroid", "nH", "category", "j_values"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("NMRPeak runner proton peak fields are not exact")
    centroid = canonical_decimal(value["centroid"], "proton centroid")
    n_h = value["nH"]
    if type(n_h) is not int or n_h <= 0:
        raise ValueError("NMRPeak runner proton nH must be positive")
    category = value["category"]
    if type(category) is not str or not category:
        raise ValueError("NMRPeak runner proton category must be text")
    j_values = value["j_values"]
    if type(j_values) is not str or not _canonical_couplings(j_values):
        raise ValueError("NMRPeak runner proton j_values is not canonical")
    return RunnerProtonPeak(centroid, n_h, category, j_values)


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
    return "_".join(canonical_decimal_text(value) for value in couplings) + "_"
