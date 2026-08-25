"""Prove CHF tokenizer admission remains separate from model execution."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
    ChfRunnerProtonPeak,
)


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
    (ChfRunnerProtonPeak("1.25", 3, "t", "7.1_"),),
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
                with self.assertRaises(runtime_module.ChfRuntimeInputRejected):
                    runtime.validate(MODEL_INPUT)

    def test_loaded_stack_builds_the_pinned_sample_and_decode_call(self) -> None:
        torch = RecordingTorch()
        data_utils = ModuleType("unicore.data.data_utils")
        data_utils.collate_tokens = RecordingCollate()
        unicore_data = ModuleType("unicore.data")
        unicore_data.data_utils = data_utils
        model = RecordingModel()
        stack = runtime_module._LoadedNmrpeakChfStack(
            object(),
            RecordingDictionary(),
            model,
        )

        with patch.dict(
            sys.modules,
            {"torch": torch, "unicore.data": unicore_data},
        ):
            generated = stack.generate(("one", "two"))

        self.assertEqual(generated, ["CC", "O", "CC"])
        self.assertEqual(torch.from_numpy_values, [(11, 12)])
        self.assertEqual(torch.concatenated, [(1,), (11, 12), (2,)])
        self.assertEqual(
            data_utils.collate_tokens.calls,
            [(((1, 11, 12, 2),), 0, False, 8)],
        )
        self.assertEqual(
            model.calls,
            [
                (
                    {"spec": {"src_tokens": "padded-source"}},
                    {
                        "max_len": 160,
                        "beam_size": 10,
                        "temperature": 3.0,
                        "use_beam_search": True,
                    },
                )
            ],
        )


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


class FakeTensor:
    def __init__(self, values: tuple[int, ...]) -> None:
        self.values = values

    def long(self) -> FakeTensor:
        return self


class RecordingTorch(ModuleType):
    def __init__(self) -> None:
        super().__init__("torch")
        self.from_numpy_values: list[tuple[int, ...]] = []
        self.concatenated: list[tuple[int, ...]] = []

    def from_numpy(self, values: tuple[int, ...]) -> FakeTensor:
        self.from_numpy_values.append(values)
        return FakeTensor(values)

    def tensor(self, values: list[int]) -> FakeTensor:
        return FakeTensor(tuple(values))

    def cat(self, tensors: tuple[FakeTensor, ...]) -> FakeTensor:
        self.concatenated = [tensor.values for tensor in tensors]
        return FakeTensor(tuple(value for tensor in tensors for value in tensor.values))


class RecordingCollate:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[tuple[int, ...], ...], int, bool, int]] = []

    def __call__(
        self,
        tensors: list[FakeTensor],
        pad: int,
        *,
        left_pad: bool,
        pad_to_multiple: int,
    ) -> str:
        self.calls.append(
            (tuple(tensor.values for tensor in tensors), pad, left_pad, pad_to_multiple)
        )
        return "padded-source"


class RecordingDictionary:
    def vec_index(self, tokens: tuple[str, ...]) -> tuple[int, ...]:
        self.tokens = tokens
        return (11, 12)

    def bos(self) -> int:
        return 1

    def eos(self) -> int:
        return 2

    def pad(self) -> int:
        return 0


class RecordingModel:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def generate_(self, sample: dict[str, object], **options: object) -> object:
        self.calls.append((sample, options))
        return [[["C", "C"], ["O"], ["C", "C"]]], None, None


if __name__ == "__main__":
    unittest.main()
