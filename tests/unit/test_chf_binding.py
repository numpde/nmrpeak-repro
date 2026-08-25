"""Prove the complete CHF scientific projection at its owning seam."""

from __future__ import annotations

import json
import unittest

from nmrpeak_provider.chf_binding import (
    bind_chf_runner_input,
    materialize_chf_nmrpeak_document,
)
from nmrpeak_provider.product import NMRPEAK_PRODUCT
from nmrpeak_provider.product_input import ChfModelInput, parse_job_input


CHF = NMRPEAK_PRODUCT.offerings[1]


class ChfBindingTests(unittest.TestCase):
    def test_admitted_input_has_one_exact_runner_projection(self) -> None:
        raw = json.dumps(
            {
                "schema_id": "nmrpeak.structure_generation.request.v1",
                "model_input": {
                    "formula": "O3H16C17N2",
                    "spectra": {
                        "1H": {
                            "peaks": [
                                {
                                    "shift_lo": "3.71",
                                    "shift_hi": "3.68",
                                    "integral": "3",
                                    "multiplicity": "t",
                                    "j_hz": ["1.0", "7.1"],
                                },
                                {
                                    "shift_lo": "4.91",
                                    "shift_hi": "4.99",
                                    "integral": "2",
                                    "multiplicity": "m",
                                    "j_hz": [],
                                },
                            ]
                        },
                        "13C": {
                            "peaks": [
                                {"shift": "70.0"},
                                {"shift": "109.4"},
                                {"shift": "109.4"},
                            ]
                        },
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

        admitted = parse_job_input(raw, CHF)
        if not isinstance(admitted, ChfModelInput):
            self.fail("The CHF offering did not produce a CHF model input")

        runner_input = bind_chf_runner_input(admitted)

        self.assertEqual(
            b'{"c_nmr_peaks":[{"delta (ppm)":"109.4"},{"delta (ppm)":"109.4"},{"delta (ppm)":"70"}],"h_nmr_peaks":[{"category":"m","centroid":"4.95","j_values":"_","nH":2},{"category":"t","centroid":"3.695","j_values":"7.1_1_","nH":3}],"molecular_formula":"O3H16C17N2"}',
            runner_input.canonical_bytes(),
        )
        self.assertEqual(
            {
                "c_nmr_peaks": [
                    {"delta (ppm)": 109.4},
                    {"delta (ppm)": 109.4},
                    {"delta (ppm)": 70.0},
                ],
                "h_nmr_peaks": [
                    {
                        "category": "m",
                        "centroid": 4.95,
                        "j_values": "_",
                        "nH": 2,
                    },
                    {
                        "category": "t",
                        "centroid": 3.695,
                        "j_values": "7.1_1_",
                        "nH": 3,
                    },
                ],
                "molecular_formula": "O3H16C17N2",
            },
            materialize_chf_nmrpeak_document(runner_input),
        )


if __name__ == "__main__":
    unittest.main()
