#!/usr/bin/env python3
"""Run the supplied 1H + 13C + molecular-formula example in the sandbox."""

from __future__ import annotations

import argparse
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


TARGET_FORMULA = "C16H27NO8"
MODEL_DIRECTORY = (
    "NMRexp_lr3e-4_bs16_gpu8_spec_trans_mol_bart_base_"
    "spec_trans_mol_60000_1000000"
)

SPECTRUM = {
    "molecular_formula": TARGET_FORMULA,
    "h_nmr_peaks": [
        {"category": "m", "j_values": "_", "nH": 2, "rangeMin": 4.91, "rangeMax": 4.99},
        {"category": "dd", "j_values": "7.9_2.6_", "nH": 1, "rangeMin": 4.62, "rangeMax": 4.62},
        {"category": "t", "j_values": "6.3_", "nH": 1, "rangeMin": 4.59, "rangeMax": 4.59},
        {"category": "d", "j_values": "2.6_", "nH": 1, "rangeMin": 4.33, "rangeMax": 4.33},
        {"category": "ddd", "j_values": "7.9_1.9_0.8_", "nH": 1, "rangeMin": 4.24, "rangeMax": 4.24},
        {"category": "d", "j_values": "10.5_", "nH": 1, "rangeMin": 4.22, "rangeMax": 4.22},
        {"category": "d", "j_values": "10.6_", "nH": 1, "rangeMin": 4.15, "rangeMax": 4.15},
        {"category": "dd", "j_values": "13.0_1.9_", "nH": 1, "rangeMin": 3.91, "rangeMax": 3.91},
        {"category": "dd", "j_values": "13.0_0.8_", "nH": 1, "rangeMin": 3.76, "rangeMax": 3.76},
        {"category": "m", "j_values": "_", "nH": 2, "rangeMin": 3.68, "rangeMax": 3.71},
        {"category": "dd", "j_values": "1.4_0.8_", "nH": 3, "rangeMin": 1.79, "rangeMax": 1.79},
        {"category": "d", "j_values": "0.7_", "nH": 3, "rangeMin": 1.55, "rangeMax": 1.55},
        {"category": "d", "j_values": "0.7_", "nH": 3, "rangeMin": 1.47, "rangeMax": 1.47},
        {"category": "d", "j_values": "0.7_", "nH": 3, "rangeMin": 1.42, "rangeMax": 1.42},
        {"category": "d", "j_values": "0.7_", "nH": 3, "rangeMin": 1.34, "rangeMax": 1.34},
    ],
    # Duplicate shifts are intentionally separate observations.
    "c_nmr_peaks": [
        {"delta (ppm)": 140.4},
        {"delta (ppm)": 113.4},
        {"delta (ppm)": 109.4},
        {"delta (ppm)": 109.4},
        {"delta (ppm)": 101.0},
        {"delta (ppm)": 70.8},
        {"delta (ppm)": 70.4},
        {"delta (ppm)": 70.4},
        {"delta (ppm)": 70.1},
        {"delta (ppm)": 61.5},
        {"delta (ppm)": 49.8},
        {"delta (ppm)": 26.6},
        {"delta (ppm)": 26.0},
        {"delta (ppm)": 25.3},
        {"delta (ppm)": 24.2},
        {"delta (ppm)": 20.3},
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--beam-size", type=int, default=10)
    parser.add_argument("--matches-only", action="store_true")
    cli_args = parser.parse_args()
    if cli_args.beam_size < 1 or cli_args.beam_size > 200:
        raise SystemExit("--beam-size must be between 1 and 200")

    weights_root = Path(os.environ.get("NMRPEAK_WEIGHTS_DIR", "/models/current"))
    checkpoint = (
        weights_root
        / "generation"
        / "all_weights"
        / MODEL_DIRECTORY
        / "CHF"
        / "checkpoint_best.pt"
    )
    if not checkpoint.is_file():
        raise SystemExit(f"CHF checkpoint is missing: {checkpoint}")

    print(f"container checkpoint: {checkpoint}")
    print(f"device: {'cuda' if torch.cuda.is_available() else 'cpu'}")

    config = get_generation_config(
        dict_path="/opt/nmrpeak/dict",
        nmr_type="CHF",
        base_dir=str(weights_root),
        generation_weight_dir=MODEL_DIRECTORY,
    )
    args = config["CHF"]["args"]
    args.cpu = not torch.cuda.is_available()
    args.fp16 = torch.cuda.is_available()
    args.num_workers = 0

    generated, _scores = api_nmrpeak_generation(
        spec_list=[SPECTRUM],
        nmr_type="CHF",
        generation_config=config,
        batch_size=1,
        beam_size=cli_args.beam_size,
        rerank=False,
    )
    candidates = [describe_smiles(smiles) for smiles in generated[0]]
    formula_matches = [candidate for candidate in candidates if candidate["formula_match"]]
    result = {
        "target_formula": TARGET_FORMULA,
        "beam_size": cli_args.beam_size,
        "proton_integral_sum": sum(peak["nH"] for peak in SPECTRUM["h_nmr_peaks"]),
        "carbon_peak_count": len(SPECTRUM["c_nmr_peaks"]),
        "candidate_count": len(candidates),
        "valid_candidate_count": sum(bool(candidate["valid"]) for candidate in candidates),
        "formula_matches": formula_matches,
    }
    if not cli_args.matches_only:
        result["candidates"] = candidates
    print("NMRPEAK_RESULT_JSON=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
