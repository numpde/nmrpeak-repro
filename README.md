# nmrpeak-repro

Deployment repository for an NMRPeak NMR API provider with two fixed analysis
offerings:

- `nmrpeak_hf_v1` generates structures from a molecular formula and proton NMR.
- `nmrpeak_chf_v1` generates structures from a molecular formula, carbon NMR,
  and proton NMR.

The provider owns signed NMR API communication and durable Attempt recovery.
Each offering runs in its own networkless checkpoint container. NMRPeak and
Uni-Core source are pinned Git submodules; the public weights archive and every
selected checkpoint member have separate reviewed identities.

## Proof boundary

The maintained default verification lane is:

```sh
make test
```

It is networkless, credential-free, and checkpoint-free. It exercises the
provider lifecycle against a fake TLS API, both runner protocols, deployment
rendering and cleanup decisions, source and image inputs, release admission,
and checkpoint-volume operations with inert fixtures. It does not load the
downloaded NMRPeak checkpoints or claim a live deployment.

Clone with both pinned upstream repositories:

```sh
git clone --recurse-submodules https://github.com/numpde/nmrpeak-repro.git
cd nmrpeak-repro
make test
```

## Prepare the public weights

The tracked acquisition declaration pins Zenodo DOI
`10.5281/zenodo.19122815`, the exact versioned `weights.zip` URL, byte length,
and Zenodo MD5. Downloading is explicit and resumable:

```sh
make weights/download
```

Select an outbound interface only when required by the host:

```sh
make weights/download INTERFACE=wlp1s0
```

The command writes the ignored `weights/weights.zip.part` and renames it to
`weights/weights.zip` only after its size and MD5 match the declaration. Recheck
a complete local copy without network access with `make weights/check`.

## Review checkpoint releases

`release/write` reads one allowlisted ZIP member without importing Torch or
loading checkpoint bytes. It emits a candidate declaration to standard output;
it does not change the checkout.

```sh
make release/write \
  RUNNER=nmrpeak_hf_v1 \
  RELEASE=nmrpeak-zenodo-19122815-hf \
  ARCHIVE=/absolute/path/to/weights/weights.zip \
  > /absolute/path/to/hf-candidate.json

make release/write \
  RUNNER=nmrpeak_chf_v1 \
  RELEASE=nmrpeak-zenodo-19122815-chf \
  ARCHIVE=/absolute/path/to/weights/weights.zip \
  > /absolute/path/to/chf-candidate.json
```

Review each candidate, then install it without replacement:

```sh
make release/install \
  RUNNER=nmrpeak_hf_v1 \
  RELEASE=nmrpeak-zenodo-19122815-hf \
  ARCHIVE=/absolute/path/to/weights/weights.zip \
  DECLARATION=/absolute/path/to/hf-candidate.json

make release/install \
  RUNNER=nmrpeak_chf_v1 \
  RELEASE=nmrpeak-zenodo-19122815-chf \
  ARCHIVE=/absolute/path/to/weights/weights.zip \
  DECLARATION=/absolute/path/to/chf-candidate.json
```

The commands create
`models/nmrpeak_hf_v1/releases/nmrpeak-zenodo-19122815-hf.json` and
`models/nmrpeak_chf_v1/releases/nmrpeak-zenodo-19122815-chf.json`. Commit those
reviewed declarations before checkpoint import. Import is a separate explicit
Docker-volume mutation. It safely streams only the selected member and never
loads it:

```sh
make checkpoint/import \
  RUNNER=nmrpeak_hf_v1 \
  RELEASE=nmrpeak-zenodo-19122815-hf \
  ARCHIVE=/absolute/path/to/weights/weights.zip

make checkpoint/import \
  RUNNER=nmrpeak_chf_v1 \
  RELEASE=nmrpeak-zenodo-19122815-chf \
  ARCHIVE=/absolute/path/to/weights/weights.zip
```

## Build and configure a deployment

Image builds consume only committed, authenticated inputs. Dependency downloads
are explicit build effects; runtime Compose never builds or pulls implicitly.

```sh
make provider/image/build
make runner/image/build RUNNER=nmrpeak_hf_v1 TARGET=cpu-x86_64
make runner/image/build RUNNER=nmrpeak_chf_v1 TARGET=cpu-x86_64
```

Initialize one named deployment, install the matching API-issued private
provider credential, and review the generated configuration:

```sh
make provider/deployment/init DEPLOYMENT=prod
make provider/credential/install \
  DEPLOYMENT=prod \
  NMR_API_V1_DIR=/absolute/path/to/nmr-api-v1
make provider/deployment/config DEPLOYMENT=prod
```

Initialization creates `config/deployments/prod/provider.toml` and
`config/deployments/prod/deployment.toml`. In `deployment.toml`, bind the
API-issued `provider_ref`, both release names, and each run-generation identity
and admission window. In `provider.toml`, review the Server A origin/topology,
journal capacity and reserve, process pacing, and runner timeouts. The config
command validates those inputs, resolves the committed releases and local image
identities, and prints the exact read-only deployment plan. `up` revalidates
that plan and materializes the frozen generation consumed by startup.

Starting the deployment loads the reviewed checkpoints inside the two isolated
runner containers and begins signed API activity. It is therefore a deliberate
operator action, not a validation step:

```sh
make provider/deployment/up DEPLOYMENT=prod
make provider/deployment/status DEPLOYMENT=prod
make provider/logs DEPLOYMENT=prod
make provider/deployment/down DEPLOYMENT=prod
```

There is no blanket cleanup command. Normal teardown preserves configuration,
credentials, journals, retained generations, images, checkpoint volumes, and
the provider identity lock. Exceptional removal operations require exact
ownership proofs and confirmations.

## Design and operations reference

[`notes/001_nmrpeak_multi_runner_deployment_20260824.txt`](notes/001_nmrpeak_multi_runner_deployment_20260824.txt)
defines the lifecycle, trust, storage, failure, and proof boundaries. Checked-in
code and the current Makefile remain authoritative where the note describes a
requirement whose implementation or runtime proof is still pending.
