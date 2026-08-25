"""Load and run the pinned NMRPeak CHF generation component on CPU."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import BinaryIO, Protocol

from nmrpeak_provider.canonical_json import JsonValue
from nmrpeak_provider.chf_binding import (
    ChfRunnerInput,
    materialize_chf_nmrpeak_document,
)
from nmrpeak_provider.product_decode import CHF_DECODE_POLICY


DICTIONARY_PATH = Path("/opt/nmrpeak/dict/bart/total_dict.txt")
BART_CONFIG_PATH = Path("/opt/nmrpeak/bart-base/config.json")
MAXIMUM_TOKENIZED_INPUT_LENGTH = 511


class ChfRuntimeInputRejected(ValueError):
    """The loaded tokenizer deterministically rejects one complete input."""


class _ChfInferenceStack(Protocol):
    """The pinned tokenizer and loaded model behavior used by one runtime."""

    def tokenize(self, document: dict[str, object]) -> tuple[str, ...]: ...

    def generate(self, tokens: tuple[str, ...]) -> JsonValue: ...


class NmrpeakChfRuntime:
    """Validate with the real tokenizer before entering model execution."""

    def __init__(self, stack: _ChfInferenceStack) -> None:
        self._stack = stack

    def validate(self, model_input: ChfRunnerInput) -> None:
        tokens = self._tokenize(model_input)
        if not tokens or len(tokens) > MAXIMUM_TOKENIZED_INPUT_LENGTH:
            raise ChfRuntimeInputRejected()

    def generate(self, model_input: ChfRunnerInput) -> JsonValue:
        return self._stack.generate(self._tokenize(model_input))

    def _tokenize(self, model_input: ChfRunnerInput) -> tuple[str, ...]:
        document = materialize_chf_nmrpeak_document(model_input)
        return self._stack.tokenize(document)


class _LoadedNmrpeakChfStack:
    """Own tokenizer and model calls for the protocol's one-input request."""

    def __init__(self, tokenizer: object, dictionary: object, model: object) -> None:
        self._tokenizer = tokenizer
        self._dictionary = dictionary
        self._model = model

    def tokenize(self, document: dict[str, object]) -> tuple[str, ...]:
        tokens = self._tokenizer.tokenize_item(document)
        return tuple(str(token) for token in tokens)

    def generate(self, tokens: tuple[str, ...]) -> JsonValue:
        import torch
        from unicore.data import data_utils

        token_ids = torch.from_numpy(self._dictionary.vec_index(tokens)).long()
        source = torch.cat(
            (
                torch.tensor([self._dictionary.bos()]),
                token_ids,
                torch.tensor([self._dictionary.eos()]),
            )
        )
        sample = {
            "spec": {
                "src_tokens": data_utils.collate_tokens(
                    [source],
                    self._dictionary.pad(),
                    left_pad=False,
                    pad_to_multiple=8,
                )
            }
        }
        generated, _scores, _targets = self._model.generate_(
            sample,
            max_len=CHF_DECODE_POLICY.maximum_generated_tokens,
            beam_size=CHF_DECODE_POLICY.beam_size,
            temperature=float(CHF_DECODE_POLICY.temperature),
            use_beam_search=True,
        )
        return ["".join(sequence) for sequence in generated[0]]


def load_nmrpeak_chf_runtime(
    checkpoint: BinaryIO,
    *,
    dictionary_path: Path = DICTIONARY_PATH,
    bart_config_path: Path = BART_CONFIG_PATH,
) -> NmrpeakChfRuntime:
    """Deserialize one verified checkpoint into the fixed CPU component."""

    import torch
    from nmrpeak.data import TokenizedNmrDataset
    from nmrpeak.models.spec_trans_mol import SpecTransMolModel
    from transformers import BartConfig
    from unicore.data import Dictionary

    configuration = BartConfig.from_json_file(str(bart_config_path))
    arguments = Namespace(
        arch="spec_trans_mol_bart_base",
        configuration=configuration,
        seed=CHF_DECODE_POLICY.seed,
        share_embedding=True,
        shift_length=2795,
    )
    spec_dictionary = Dictionary.load(str(dictionary_path))
    mol_dictionary = Dictionary.load(str(dictionary_path))
    spec_dictionary.add_symbol("[MASK]", is_special=True)
    mol_dictionary.add_symbol("[MASK]", is_special=True)
    model = SpecTransMolModel(arguments, spec_dictionary, mol_dictionary)
    checkpoint_state = torch.load(
        checkpoint,
        map_location="cpu",
        weights_only=False,
    )
    model.load_state_dict(checkpoint_state["model"], strict=True)
    model.cpu()
    model.eval()
    if any(parameter.device.type != "cpu" for parameter in model.parameters()) or any(
        buffer.device.type != "cpu" for buffer in model.buffers()
    ):
        raise RuntimeError("loaded CHF model is outside the fixed CPU device")
    tokenizer = TokenizedNmrDataset(
        (),
        use_cnmr=True,
        use_hnmr=True,
        use_h_nh=True,
        use_h_jvalue=True,
        use_h_category=True,
        use_range_shift=False,
        use_formula=True,
        use_spec_end_token=True,
    )
    return NmrpeakChfRuntime(
        _LoadedNmrpeakChfStack(tokenizer, spec_dictionary, model)
    )
