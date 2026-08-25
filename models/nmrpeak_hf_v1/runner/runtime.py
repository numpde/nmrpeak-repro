"""Compose the pinned NMRPeak HF generation component on CPU."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from families.nmrpeak.runner_runtime import (
    BART_CONFIG_PATH,
    DICTIONARY_PATH,
    NmrpeakInferenceStack,
    NmrpeakRuntime,
    TokenizerMode,
    load_nmrpeak_runtime,
)
from nmrpeak_provider.hf_binding import materialize_hf_nmrpeak_document
from nmrpeak_provider.product_decode import HF_DECODE_POLICY


HF_TOKENIZER_MODE = TokenizerMode(
    use_cnmr=False,
    use_hnmr=True,
    use_h_nh=True,
    use_h_jvalue=True,
    use_h_category=True,
    use_formula=True,
)


class NmrpeakHfRuntime(NmrpeakRuntime):
    """Apply the HF document projection before family tokenization."""

    def __init__(self, stack: NmrpeakInferenceStack) -> None:
        super().__init__(stack, materialize_hf_nmrpeak_document)


def load_nmrpeak_hf_runtime(
    checkpoint: BinaryIO,
    *,
    dictionary_path: Path = DICTIONARY_PATH,
    bart_config_path: Path = BART_CONFIG_PATH,
) -> NmrpeakRuntime:
    """Deserialize one verified HF checkpoint into the fixed CPU component."""

    return load_nmrpeak_runtime(
        checkpoint,
        tokenizer_mode=HF_TOKENIZER_MODE,
        decode_policy=HF_DECODE_POLICY,
        materialize=materialize_hf_nmrpeak_document,
        dictionary_path=dictionary_path,
        bart_config_path=bart_config_path,
    )
