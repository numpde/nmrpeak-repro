"""The closed set of NMRPeak analyses offered by this provider."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnalysisOffering:
    """One product-local lane and the API analysis kind it serves."""

    implementation_ref: str
    analysis_kind_ref: str


@dataclass(frozen=True, slots=True)
class ProviderProduct:
    """The immutable analysis composition shipped by this provider."""

    offerings: tuple[AnalysisOffering, ...]


NMRPEAK_PRODUCT = ProviderProduct(
    offerings=(
        AnalysisOffering(
            implementation_ref="hf",
            analysis_kind_ref="mol_from_1h_peaks",
        ),
        AnalysisOffering(
            implementation_ref="chf",
            analysis_kind_ref="mol_from_1h_13c_formula",
        ),
    ),
)
