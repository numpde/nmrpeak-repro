"""Prove HF tokenizer admission remains separate from model execution."""

from __future__ import annotations

from families.nmrpeak.runner_runtime import NmrpeakRuntimeInputRejected, TokenizerMode
from models.nmrpeak_hf_v1.runner.runtime import HF_TOKENIZER_MODE, NmrpeakHfRuntime
from nmrpeak_provider.hf_binding import HfRunnerInput
from nmrpeak_provider.nmrpeak_binding import RunnerProtonPeak
import unittest


MODEL_INPUT = HfRunnerInput(
    "C2H6O",
    (RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
)


class HfRuntimeTests(unittest.TestCase):
    def test_validation_admits_511_tokens_without_executing_the_model(self) -> None:
        stack = RecordingStack(tokens=("token",) * 511)
        runtime = NmrpeakHfRuntime(stack)

        runtime.validate(MODEL_INPUT)

        self.assertEqual(
            stack.documents,
            [
                {
                    "h_nmr_peaks": [
                        {
                            "category": "t",
                            "centroid": 1.25,
                            "j_values": "7.1_",
                            "nH": 3,
                        }
                    ],
                    "molecular_formula": "C2H6O",
                }
            ],
        )
        self.assertEqual(stack.generated, [])

    def test_validation_rejects_empty_and_512_token_inputs(self) -> None:
        for token_count in (0, 512):
            with self.subTest(token_count=token_count):
                runtime = NmrpeakHfRuntime(
                    RecordingStack(tokens=("token",) * token_count)
                )
                with self.assertRaises(NmrpeakRuntimeInputRejected):
                    runtime.validate(MODEL_INPUT)

    def test_hf_owns_the_pinned_proton_and_formula_tokenizer_mode(self) -> None:
        self.assertEqual(
            TokenizerMode(
                use_cnmr=False,
                use_hnmr=True,
                use_h_nh=True,
                use_h_jvalue=True,
                use_h_category=True,
                use_formula=True,
            ),
            HF_TOKENIZER_MODE,
        )


class RecordingStack:
    def __init__(self, *, tokens: tuple[str, ...]) -> None:
        self.tokens = tokens
        self.documents: list[dict[str, object]] = []
        self.generated: list[tuple[str, ...]] = []

    def tokenize(self, document: dict[str, object]) -> tuple[str, ...]:
        self.documents.append(document)
        return self.tokens

    def generate(self, tokens: tuple[str, ...]) -> list[str]:
        self.generated.append(tokens)
        return ["CCO"]


if __name__ == "__main__":
    unittest.main()
