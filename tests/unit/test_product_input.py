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
        self.assertEqual(reason.value, str(raised.exception))

    def test_hf_input_preserves_formula_and_sorts_peaks_for_nmrpeak(self) -> None:
        proton_peaks = [
            {
                "shift_lo": "1.20",
                "shift_hi": "1.30",
                "integral": "3",
                "multiplicity": "t",
                "j_hz": ["1.0", "7.1"],
            },
            {
                "shift_lo": "4.99",
                "shift_hi": "4.91",
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
                formula="O3H16C17N2",
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

    def test_invalid_json_retains_the_decoder_failure(self) -> None:
        with self.assertRaises(InputRejected) as raised:
            parse_job_input(b"\xff", HF)

        self.assertIs(raised.exception.reason, InputRejectionReason.INVALID_JSON)
        self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)

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

    def test_formula_text_is_preserved_for_runner_tokenization(self) -> None:
        for formula in (
            "O3ClH16C17N2",
            "NaCl",
            "ClH",
            "C2C3H6",
            "C0H6",
            "C101",
            "C50H51",
            "C2H6+",
            "C2H6-2",
        ):
            with self.subTest(formula=formula):
                parsed = parse_job_input(encoded(document(formula=formula)), HF)
                self.assertEqual(parsed.formula, formula)

        for formula in ("13C2H6", "C2(H6)", "C2H6.O", ""):
            with self.subTest(formula=formula):
                self.assert_rejected(
                    encoded(document(formula=formula)),
                    InputRejectionReason.INVALID_FORMULA,
                )

    def test_finite_decimal_values_survive_product_parsing(self) -> None:
        parsed = parse_job_input(
            encoded(
                document(
                    formula="C2H6O+",
                    proton_peaks=[
                        {
                            "shift_lo": "3.71",
                            "shift_hi": "3.68",
                            "integral": "1",
                            "multiplicity": "s",
                            "j_hz": ["0.0", "300.25", "1e1"],
                        }
                    ],
                    carbon_peaks=[{"shift": "250"}, {"shift": "-7.25"}],
                )
            ),
            CHF,
        )

        self.assertEqual(parsed.formula, "C2H6O+")
        self.assertEqual(parsed.proton_peaks[0].centroid, Decimal("3.695"))
        self.assertEqual(
            parsed.proton_peaks[0].couplings_hz,
            (Decimal("300.25"), Decimal("1e1"), Decimal("0.0")),
        )
        self.assertEqual(
            parsed.carbon_peaks,
            (
                CarbonPeak(Decimal("250")),
                CarbonPeak(Decimal("-7.25")),
            ),
        )

    def test_non_string_measurements_are_rejected(self) -> None:
        base_peak = {
            "shift_lo": "1.0",
            "shift_hi": "1.0",
            "integral": "1",
            "multiplicity": "s",
            "j_hz": [],
        }
        cases = (
            ({**base_peak, "shift_lo": 1}, InputRejectionReason.INVALID_STRUCTURE),
            ({**base_peak, "shift_lo": True}, InputRejectionReason.INVALID_STRUCTURE),
            (
                {**base_peak, "shift_lo": "NaN"},
                InputRejectionReason.INVALID_STRUCTURE,
            ),
            (
                {**base_peak, "shift_lo": "Infinity"},
                InputRejectionReason.INVALID_STRUCTURE,
            ),
            ({**base_peak, "integral": 1}, InputRejectionReason.INVALID_STRUCTURE),
        )
        for peak, reason in cases:
            with self.subTest(reason=reason):
                self.assert_rejected(
                    encoded(document(proton_peaks=[peak])),
                    reason,
                )

    def test_integral_and_multiplicity_follow_the_model_vocabulary(self) -> None:
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
        )
        for peak, reason in cases:
            with self.subTest(reason=reason):
                self.assert_rejected(
                    encoded(document(proton_peaks=[peak])),
                    reason,
                )

    def test_negative_coupling_is_rejected_before_nmrpeak_can_change_its_sign(
        self,
    ) -> None:
        peak = {
            "shift_lo": "1.0",
            "shift_hi": "1.0",
            "integral": "1",
            "multiplicity": "s",
            "j_hz": ["-7.1"],
        }

        self.assert_rejected(
            encoded(document(proton_peaks=[peak])),
            InputRejectionReason.COUPLING_MUST_BE_NONNEGATIVE,
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
