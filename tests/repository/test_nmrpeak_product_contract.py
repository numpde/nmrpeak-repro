"""Tie copied product vocabulary to the authenticated upstream source."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from nmrpeak_provider.product_input import SUPPORTED_MULTIPLICITIES
from nmrpeak_provider.product_result import (
    MAX_DECODER_SYMBOL_BYTES,
    MAX_DECODER_SYMBOL_CHARACTERS,
    NMRPEAK_SOURCE_CLOSURE_REF,
    SUPPORTED_GENERATED_CHARACTERS,
    source_closure_ref,
)


REPOSITORY_ROOT = Path(__file__).parents[2]
VOCABULARY_SOURCE = (
    REPOSITORY_ROOT / "nmrpeak-upstream/nmrpeak/utils/vocab_rules.py"
)


class NmrpeakProductContractTests(unittest.TestCase):
    def test_multiplicities_equal_the_pinned_tokenizer_vocabulary(self) -> None:
        module = ast.parse(VOCABULARY_SOURCE.read_text(encoding="utf-8"))
        categorical_rules = next(
            node
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "CATEGORICAL_RULES"
                for target in node.targets
            )
        )
        upstream_rules = ast.literal_eval(categorical_rules.value)
        multiplicities = upstream_rules["hnmr_category"]

        self.assertEqual(frozenset(multiplicities), SUPPORTED_MULTIPLICITIES)

    def test_result_alphabet_equals_the_pinned_decoder_vocabulary(self) -> None:
        symbols = (
            REPOSITORY_ROOT / "nmrpeak-upstream/dict/bart/total_dict.txt"
        ).read_text(encoding="utf-8").splitlines()
        characters = frozenset("".join(symbols))
        self.assertEqual(characters, SUPPORTED_GENERATED_CHARACTERS)
        self.assertEqual(MAX_DECODER_SYMBOL_CHARACTERS, max(map(len, symbols)))
        self.assertEqual(
            MAX_DECODER_SYMBOL_BYTES,
            max(len(symbol.encode("utf-8")) for symbol in symbols),
        )

    def test_source_closure_identity_is_the_exact_manifest_digest(self) -> None:
        manifest = (
            REPOSITORY_ROOT / "families/nmrpeak/source-closure.sha256"
        ).read_bytes()
        self.assertEqual(NMRPEAK_SOURCE_CLOSURE_REF, source_closure_ref(manifest))


if __name__ == "__main__":
    unittest.main()
