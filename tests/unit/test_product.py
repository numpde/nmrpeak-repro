"""Prove the provider product is exactly the reviewed HF and CHF composition."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from nmrpeak_provider.product import (
    AnalysisOffering,
    NMRPEAK_PRODUCT,
)


class ProviderProductTests(unittest.TestCase):
    def test_product_contains_only_the_two_reviewed_offerings(self) -> None:
        self.assertEqual(
            (
                AnalysisOffering(
                    implementation_ref="hf",
                    analysis_kind_ref="mol_from_1h_peaks",
                ),
                AnalysisOffering(
                    implementation_ref="chf",
                    analysis_kind_ref="mol_from_1h_13c_formula",
                ),
            ),
            NMRPEAK_PRODUCT.offerings,
        )

    def test_product_and_offerings_are_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            NMRPEAK_PRODUCT.offerings = ()
        with self.assertRaises(FrozenInstanceError):
            NMRPEAK_PRODUCT.offerings[0].analysis_kind_ref = "replacement"


if __name__ == "__main__":
    unittest.main()
