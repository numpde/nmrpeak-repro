# nmrpeak-repro

Deployment repository for an NMRPeak NMR API provider with two fixed analysis
offerings:

- `nmrpeak_hf_v1` generates structures from a molecular formula and proton NMR.
- `nmrpeak_chf_v1` generates structures from a molecular formula, carbon NMR,
  and proton NMR.

When deployed, each offering runs its pinned source and reviewed checkpoint in
a separate networkless container. The provider owns signed NMR API communication
and durable recovery of interrupted API attempts.

## Scientific references

- **Model and upstream implementation:** Xu et al.,
  [“NMRPeak: a ready-to-use intelligent system for molecular structure
  elucidation enabled by synergistic cross-modal
  learning”](https://arxiv.org/abs/2602.08752), arXiv:2602.08752 (2026).
  The `nmrpeak-upstream` submodule pins the authors'
  [official implementation](https://github.com/Colin-Jay/NMRPeak).
- **Experimental training-data source:** Wang et al.,
  [“NMRexp: A database of 3.3 million experimental NMR spectra”](https://doi.org/10.1038/s41597-025-06245-5),
  *Scientific Data* **12**, 1954 (2025). NMRPeak uses a curated subset of
  NMRexp together with simulated MST-NMR data; the NMRPeak paper describes its
  curation and splits. This deployment repository contains neither the training
  data nor the training pipeline.

## Operator map

Run `make help` for the authoritative command list, required inputs, effects,
and operational boundaries. Do not infer current commands from the design note.

## Proof boundary

Clone this repository with its pinned submodules and run the maintained
verification lane:

```sh
git clone --recurse-submodules https://github.com/numpde/nmrpeak-repro.git
cd nmrpeak-repro
# Use a Python environment containing the dependencies in requirements.lock.
make test
```

`make test` is networkless, credential-free, and checkpoint-free. It exercises
the provider and both runner protocols with inert fixtures. It neither loads
the released checkpoints nor proves a live deployment.

## Design and operations reference

[`notes/001_nmrpeak_multi_runner_deployment_20260824.txt`](notes/001_nmrpeak_multi_runner_deployment_20260824.txt)
records the design rationale and lifecycle boundaries. Checked-in code and
`make help` remain authoritative for current behavior and commands.
