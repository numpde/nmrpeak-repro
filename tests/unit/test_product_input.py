"""Prove strict HF and CHF Job admission without exposing rejected values."""

from __future__ import annotations

from decimal import Decimal
import json
import unittest

from nmrpeak_provider.product import AnalysisOffering, NMRPEAK_PRODUCT
from nmrpeak_provider.product_input import (
    CarbonPeak,
    HfModelInput,
    InputRejected,
    InputRejectionReason,
    ProtonPeak,
    parse_job_input,
)


HF, CHF = NMRPEAK_PRODUCT.offerings


def document(
    *,
    formula: object = "C2H6O",
    proton_peaks: object | None = None,
    carbon_peaks: object | None = None,
) -> dict[str, object]:
    if proton_peaks is None:
        proton_peaks = [
            {
                "shift_lo": "1.20",
                "shift_hi": "1.30",
                "integral": "3",
                "multiplicity": "t",
                "j_hz": ["1.0", "7.1"],
            }
        ]
    spectra: dict[str, object] = {"1H": {"peaks": proton_peaks}}
    if carbon_peaks is not None:
        spectra["13C"] = {"peaks": carbon_peaks}
    return {
        "schema_id": "nmrpeak.structure_generation.request.v1",
        "model_input": {"formula": formula, "spectra": spectra},
    }


def encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class ProductInputTests(unittest.TestCase):
    def assert_rejected(
        self,
        raw: bytes,
        reason: InputRejectionReason,
        *,
        offering=HF,
    ) -> None:
        with self.assertRaises(InputRejected) as raised:
            parse_job_input(raw, offering)
        self.assertEqual(reason, raised.exception.reason)
        self.assertEqual(
            "The input could not be validated, so this Job did not run the analysis. "
            "Check the molecular formula and the peak lists required for this "
            "analysis. Because this Job is terminal, submit corrected input as a new "
            "Job.",
            str(raised.exception),
        )

    def test_hf_input_is_canonicalized_and_sorted_for_nmrpeak(self) -> None:
        proton_peaks = [
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

        parsed = parse_job_input(
            encoded(document(formula="O3H16C17N2", proton_peaks=proton_peaks)),
            HF,
        )

        self.assertEqual(
            HfModelInput(
                formula="C17H16N2O3",
                proton_peaks=(
                    ProtonPeak(Decimal("4.95"), 2, "m", ()),
                    ProtonPeak(
                        Decimal("1.25"),
                        3,
                        "t",
                        (Decimal("7.1"), Decimal("1.0")),
                    ),
                ),
            ),
            parsed,
        )

    def test_chf_retains_duplicate_carbon_observations_in_stable_order(self) -> None:
        parsed = parse_job_input(
            encoded(
                document(
                    carbon_peaks=[
                        {"shift": "70.4"},
                        {"shift": "109.4"},
                        {"shift": "109.4"},
                    ]
                )
            ),
            CHF,
        )

        self.assertEqual(
            (
                CarbonPeak(Decimal("109.4")),
                CarbonPeak(Decimal("109.4")),
                CarbonPeak(Decimal("70.4")),
            ),
            parsed.carbon_peaks,
        )

    def test_document_syntax_and_shape_are_closed(self) -> None:
        malformed_cases = (
            (b"\xff", InputRejectionReason.INVALID_JSON),
            (b'{"schema_id":NaN}', InputRejectionReason.INVALID_JSON),
            (b'{"schema_id":1.2}', InputRejectionReason.INVALID_JSON),
            (b'{"a":1,"a":2}', InputRejectionReason.DUPLICATE_FIELD),
            (encoded([]), InputRejectionReason.INVALID_STRUCTURE),
            (
                encoded({**document(), "extra": None}),
                InputRejectionReason.INVALID_STRUCTURE,
            ),
        )
        for raw, reason in malformed_cases:
            with self.subTest(reason=reason, raw=raw[:30]):
                self.assert_rejected(raw, reason)

    def test_deeply_nested_json_is_a_safe_rejection(self) -> None:
        raw = ("[" * 10_000 + "]" * 10_000).encode("ascii")
        self.assert_rejected(raw, InputRejectionReason.INVALID_JSON)

    def test_only_the_product_owned_offering_objects_select_a_lane(self) -> None:
        forged_hf = AnalysisOffering("hf", "mol_from_1h_peaks")
        with self.assertRaisesRegex(AssertionError, "product-owned offering"):
            parse_job_input(encoded(document()), forged_hf)

    def test_offering_owns_the_required_spectra(self) -> None:
        self.assert_rejected(
            encoded(document(carbon_peaks=[{"shift": "10.0"}])),
            InputRejectionReason.WRONG_SPECTRA,
        )
        self.assert_rejected(
            encoded(document()),
            InputRejectionReason.WRONG_SPECTRA,
            offering=CHF,
        )

    def test_formula_grammar_is_closed_without_narrowing_model_elements(self) -> None:
        chlorinated = parse_job_input(
            encoded(document(formula="O3ClH16C17N2")),
            HF,
        )
        self.assertEqual(chlorinated.formula, "C17H16ClN2O3")
        sodium_chloride = parse_job_input(
            encoded(document(formula="NaCl")),
            HF,
        )
        self.assertEqual(sodium_chloride.formula, "ClNa")
        hydrogen_chloride = parse_job_input(
            encoded(document(formula="ClH")),
            HF,
        )
        self.assertEqual(hydrogen_chloride.formula, "HCl")

        cases = (
            ("C2C3H6", InputRejectionReason.INVALID_FORMULA),
            ("C0H6", InputRejectionReason.INVALID_FORMULA),
            ("C101", InputRejectionReason.INVALID_FORMULA),
            ("C50H51", InputRejectionReason.INVALID_FORMULA),
            ("13C2H6", InputRejectionReason.INVALID_FORMULA),
            ("C2H6+", InputRejectionReason.INVALID_FORMULA),
            ("C2(H6)", InputRejectionReason.INVALID_FORMULA),
            ("C2H6.O", InputRejectionReason.INVALID_FORMULA),
        )
        for formula, reason in cases:
            with self.subTest(formula=formula):
                self.assert_rejected(encoded(document(formula=formula)), reason)

    def test_decimal_grammar_ranges_and_midpoint_are_closed(self) -> None:
        invalid_shifts = ("+1.0", "01.0", "1e0", "-0", "1.000", "16.00")
        for shift in invalid_shifts:
            with self.subTest(shift=shift):
                peak = {
                    "shift_lo": shift,
                    "shift_hi": shift,
                    "integral": "1",
                    "multiplicity": "s",
                    "j_hz": [],
                }
                expected = (
                    InputRejectionReason.DECIMAL_OUT_OF_RANGE
                    if shift == "16.00"
                    else InputRejectionReason.INVALID_STRUCTURE
                )
                self.assert_rejected(
                    encoded(document(proton_peaks=[peak])),
                    expected,
                )

        midpoint_peak = {
            "shift_lo": "1.00",
            "shift_hi": "1.01",
            "integral": "1",
            "multiplicity": "s",
            "j_hz": [],
        }
        self.assert_rejected(
            encoded(document(proton_peaks=[midpoint_peak])),
            InputRejectionReason.MIDPOINT_NOT_REPRESENTABLE,
        )

    def test_numeric_boundaries_are_explicit(self) -> None:
        boundary_protons = [
            {
                "shift_lo": "-1",
                "shift_hi": "-1",
                "integral": "1",
                "multiplicity": "s",
                "j_hz": ["0.1", "299.9"],
            },
            {
                "shift_lo": "15.99",
                "shift_hi": "15.99",
                "integral": "50",
                "multiplicity": "AA'BB'",
                "j_hz": [],
            },
        ]
        parsed = parse_job_input(
            encoded(
                document(
                    formula="C97HNO",
                    proton_peaks=boundary_protons,
                    carbon_peaks=[{"shift": "-6"}, {"shift": "249.9"}],
                )
            ),
            CHF,
        )
        self.assertEqual("C97HNO", parsed.formula)

        for carbon_shift in ("250", "300.0"):
            with self.subTest(carbon_shift=carbon_shift):
                self.assert_rejected(
                    encoded(document(carbon_peaks=[{"shift": carbon_shift}])),
                    InputRejectionReason.DECIMAL_OUT_OF_RANGE,
                    offering=CHF,
                )

    def test_reversed_and_non_string_measurements_are_rejected(self) -> None:
        base_peak = {
            "shift_lo": "1.0",
            "shift_hi": "1.0",
            "integral": "1",
            "multiplicity": "s",
            "j_hz": [],
        }
        cases = (
            (
                {**base_peak, "shift_lo": "2.0"},
                InputRejectionReason.DECIMAL_OUT_OF_RANGE,
            ),
            ({**base_peak, "shift_lo": 1}, InputRejectionReason.INVALID_STRUCTURE),
            ({**base_peak, "shift_lo": True}, InputRejectionReason.INVALID_STRUCTURE),
            ({**base_peak, "integral": 1}, InputRejectionReason.INVALID_STRUCTURE),
        )
        for peak, reason in cases:
            with self.subTest(reason=reason):
                self.assert_rejected(
                    encoded(document(proton_peaks=[peak])),
                    reason,
                )

    def test_integral_multiplicity_and_couplings_are_bounded(self) -> None:
        base_peak = {
            "shift_lo": "1.0",
            "shift_hi": "1.0",
            "integral": "1",
            "multiplicity": "s",
            "j_hz": [],
        }
        cases = (
            ({**base_peak, "integral": "0"}, InputRejectionReason.INVALID_STRUCTURE),
            ({**base_peak, "integral": "51"}, InputRejectionReason.INVALID_STRUCTURE),
            (
                {**base_peak, "multiplicity": "S"},
                InputRejectionReason.UNSUPPORTED_MULTIPLICITY,
            ),
            (
                {**base_peak, "j_hz": ["1.0"] * 9},
                InputRejectionReason.TOO_MANY_COUPLINGS,
            ),
            (
                {**base_peak, "j_hz": ["0.0"]},
                InputRejectionReason.DECIMAL_OUT_OF_RANGE,
            ),
        )
        for peak, reason in cases:
            with self.subTest(reason=reason):
                self.assert_rejected(
                    encoded(document(proton_peaks=[peak])),
                    reason,
                )

    def test_peak_counts_are_bounded(self) -> None:
        proton = {
            "shift_lo": "1.0",
            "shift_hi": "1.0",
            "integral": "1",
            "multiplicity": "s",
            "j_hz": ["1.0"] * 8,
        }
        carbon = {"shift": "1.0"}
        accepted = document(
            proton_peaks=[proton] * 32,
            carbon_peaks=[carbon] * 64,
        )
        parse_job_input(encoded(accepted), CHF)

        self.assert_rejected(
            encoded(document(proton_peaks=[proton] * 33)),
            InputRejectionReason.TOO_MANY_PEAKS,
        )
        self.assert_rejected(
            encoded(
                document(
                    proton_peaks=[proton],
                    carbon_peaks=[carbon] * 65,
                )
            ),
            InputRejectionReason.TOO_MANY_PEAKS,
            offering=CHF,
        )

    def test_empty_spectra_and_trailing_json_are_rejected(self) -> None:
        self.assert_rejected(
            encoded(document(proton_peaks=[])),
            InputRejectionReason.INVALID_STRUCTURE,
        )
        self.assert_rejected(
            encoded(document(proton_peaks=[], carbon_peaks=[])),
            InputRejectionReason.INVALID_STRUCTURE,
            offering=CHF,
        )
        self.assert_rejected(
            encoded(document()) + b"{}",
            InputRejectionReason.INVALID_JSON,
        )

    def test_oversized_document_is_rejected_before_json_decoding(self) -> None:
        self.assert_rejected(
            b"secret" * 11_000,
            InputRejectionReason.DOCUMENT_TOO_LARGE,
        )

    def test_rejection_does_not_disclose_job_values(self) -> None:
        secret_formula = "C2H6Secret"
        with self.assertRaises(InputRejected) as raised:
            parse_job_input(encoded(document(formula=secret_formula)), HF)

        self.assertNotIn(secret_formula, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
