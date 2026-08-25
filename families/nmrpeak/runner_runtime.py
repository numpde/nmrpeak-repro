"""Load and execute the model stack shared by the fixed NMRPeak runners."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Protocol

from nmrpeak_provider.canonical_json import JsonValue
from nmrpeak_provider.product_decode import DecodePolicy


DICTIONARY_PATH = Path("/opt/nmrpeak/dict/bart/total_dict.txt")
BART_CONFIG_PATH = Path("/opt/nmrpeak/bart-base/config.json")
MAXIMUM_TOKENIZED_INPUT_LENGTH = 511


@dataclass(frozen=True, slots=True)
class TokenizerMode:
    """The spectrum fields consumed by one pinned NMRPeak checkpoint lane."""

    use_cnmr: bool
    use_hnmr: bool
    use_h_nh: bool
    use_h_jvalue: bool
    use_h_category: bool
    use_formula: bool


class NmrpeakRuntimeInputRejected(ValueError):
    """The loaded tokenizer deterministically rejects one complete input."""


class NmrpeakInferenceStack(Protocol):
    """The pinned tokenizer and loaded model behavior used by one runtime."""

    def tokenize(self, document: dict[str, object]) -> tuple[str, ...]: ...

    def generate(self, tokens: tuple[str, ...]) -> JsonValue: ...


class NmrpeakRuntime:
    """Validate with the real tokenizer before entering model execution."""

    def __init__(
        self,
        stack: NmrpeakInferenceStack,
        materialize: Callable[[object], dict[str, object]],
    ) -> None:
        self._stack = stack
        self._materialize = materialize

    def validate(self, model_input: object) -> None:
        tokens = self._tokenize(model_input)
        if not tokens or len(tokens) > MAXIMUM_TOKENIZED_INPUT_LENGTH:
            raise NmrpeakRuntimeInputRejected()

    def generate(self, model_input: object) -> JsonValue:
        return self._stack.generate(self._tokenize(model_input))

    def _tokenize(self, model_input: object) -> tuple[str, ...]:
        return self._stack.tokenize(self._materialize(model_input))


class LoadedNmrpeakStack:
    """Own the family tokenizer and model calls for one protocol request."""

    def __init__(
        self,
        tokenizer: object,
        dictionary: object,
        model: object,
        decode_policy: DecodePolicy,
    ) -> None:
        self._tokenizer = tokenizer
        self._dictionary = dictionary
        self._model = model
        self._decode_policy = decode_policy

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
            max_len=self._decode_policy.maximum_generated_tokens,
            beam_size=self._decode_policy.beam_size,
            temperature=float(self._decode_policy.temperature),
            use_beam_search=True,
        )
        return ["".join(sequence) for sequence in generated[0]]


def load_nmrpeak_runtime(
    checkpoint: BinaryIO,
    *,
    tokenizer_mode: TokenizerMode,
    decode_policy: DecodePolicy,
    materialize: Callable[[object], dict[str, object]],
    dictionary_path: Path = DICTIONARY_PATH,
    bart_config_path: Path = BART_CONFIG_PATH,
) -> NmrpeakRuntime:
    """Deserialize one verified checkpoint into the fixed CPU model stack."""

    import torch
    from nmrpeak.data import TokenizedNmrDataset
    from nmrpeak.models.spec_trans_mol import SpecTransMolModel
    from transformers import BartConfig
    from unicore.data import Dictionary

    configuration = BartConfig.from_json_file(str(bart_config_path))
    arguments = Namespace(
        arch="spec_trans_mol_bart_base",
        configuration=configuration,
        seed=decode_policy.seed,
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
        raise RuntimeError("loaded NMRPeak model is outside the fixed CPU device")
    tokenizer = TokenizedNmrDataset(
        (),
        use_cnmr=tokenizer_mode.use_cnmr,
        use_hnmr=tokenizer_mode.use_hnmr,
        use_h_nh=tokenizer_mode.use_h_nh,
        use_h_jvalue=tokenizer_mode.use_h_jvalue,
        use_h_category=tokenizer_mode.use_h_category,
        use_range_shift=False,
        use_formula=tokenizer_mode.use_formula,
        use_spec_end_token=True,
    )
    return NmrpeakRuntime(
        LoadedNmrpeakStack(tokenizer, spec_dictionary, model, decode_policy),
        materialize,
    )
