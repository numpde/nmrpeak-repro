"""Prove CHF tokenizer admission remains separate from model execution."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from families.nmrpeak.runner_runtime import NmrpeakRuntimeInputRejected
from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
)
from nmrpeak_provider.nmrpeak_binding import RunnerProtonPeak


_RUNTIME_PATH = (
    Path(__file__).resolve().parents[2]
    / "models/nmrpeak_chf_v1/runner/runtime.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "models.nmrpeak_chf_v1.runner.runtime",
    _RUNTIME_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
runtime_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime_module)

MODEL_INPUT = ChfRunnerInput(
    "C2H6O",
    (RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
    (
        ChfRunnerCarbonPeak("70.4"),
        ChfRunnerCarbonPeak("70.4"),
    ),
)


class ChfRuntimeTests(unittest.TestCase):
    def test_validation_admits_511_tokens_without_executing_the_model(self) -> None:
        stack = RecordingStack(tokens=("token",) * 511)
        runtime = runtime_module.NmrpeakChfRuntime(stack)

        runtime.validate(MODEL_INPUT)

        self.assertEqual(len(stack.documents), 1)
        self.assertEqual(stack.generated, [])

    def test_validation_rejects_empty_and_512_token_inputs(self) -> None:
        for token_count in (0, 512):
            with self.subTest(token_count=token_count):
                runtime = runtime_module.NmrpeakChfRuntime(
                    RecordingStack(tokens=("token",) * token_count)
                )
                with self.assertRaises(NmrpeakRuntimeInputRejected):
                    runtime.validate(MODEL_INPUT)


class RecordingStack:
    def __init__(
        self,
        *,
        tokens: tuple[str, ...],
        candidates: list[str] | None = None,
    ) -> None:
        self.tokens = tokens
        self.candidates = ["CCO"] if candidates is None else candidates
        self.documents: list[dict[str, object]] = []
        self.generated: list[tuple[str, ...]] = []

    def tokenize(self, document: dict[str, object]) -> tuple[str, ...]:
        self.documents.append(document)
        return self.tokens

    def generate(self, tokens: tuple[str, ...]) -> list[str]:
        self.generated.append(tokens)
        return self.candidates


if __name__ == "__main__":
    unittest.main()
