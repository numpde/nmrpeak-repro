"""Tie copied product vocabulary to the authenticated upstream source."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from nmrpeak_provider.product_input import SUPPORTED_MULTIPLICITIES


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

        self.assertEqual(140, len(multiplicities))
        self.assertEqual(frozenset(multiplicities), SUPPORTED_MULTIPLICITIES)


if __name__ == "__main__":
    unittest.main()
