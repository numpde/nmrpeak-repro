"""Compose the pinned NMRPeak CHF generation component on CPU."""

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
from nmrpeak_provider.chf_binding import materialize_chf_nmrpeak_document
from nmrpeak_provider.product_decode import CHF_DECODE_POLICY


CHF_TOKENIZER_MODE = TokenizerMode(
    use_cnmr=True,
    use_hnmr=True,
    use_h_nh=True,
    use_h_jvalue=True,
    use_h_category=True,
    use_formula=True,
)


class NmrpeakChfRuntime(NmrpeakRuntime):
    """Apply the CHF document projection before family tokenization."""

    def __init__(self, stack: NmrpeakInferenceStack) -> None:
        super().__init__(stack, materialize_chf_nmrpeak_document)


def load_nmrpeak_chf_runtime(
    checkpoint: BinaryIO,
    *,
    dictionary_path: Path = DICTIONARY_PATH,
    bart_config_path: Path = BART_CONFIG_PATH,
) -> NmrpeakRuntime:
    """Deserialize one verified CHF checkpoint into the fixed CPU component."""

    return load_nmrpeak_runtime(
        checkpoint,
        tokenizer_mode=CHF_TOKENIZER_MODE,
        decode_policy=CHF_DECODE_POLICY,
        materialize=materialize_chf_nmrpeak_document,
        dictionary_path=dictionary_path,
        bart_config_path=bart_config_path,
        device="cpu",
    )
