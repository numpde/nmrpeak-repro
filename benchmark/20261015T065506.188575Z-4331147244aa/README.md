# NMRPeak HF/CHF generation on CPU, Blackwell, and Ada

This experiment measures generation latency and candidate agreement for three
fixed synthetic inputs on one workstation. Both HF (proton peaks and formula)
and CHF (carbon/proton peaks and formula) were evaluated. The recorded execution
window was **2026-10-15 06:55:09–07:04:29 UTC**, from the first pre-run to the last
post-run activity snapshot. Source revision:
`4331147244aaa9cab5356b8dc864065950c3e592`.

[comparison.md](comparison.md) contains every median, p95, memory measurement,
and output comparison. [manifest.json](manifest.json) records source, image,
checkpoint and corpus identities, run order, software versions and activity.
The raw [worker logs](logs/), [inputs](cases.json),
[timings](measurements.jsonl), and [candidate outputs](outputs.jsonl) are committed
alongside the report. The original run files are preserved byte for byte.

## Machine and software

The following hardware facts were recorded during the experiment:

| Component | Recorded configuration |
|---|---|
| CPU | AMD Ryzen 9 7900X, x86_64; one socket, 12 physical cores, 24 logical CPUs, 2 threads/core |
| CPU cache / NUMA | 12 MiB aggregate L2, 64 MiB aggregate L3, one NUMA node |
| CPU frequency | Boost enabled; reported min/max 414.795/5737.986 MHz; these are not measured sustained execution frequencies |
| Blackwell GPU | NVIDIA RTX PRO 4000 Blackwell; 24,467 MiB reported memory; compute capability 12.0 |
| Ada GPU | NVIDIA RTX 2000 Ada Generation; 16,380 MiB reported memory; compute capability 8.9 |
| NVIDIA driver | 580.159.03 on both devices |
| CPU reference | PyTorch 2.3.0+cpu |
| Common CPU/GPU image | PyTorch 2.10.0+cu128, CUDA runtime 12.8 |
| Shared Python packages | NumPy 1.24.4, Transformers 4.55.4, RDKit 2024.9.6 |

GPU UUIDs identify the devices throughout the raw data:

- Blackwell: `GPU-75078110-7bdd-beaa-2195-2ba33a555cd7`.
- Ada: `GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347`.

The host OS, kernel, RAM and Docker version were **not captured during this
run**. A separate [machine observation](machine-observation.json), taken on the
same workstation on **2026-10-16 at 12:09:29 UTC**, found Ubuntu 24.04.4 LTS,
Linux 6.17.0-35-generic, Docker Engine 29.6.1, and 201,702,354,944 bytes
(187.85 GiB) of kernel-reported usable RAM (`MemTotal`). Host swap totaled
8,589,930,496 bytes. These are later observations, not proof of the exact earlier
host state or installed DIMM capacity. The container recipes at the source
revision pin Python 3.11.14 on Debian bookworm; Python's runtime version was not
independently emitted by this experiment.

Full image IDs and image-input digests are in the manifest. The original source
contains the hash-locked CPU and CUDA dependencies, digest-pinned base images,
and pinned NMRPeak/Uni-Core source closures. Image IDs identify the images used;
they are not downloadable registry references. Rebuilding requires the declared
upstream artifacts to remain available and need not produce identical image IDs.

## Protocol

One fresh container/model process ran for each of eight model/device
combinations, sequentially in this fixed order: original CPU HF/CHF, common
image CPU HF/CHF, Blackwell HF/CHF, Ada HF/CHF. No other benchmark lane ran in
parallel. Each container was configured for an eight-CPU quota, 32 GiB memory
with no additional swap allowance, 1 GiB shared memory, and a 2 GiB temporary
filesystem. The quota did not pin execution to particular physical cores.
Kernel enforcement of these requested limits was not independently recorded.
PyTorch used eight compute threads and one interop thread. GPU containers exposed
one selected physical GPU as `cuda:0`.

The committed `cases.json` defines small, medium, and long inputs. Token counts
were 15/37/91 for HF and 18/48/116 for CHF. These hand-authored workload fixtures
have no reference structures for evaluating chemical accuracy. Batch size was
one, precision FP32, with no autocast or TF32. Decoding used beam size 10,
maximum generated tokens 160, temperature 3.0, and seed 1. The Python, NumPy and
PyTorch random seeds were reset before every generation. cuDNN benchmarking was
disabled, its deterministic flag enabled, and `CUBLAS_WORKSPACE_CONFIG=:4096:8`
was set; this does not establish universal cross-platform determinism.

After checkpoint verification and model loading, each process made one initial
pass through all three cases, then three warmup passes and twenty measured
passes. Every pass visited small, medium, and long in that order. Thus each case
has 20 measured observations within one model process: 480 measured generations
and 576 generations including warmup/initial passes across the experiment.
The initial pass is excluded from reported median/p95; only the first actual
generation in each process is labelled first generation.

Generation wall time used Python `time.perf_counter`, with CUDA synchronization
before and after each GPU call. It includes input materialization, tokenization,
tensor creation/transfer, beam-search generation, and candidate string formatting.
Checkpoint verification, model loading/transfer, RDKit validity/canonicalization,
and event writing are outside this interval. CUDA qualification ran a small kernel
before loading/timing the model. Filesystem caches were not cleared. Process RSS
is a lifetime high-water mark; GPU allocation/reservation peaks are reset before
each generation and include the resident model.

The median is the usual sample median. p95 uses nearest rank, the 19th sorted
observation out of 20. Speed ratios divide each CPU median by the corresponding
GPU median. Ranked agreement compares complete candidate lists; molecule overlap
is Jaccard overlap of the sets of recorded valid canonical SMILES. Invalid
candidates are retained and excluded from that set comparison.

## Results and interpretation

All eight runs completed, with twenty observations in each of 24 model/device/
case groups. Compared with the **same PyTorch 2.10 image on CPU**, observed median
speed ratios were 3.94–9.13 for Blackwell and 4.01–5.97 for Ada. The separate
PyTorch 2.3 CPU reference also appears in the full report; its ratios include a
software-stack difference.

All 480 paired GPU/CPU comparisons (240 measured GPU outputs, each compared
with two CPU references) had identical ranked candidates and canonical molecule
overlap 1.0. Some small/medium outputs contained invalid SMILES on every device.
Agreement on this finite corpus does not establish model accuracy or equivalence
on unseen inputs.

The workstation was shared with existing services. Before the first run, the
Ada GPU already used 8,622 MiB, including a recorded compute process using
8,612 MiB. GPU utilization, memory, temperature, process IDs and host load
averages were sampled around each lane, not continuously during timing. The
benchmark itself contributes to host load. Blackwell's sampled temperature rose
from 28 to 56 degrees C during its lanes; Ada's rose from 31 to 51 degrees C
during its lanes. Clock rates, power limits, CPU governor and competing workload
were not controlled or continuously recorded.

This is one experiment with fixed ordering and repeated timings within each
process, without independent session replicates, randomized order or confidence
intervals. It supports the reported observations under these conditions, not a
general hardware ranking or population speedup estimate. A stronger performance
study would repeat independent sessions with counterbalanced device order and
controlled competing load. API/interpreter overhead and production GPU deployment
were outside this experiment.

## Reproduce the calculations offline

From a checkout containing this archive, using Python 3.11+ and Make:

```bash
make benchmark/verify RUN=benchmark/20261015T065506.188575Z-4331147244aa
```

This checks `SHA256SUMS` against the full file inventory and verifies the input
corpus digest. It regenerates the manifest, consolidated events, summary and
Markdown report in temporary storage from the archived manifest and worker logs,
then requires byte equality with every archived derived file. It uses Python's
standard library, without network, Docker, model weights or GPUs, and leaves the
archive untouched. Checksums detect byte changes; the Git commit identifies the
published checksum list. This reproduces the calculations on recorded outputs;
it does not rerun inference or independently validate the recorded RDKit results.

## Repeat the measurement

Use a Linux x86_64 workstation with Git, Make, Bash, Python 3.11+, a local Docker
default context with BuildKit, and a regular Docker-enabled account. GPU runs
require `nvidia-smi`, an NVIDIA driver and NVIDIA Container Toolkit supporting
both GPUs, plus enough available memory for the declared limits. From an existing
clone, prepare the measured source separately:

```bash
git worktree add --detach ../nmrpeak-benchmark-4331147 4331147244aaa9cab5356b8dc864065950c3e592
cd ../nmrpeak-benchmark-4331147
git submodule update --init --recursive
make benchmark/image/build
make benchmark/devices
```

Reuse the existing verified checkpoint volumes on the same Docker engine; the
benchmark admits and hashes them before use. On a different engine, use the
already acquired public archive through its absolute path:

```bash
make checkpoint/import RUNNER=nmrpeak_hf_v1 RELEASE=nmrpeak-zenodo-19122815-hf ARCHIVE=/absolute/path/to/weights.zip
make checkpoint/import RUNNER=nmrpeak_chf_v1 RELEASE=nmrpeak-zenodo-19122815-chf ARCHIVE=/absolute/path/to/weights.zip
```

If the public archive is absent, `make weights/download` acquires the archive
identified by `families/nmrpeak/weights.declaration.json` into `weights/weights.zip`.
The release declarations under `models/nmrpeak_{hf,chf}_v1/releases/` specify the
selected checkpoint members and their SHA-256 identities. Do not download another
copy merely because the checkout or benchmark lane changed.

Select the UUIDs printed by `make benchmark/devices`. On the original workstation:

```bash
make benchmark DEVICES='GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347' MODELS='hf chf' CASES='small medium long' REPETITIONS=20 WARMUPS=3 THREADS=8 TIMEOUT=7200
```

Both CPU references are included automatically. A repeat creates a new
`benchmark/<timestamp>-<sha>/` directory; retain it as a distinct experiment.
Timings will vary. Repeating the historical source also repeats its documented
metadata omissions. Current tooling records additional host/runtime provenance;
using it is a new source revision and must be reported as such.

## Publication and disclosure

This directory is an explicitly reviewed Git exception to the scratch-run ignore
rule. It contains synthetic scientific inputs/outputs, device identities and
performance evidence. No credentials, environment dumps, network addresses,
account names, machine IDs, hostnames or hardware serial-number inventories are
included. The supplemental collection queried only OS version fields, kernel
release/architecture, memory totals and Docker server version. Raw historical
data were reviewed without redaction; all additions are this methods document,
the dated machine observation and the checksum list.
