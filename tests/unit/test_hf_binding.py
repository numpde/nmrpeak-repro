"""Prove the complete HF scientific projection at its owning seam."""

from __future__ import annotations

import json
import unittest

from nmrpeak_provider.hf_binding import (
    bind_hf_runner_input,
    materialize_hf_nmrpeak_document,
    parse_hf_runner_input,
)
from nmrpeak_provider.product import HF_OFFERING
from nmrpeak_provider.product_input import HfModelInput, parse_job_input


class HfBindingTests(unittest.TestCase):
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
                                    "shift_lo": "1.20",
                                    "shift_hi": "1.30",
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
                        }
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

        admitted = parse_job_input(raw, HF_OFFERING)
        if not isinstance(admitted, HfModelInput):
            self.fail("The HF offering did not produce an HF model input")

        runner_input = bind_hf_runner_input(admitted)

        self.assertEqual(
            b'{"h_nmr_peaks":[{"category":"m","centroid":"4.95","j_values":"_","nH":2},{"category":"t","centroid":"1.25","j_values":"7.1_1_","nH":3}],"molecular_formula":"C17H16N2O3"}',
            runner_input.canonical_bytes(),
        )
        self.assertEqual(
            {
                "h_nmr_peaks": [
                    {
                        "category": "m",
                        "centroid": 4.95,
                        "j_values": "_",
                        "nH": 2,
                    },
                    {
                        "category": "t",
                        "centroid": 1.25,
                        "j_values": "7.1_1_",
                        "nH": 3,
                    },
                ],
                "molecular_formula": "C17H16N2O3",
            },
            materialize_hf_nmrpeak_document(runner_input),
        )
        self.assertEqual(runner_input, parse_hf_runner_input(runner_input.wire_document()))

    def test_private_input_rejects_carbon_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "object fields are not exact"):
            parse_hf_runner_input(
                {
                    "c_nmr_peaks": [],
                    "h_nmr_peaks": [],
                    "molecular_formula": "CH4",
                }
            )


if __name__ == "__main__":
    unittest.main()
