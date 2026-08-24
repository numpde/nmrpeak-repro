#!/usr/bin/env python3
"""Dependency/import smoke test that intentionally cannot load checkpoints."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import pickle
import sys


def _blocked_loader(*_args, **_kwargs):
    raise RuntimeError("checkpoint/pickle loading is disabled in the smoke test")


for cache_dir in (
    Path(os.environ.get("HOME", "/tmp/home")),
    Path(os.environ.get("MPLCONFIGDIR", "/tmp/matplotlib")),
    Path(os.environ.get("HF_HOME", "/tmp/huggingface")),
    Path(os.environ.get("TORCH_HOME", "/tmp/torch")),
):
    cache_dir.mkdir(parents=True, exist_ok=True)

# Block the common unsafe checkpoint paths before importing NMRPeak modules.
pickle.load = _blocked_loader
pickle.loads = _blocked_loader

import torch  # noqa: E402

torch.load = _blocked_loader
torch.jit.load = _blocked_loader

modules = (
    "numpy",
    "pandas",
    "rdkit",
    "transformers",
    "unicore",
    "faiss",
    "nmrpeak",
    "nmrpeak.api",
)

for module_name in modules:
    importlib.import_module(module_name)
    print(f"ok: import {module_name}")

print(f"python={sys.version.split()[0]}")
print(f"torch={torch.__version__}")
print(f"cuda_available={torch.cuda.is_available()}")
print("ok: checkpoint loading remained disabled")
