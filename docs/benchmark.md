# CPU/GPU comparison

The [2026-10-15 experiment](https://github.com/numpde/nmrpeak-repro/tree/main/benchmark/20261015T065506.188575Z-4331147244aa)
includes the machine description, protocol, limitations, raw observations, and
derived results in Git. Reproduce its calculations offline without GPUs or weights:

```bash
make benchmark/verify RUN=benchmark/20261015T065506.188575Z-4331147244aa
```

Verification checks file hashes, corpus identity, completeness and byte-for-byte
report regeneration in temporary storage. It does not repeat inference or RDKit
validation. The experiment README also gives commands to repeat the measurement
at its original source revision.

Run on the Linux x86_64 machine being measured, using Git, Make, Bash, Python
3.11+, and a local Docker default context with BuildKit. Use a regular
Docker-enabled account. GPU runs require `nvidia-smi`, a compatible NVIDIA driver
and NVIDIA Container Toolkit. Initialize the pinned submodules and use a clean,
committed checkout:

```bash
git submodule update --init --recursive
make benchmark/image/build
```

To select a route without a prompt, use
`make benchmark/image/build NETWORK_INTERFACE=wlp10s0`, substituting your
chosen interface, or use `NETWORK_INTERFACE=system` for the system route.
Without a selection, an interactive terminal offers a choice with no default;
unattended builds require an explicit selection. An interface selection starts
a temporary bound proxy for dependency tools; base/frontend image pulls retain
Docker engine networking.
It does not change host routes, DNS, or daemon configuration.

The build prepares the current HF/CHF CPU runner images and one common benchmark
image. The run measures both models on the current CPU stack, the common image on
CPU, and the common image on every discovered GPU, sequentially. It does not
submit Jobs, contact an interpreter, or change the deployed provider.

Imported HF and CHF checkpoint volumes must already exist. Their reviewed release
identities, volume markers, and checkpoint bytes are verified before execution.
If they are missing, reuse an existing public weights archive at its absolute
path to import both checkpoints into the local engine:

```bash
make checkpoint/import RUNNER=nmrpeak_hf_v1 RELEASE=nmrpeak-zenodo-19122815-hf ARCHIVE=/absolute/path/to/weights.zip
make checkpoint/import RUNNER=nmrpeak_chf_v1 RELEASE=nmrpeak-zenodo-19122815-chf ARCHIVE=/absolute/path/to/weights.zip
```

If no archive is available, `make weights/download` acquires the pinned archive
into `weights/weights.zip`. Reuse that verified file across checkouts and lanes.
Once the images and checkpoints are ready, start the comparison:

```bash
make benchmark
```

Results are created exclusively under `benchmark/<UTC-timestamp>-<12-character-sha>/`.
The timestamp includes microseconds; existing comparisons are never replaced.
New runs are ignored as scratch data until reviewed for publication. Each run
directory contains:

- `manifest.json`: full source SHA, input/image/checkpoint/corpus identities,
  hardware, driver, resource settings, activity snapshots, and per-run status.
- `cases.json`: the exact synthetic runner inputs used in the comparison.
- `measurements.jsonl` and `outputs.jsonl`: consolidated measurements and candidates.
- `logs/`: flushed worker events and complete stderr, including upstream notices
  and tracebacks. Raw events survive interruption even before consolidation.
- `summary.json` and `comparison.md`: derived results, including incomplete runs.
- `error.log` when orchestration fails.

Host provenance includes CPU inventory, OS version, kernel release/architecture,
kernel-reported RAM and swap totals, Docker server version, and observation time.
Workers record Python and relevant library versions. Collection does not dump
environment variables, account/network configuration or system serial numbers.
Earlier runs may lack these fields; disclose missing provenance instead of
filling historical fields with current observations.

For a result used to support a shared claim, commit the entire reviewed run:
inputs, manifest, raw logs and derived outputs, plus a methods README describing
the machine, protocol, results and limitations. Add a precise exception under
`/benchmark/*` in `.gitignore`; preserve the timestamp/source directory name.
Review for sensitive material before publication, and disclose any redactions.
After finalizing that directory's contents, create its checksum inventory:

```bash
cd benchmark/<timestamp>-<sha>
sha256sum README.md cases.json manifest.json logs/* measurements.jsonl outputs.jsonl summary.json comparison.md > SHA256SUMS
```

Include any additional evidence files in that command (such as the historical
experiment's separately dated `machine-observation.json`). From the repository
root, run `make benchmark/verify RUN=benchmark/<timestamp>-<sha>` before committing.
Do not publish a prose summary whose supporting observations exist only in an
ignored directory or temporary logs. Preserve published runs; corrections and
new measurements must explain their provenance.

## Selection and quick qualification

```bash
make benchmark/devices
make benchmark DEVICES=cpu
make benchmark DEVICES='GPU-<full-uuid>' MODELS=hf CASES=small REPETITIONS=1 WARMUPS=0
```

CPU references are always included. `DEVICES=all` is the default; it requires at
least one GPU. Multiple UUIDs can be supplied separated by spaces. Missing GPUs
and CUDA execution failures are reported explicitly; the run never falls back to
CPU. Each GPU container sees only its selected device, exposed as `cuda:0`.

`MODELS` defaults to `hf chf`; `CASES` defaults to all three committed synthetic
cases. The cases cover relative short, medium, and long input sizes. They are
workload fixtures, not real reference spectra or scientific accuracy evidence.
Every case passes the maintained parser and tokenizer admission before timing.

Defaults are three warm-up passes and twenty measured passes per case, after one
initial case pass. Only the first actual generation on each freshly loaded model
is labelled first generation. `REPETITIONS`, `WARMUPS`, `THREADS` (1..8), and
`TIMEOUT` (seconds per model/device container, default 7200) can be overridden.
Timeouts, Ctrl-C, and SIGTERM trigger owned-container cleanup and preserve an
incomplete report. SIGKILL, host failure, or Docker daemon failure can prevent
cleanup; raw event logs remain the available evidence.

## What is measured

All lanes use batch size one, FP32 without autocast or TF32, and the maintained
beam-search decoding policy. Inputs, checkpoints, dictionaries, seed, and policy
are identical between devices. The common CPU/GPU image uses the same pinned
software; the current CPU stack is a separate operational reference, since its
PyTorch version differs.

Generation latency includes input materialization, tokenization, tensor creation
and transfer, beam search, and candidate string formatting. CUDA synchronization
surrounds wall-clock timing. RDKit candidate validation and JSON event writing
happen after the measured call. Model loading plus device transfer and checkpoint
verification are recorded separately from generation. Filesystem caches are not
cleared, so loading times are not cold-disk measurements.

The report includes median and nearest-rank p95, input token count, candidate
validity and character lengths, exact ranked output equality, canonical molecule
set overlap, and speed ratios against both CPU references. Output differences
are retained; numerical differences are not silently treated as equivalent.

Host RSS is the cumulative process high-water mark. GPU allocated/reserved peaks
are reset before each generation and include the resident model. Neither is total
host or GPU memory use. Each container is configured for an eight-CPU quota,
32 GiB memory with no additional swap allowance, and 1 GiB shared memory. Execution
is not pinned to particular cores. CPU compute threads default to eight and
interop threads to one.

Host load averages are recorded before and after each lane; GPU selections also
record GPU utilization, memory, process IDs and temperatures. CPU-only selections
do not inventory GPU activity. The benchmark does not isolate competing host
workloads, and these snapshots cannot establish absence of interference throughout
a lane.

## Image and container authority

Builds use allowlisted committed source and pinned upstream closures; secrets,
weights, local deployment state, and benchmark results are excluded. Networked
builds fetch pinned dependency artifacts. `make benchmark/lock` uses the same
explicit network-route selection and stages a new hash-locked candidate in
`/tmp` for review; it does not replace the committed lock.

CPU and CUDA image builds use the builder-wide UV cache at `/root/.cache/uv`,
without a repository or lane-specific cache ID. Other repositories using that
same cache mount on the same builder can reuse completed artifacts. Each install
still checks its own lockfile hashes and copies packages into its own image.
Cache reuse does not imply that different package versions or CPU/CUDA wheels are
interchangeable. Builder garbage collection or an incomplete download can cause
an artifact to be fetched again; changing repositories or lanes should not.

The common image pins PyTorch 2.10.0 with CUDA 12.8 for Ada and Blackwell support.
Before selecting it, the PyTorch security advisories were reviewed, including the
checkpoint issue fixed in 2.10.0:
https://github.com/pytorch/pytorch/security/advisories/GHSA-63cw-57p8-fm3p
An OSV check of all 61 pinned benchmark packages also reported advisories in the
inherited scikit-learn, tqdm, and transformers pins, and a torch.jit.script issue
([GHSA-rrmf-rvhw-rf47](https://github.com/advisories/GHSA-rrmf-rvhw-rf47), fixed in
PyTorch 2.13). This benchmark preserves the reviewed family dependency intent and
CUDA 12.8 compatibility; it is not an advisory-free
image. It executes committed JIT code and fixed local configuration, with synthetic
inputs, no tokenizer/model downloads, no tqdm CLI, and no TF-IDF training. The
production dependency lock is unchanged. Review advisories again when refreshing
the benchmark lock.

Only the existing reviewed, SHA-256-verified checkpoints are deserialized; loading
uses the existing trusted-checkpoint `weights_only=False` policy.

Runtime uses the local Docker default context and immutable image IDs, with no
network, read-only root and checkpoint mounts, dropped capabilities, no new
privileges, bounded resources, and the image's fixed non-root identity. The harness mount
contains only committed worker code and synthetic cases. Output goes through
host-opened stdout/stderr files; containers have no writable host mount or engine
socket. Temporary writes use bounded tmpfs. GPU access is an explicit UUID grant;
GPU containers still share the host kernel and driver.

The benchmark is opt-in. Ordinary tests check its measurements, reporting, source
selection, and command construction without requiring weights or a GPU; they do
not enforce speed thresholds.
