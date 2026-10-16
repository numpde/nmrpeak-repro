"""Measure one model/device in a networkless container; emit flushed JSON events."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import platform
import random
import resource
import sys
import time

_EVENTS = sys.stdout


def emit(kind: str, **facts: object) -> None:
    print(json.dumps({"type": kind, **facts}, sort_keys=True, default=str), file=_EVENTS, flush=True)


def measured(operation, synchronize):
    synchronize()
    started = time.perf_counter()
    result = operation()
    synchronize()
    return result, time.perf_counter() - started


def configure_torch(torch, device: str, threads: int) -> None:
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    if device == "cuda":
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError("Cannot benchmark CUDA: exactly one usable GPU must be exposed")
        # Execute a kernel before accepting a GPU; availability alone does not prove support.
        probe = torch.ones((8, 8), device="cuda:0")
        result = (probe @ probe).sum().item()
        if result != 512:
            raise RuntimeError(f"CUDA qualification produced {result!r}; expected 512")
        torch.cuda.synchronize()
        emit("cuda", name=torch.cuda.get_device_name(0),
             capability=torch.cuda.get_device_capability(0), architectures=torch.cuda.get_arch_list())


def load_lane(lane: str):
    if lane == "hf":
        from models.nmrpeak_hf_v1.runner.runtime import HF_TOKENIZER_MODE, load_nmrpeak_hf_runtime
        from nmrpeak_provider.hf_binding import parse_hf_runner_input, materialize_hf_nmrpeak_document
        from nmrpeak_provider.product_decode import HF_DECODE_POLICY
        return HF_TOKENIZER_MODE, HF_DECODE_POLICY, parse_hf_runner_input, materialize_hf_nmrpeak_document, load_nmrpeak_hf_runtime
    from models.nmrpeak_chf_v1.runner.runtime import CHF_TOKENIZER_MODE, load_nmrpeak_chf_runtime
    from nmrpeak_provider.chf_binding import parse_chf_runner_input, materialize_chf_nmrpeak_document
    from nmrpeak_provider.product_decode import CHF_DECODE_POLICY
    return CHF_TOKENIZER_MODE, CHF_DECODE_POLICY, parse_chf_runner_input, materialize_chf_nmrpeak_document, load_nmrpeak_chf_runtime


def candidate_facts(candidates):
    from rdkit import Chem
    canonical = []
    for candidate in candidates:
        molecule = Chem.MolFromSmiles(candidate)
        canonical.append(None if molecule is None else Chem.MolToSmiles(molecule))
    return {"candidates": candidates, "canonical_smiles": canonical,
            "valid_count": sum(value is not None for value in canonical),
            "candidate_characters": [len(value) for value in candidates]}


def run(options) -> None:
    import numpy as np
    import torch
    from families.nmrpeak.checkpoint_file import open_verified_checkpoint
    from families.nmrpeak.runner_runtime import load_nmrpeak_runtime

    configure_torch(torch, options.device, options.threads)
    mode, policy, parse_input, materialize, load_cpu = load_lane(options.lane)
    corpus = json.loads(Path(options.corpus).read_text())
    cases = [(case["id"], parse_input(case["inputs"][options.lane])) for case in corpus["cases"]
             if not options.case or case["id"] in options.case]
    if not cases:
        raise ValueError("Cannot benchmark: no corpus cases were selected")
    versions = {name: importlib.metadata.version(name) for name in
                ("torch", "numpy", "transformers", "rdkit")}
    versions["python"] = platform.python_version()
    emit("configuration", versions=versions, torch_cuda=torch.version.cuda,
         threads=torch.get_num_threads(), interop_threads=torch.get_num_interop_threads(),
         device=options.device, precision="float32", tf32=False, decode_policy=asdict(policy))
    synchronize = torch.cuda.synchronize if options.device == "cuda" else lambda: None
    started = time.perf_counter()
    with open_verified_checkpoint(options.checkpoint) as checkpoint:
        emit("checkpoint", verification_seconds=time.perf_counter() - started,
             checkpoint=options.checkpoint)
        if options.baseline:
            runtime, duration = measured(lambda: load_cpu(checkpoint), synchronize)
        else:
            runtime, duration = measured(lambda: load_nmrpeak_runtime(
                checkpoint, tokenizer_mode=mode, decode_policy=policy,
                materialize=materialize, device=options.device), synchronize)
    emit("load", model_load_and_transfer_seconds=duration)
    for case_id, model_input in cases:
        runtime.validate(model_input)
        # Count the actual maintained tokenizer output, outside the timed generation.
        token_count = len(runtime._tokenize(model_input))
        emit("case", case=case_id, input_tokens=token_count)
    if options.device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    first_generation = True
    for phase, count in (("initial_case_pass", 1), ("warmup", options.warmups), ("measured", options.repetitions)):
        for repetition in range(count):
            for case_id, model_input in cases:
                random.seed(policy.seed)
                np.random.seed(policy.seed)
                torch.manual_seed(policy.seed)
                if options.device == "cuda":
                    torch.cuda.reset_peak_memory_stats()
                candidates, duration = measured(lambda: runtime.generate(model_input), synchronize)
                emit("measurement", case=case_id, phase=phase, repetition=repetition,
                     elapsed_seconds=duration, first_generation=first_generation,
                     process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                     gpu_peak_allocated_bytes=(torch.cuda.max_memory_allocated() if options.device == "cuda" else None),
                     gpu_peak_reserved_bytes=(torch.cuda.max_memory_reserved() if options.device == "cuda" else None))
                emit("output", case=case_id, phase=phase, repetition=repetition,
                     **candidate_facts(candidates))
                first_generation = False
    emit("complete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=("hf", "chf"), required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--warmups", type=int, required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    options = parser.parse_args()
    if options.baseline and options.device != "cpu":
        parser.error("the current runner baseline supports CPU only")
    if options.threads < 1 or options.warmups < 0 or options.repetitions < 1:
        parser.error("threads/repetitions must be positive and warmups must be nonnegative")
    # Upstream modules print optional-extension notices; keep the JSON event stream separate.
    with redirect_stdout(sys.stderr):
        run(options)


if __name__ == "__main__":
    main()
