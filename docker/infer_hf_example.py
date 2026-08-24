#!/usr/bin/env python3
"""Run the supplied 1H + molecular-formula example inside the sandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path

import torch
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors

from nmrpeak.api.nmrpeak_generation import (
    api_nmrpeak_generation,
    get_generation_config,
)


TARGET_FORMULA = "C17H16N2O3"
MODEL_DIRECTORY = (
    "NMRexp_lr3e-4_bs16_gpu8_spec_trans_mol_bart_base_"
    "spec_trans_mol_60000_1000000"
)

SPECTRUM = {
    "molecular_formula": TARGET_FORMULA,
    "h_nmr_peaks": [
        {"category": "t", "j_values": "7.1_", "nH": 4, "rangeMin": 1.23, "rangeMax": 1.23},
        {"category": "q", "j_values": "7.1_", "nH": 2, "rangeMin": 4.20, "rangeMax": 4.20},
        {"category": "s", "j_values": "_", "nH": 2, "rangeMin": 5.34, "rangeMax": 5.34},
        {"category": "dd", "j_values": "9.1_2.8_", "nH": 1, "rangeMin": 7.17, "rangeMax": 7.17},
        {"category": "m", "j_values": "_", "nH": 1, "rangeMin": 7.25, "rangeMax": 7.25},
        {"category": "d", "j_values": "9.1_", "nH": 1, "rangeMin": 7.43, "rangeMax": 7.43},
        {"category": "d", "j_values": "2.8_", "nH": 1, "rangeMin": 7.51, "rangeMax": 7.51},
        {"category": "d", "j_values": "8.8_", "nH": 1, "rangeMin": 7.56, "rangeMax": 7.56},
        {"category": "m", "j_values": "_", "nH": 1, "rangeMin": 7.71, "rangeMax": 7.71},
        {"category": "dd", "j_values": "8.0_1.7_", "nH": 1, "rangeMin": 8.30, "rangeMax": 8.30},
    ],
}


def describe_smiles(smiles: str) -> dict[str, object]:
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return {"smiles": smiles, "valid": False, "formula": None, "formula_match": False}
    canonical = Chem.MolToSmiles(molecule)
    formula = rdMolDescriptors.CalcMolFormula(molecule)
    return {
        "smiles": smiles,
        "canonical_smiles": canonical,
        "valid": True,
        "formula": formula,
        "formula_match": formula == TARGET_FORMULA,
    }


def main() -> None:
    weights_root = Path(os.environ.get("NMRPEAK_WEIGHTS_DIR", "/models/current"))
    checkpoint = (
        weights_root
        / "generation"
        / "all_weights"
        / MODEL_DIRECTORY
        / "HF"
        / "checkpoint_best.pt"
    )
    if not checkpoint.is_file():
        raise SystemExit(f"HF checkpoint is missing: {checkpoint}")

    print(f"container checkpoint: {checkpoint}")
    print(f"device: {'cuda' if torch.cuda.is_available() else 'cpu'}")

    config = get_generation_config(
        dict_path="/opt/nmrpeak/dict",
        nmr_type="HF",
        base_dir=str(weights_root),
        generation_weight_dir=MODEL_DIRECTORY,
    )
    args = config["HF"]["args"]
    args.cpu = not torch.cuda.is_available()
    args.fp16 = torch.cuda.is_available()
    args.num_workers = 0

    generated, _scores = api_nmrpeak_generation(
        spec_list=[SPECTRUM],
        nmr_type="HF",
        generation_config=config,
        batch_size=1,
        beam_size=10,
        rerank=False,
    )
    candidates = [describe_smiles(smiles) for smiles in generated[0]]
    result = {
        "target_formula": TARGET_FORMULA,
        "integral_sum": sum(peak["nH"] for peak in SPECTRUM["h_nmr_peaks"]),
        "candidates": candidates,
        "formula_matches": [candidate for candidate in candidates if candidate["formula_match"]],
    }
    print("NMRPEAK_RESULT_JSON=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
