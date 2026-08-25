"""Prove the one model stack shared by the fixed NMRPeak runner lanes."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from families.nmrpeak.runner_runtime import (
    LoadedNmrpeakStack,
    TokenizerMode,
    load_nmrpeak_runtime,
)
from nmrpeak_provider.product_decode import HF_DECODE_POLICY


class NmrpeakRunnerRuntimeTests(unittest.TestCase):
    def test_loaded_stack_builds_the_pinned_sample_and_decode_call(self) -> None:
        torch = RecordingTensorOperations()
        data_utils = ModuleType("unicore.data.data_utils")
        data_utils.collate_tokens = RecordingCollate()
        unicore_data = ModuleType("unicore.data")
        unicore_data.data_utils = data_utils
        model = RecordingModel()
        stack = LoadedNmrpeakStack(
            object(),
            RecordingDictionary(),
            model,
            HF_DECODE_POLICY,
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
            model.generate_calls,
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

    def test_loader_uses_the_fixed_family_model_and_selected_tokenizer_mode(self) -> None:
        modules = FakeLoadModules()
        checkpoint = BytesIO(b"checkpoint")
        mode = TokenizerMode(False, True, True, True, True, True)
        materialize = lambda value: {"input": value}

        with patch.dict(sys.modules, modules.modules, clear=False):
            load_nmrpeak_runtime(
                checkpoint,
                tokenizer_mode=mode,
                decode_policy=HF_DECODE_POLICY,
                materialize=materialize,
                dictionary_path=Path("/dictionary"),
                bart_config_path=Path("/bart.json"),
            )

        self.assertEqual(modules.bart_paths, ["/bart.json"])
        self.assertEqual(modules.dictionary_paths, ["/dictionary", "/dictionary"])
        self.assertEqual(
            modules.model_arguments,
            [
                {
                    "arch": "spec_trans_mol_bart_base",
                    "configuration": modules.configuration,
                    "seed": 1,
                    "share_embedding": True,
                    "shift_length": 2795,
                }
            ],
        )
        self.assertEqual(
            modules.torch_loads,
            [(checkpoint, {"map_location": "cpu", "weights_only": False})],
        )
        self.assertEqual(modules.model.load_calls, [(modules.model_state, True)])
        self.assertEqual(modules.model.cpu_calls, 1)
        self.assertEqual(modules.model.eval_calls, 1)
        self.assertEqual(
            modules.tokenizer_calls,
            [
                (
                    ((),),
                    {
                        "use_cnmr": False,
                        "use_hnmr": True,
                        "use_h_nh": True,
                        "use_h_jvalue": True,
                        "use_h_category": True,
                        "use_range_shift": False,
                        "use_formula": True,
                        "use_spec_end_token": True,
                    },
                )
            ],
        )
        self.assertEqual(
            [dictionary.symbols for dictionary in modules.dictionaries],
            [[("[MASK]", True)], [("[MASK]", True)]],
        )


class FakeTensor:
    def __init__(self, values: tuple[int, ...]) -> None:
        self.values = values

    def long(self) -> FakeTensor:
        return self


class RecordingTensorOperations(ModuleType):
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
        self.generate_calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def generate_(self, sample: dict[str, object], **options: object) -> object:
        self.generate_calls.append((sample, options))
        return [[["C", "C"], ["O"], ["C", "C"]]], None, None


class FakeDictionary:
    def __init__(self) -> None:
        self.symbols: list[tuple[str, bool]] = []

    def add_symbol(self, symbol: str, *, is_special: bool) -> None:
        self.symbols.append((symbol, is_special))


class FakeLoadedModel:
    def __init__(self) -> None:
        self.load_calls: list[tuple[object, bool]] = []
        self.cpu_calls = 0
        self.eval_calls = 0

    def load_state_dict(self, state: object, *, strict: bool) -> None:
        self.load_calls.append((state, strict))

    def cpu(self) -> None:
        self.cpu_calls += 1

    def eval(self) -> None:
        self.eval_calls += 1

    def parameters(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(device=SimpleNamespace(type="cpu")),)

    def buffers(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(device=SimpleNamespace(type="cpu")),)


class FakeLoadModules:
    def __init__(self) -> None:
        self.configuration = object()
        self.model_state = object()
        self.model = FakeLoadedModel()
        self.bart_paths: list[str] = []
        self.dictionary_paths: list[str] = []
        self.dictionaries: list[FakeDictionary] = []
        self.model_arguments: list[dict[str, object]] = []
        self.torch_loads: list[tuple[object, dict[str, object]]] = []
        self.tokenizer_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        torch = ModuleType("torch")
        torch.load = self._torch_load
        nmrpeak = ModuleType("nmrpeak")
        nmrpeak.__path__ = []
        nmrpeak_data = ModuleType("nmrpeak.data")
        nmrpeak_data.TokenizedNmrDataset = self._tokenizer
        nmrpeak_models = ModuleType("nmrpeak.models")
        nmrpeak_models.__path__ = []
        spec_trans_mol = ModuleType("nmrpeak.models.spec_trans_mol")
        spec_trans_mol.SpecTransMolModel = self._model
        transformers = ModuleType("transformers")
        transformers.BartConfig = SimpleNamespace(from_json_file=self._bart_config)
        unicore = ModuleType("unicore")
        unicore.__path__ = []
        unicore_data = ModuleType("unicore.data")
        unicore_data.Dictionary = SimpleNamespace(load=self._dictionary)
        self.modules = {
            "torch": torch,
            "nmrpeak": nmrpeak,
            "nmrpeak.data": nmrpeak_data,
            "nmrpeak.models": nmrpeak_models,
            "nmrpeak.models.spec_trans_mol": spec_trans_mol,
            "transformers": transformers,
            "unicore": unicore,
            "unicore.data": unicore_data,
        }

    def _bart_config(self, path: str) -> object:
        self.bart_paths.append(path)
        return self.configuration

    def _dictionary(self, path: str) -> FakeDictionary:
        self.dictionary_paths.append(path)
        dictionary = FakeDictionary()
        self.dictionaries.append(dictionary)
        return dictionary

    def _model(
        self,
        arguments: object,
        spec_dictionary: object,
        mol_dictionary: object,
    ) -> FakeLoadedModel:
        self.model_arguments.append(vars(arguments))
        self.model_dictionaries = (spec_dictionary, mol_dictionary)
        return self.model

    def _torch_load(self, checkpoint: object, **options: object) -> dict[str, object]:
        self.torch_loads.append((checkpoint, options))
        return {"model": self.model_state}

    def _tokenizer(self, *arguments: object, **options: object) -> object:
        self.tokenizer_calls.append((arguments, options))
        return object()


if __name__ == "__main__":
    unittest.main()
