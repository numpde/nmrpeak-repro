# nmrpeak-repro

Deployment repository for an NMRPeak NMR API provider.

[Documentation](https://numpde.github.io/nmrpeak-repro/) — analysis offerings,
operator instructions, scientific references, and licensing.

The provider serves `mol_from_1h_peaks` (HF: molecular formula and proton peaks)
and `mol_from_1h_13c_formula` (CHF: formula, proton peaks, and carbon peaks).
Production model runners use CPU. See the documentation for the structured-input
and freeform-text routes and deployment requirements.

## Check the software

The maintained tests run in a pinned Docker image. On Linux, use Git, Make,
Bash, Python 3.11+, Docker with Compose and a local Unix-socket context, and a regular
Docker-enabled account. Every Docker-using Make target requires
`DOCKER_CONTEXT=<name>` or an interactive selection; there is no default. The
image-preparation command requires a network-route choice; building a missing
image also requires Podman. If the matching image already exists, run
`make test DOCKER_CONTEXT=<name>`. Otherwise replace `wlp1s0` with your
chosen interface, or use `system` for the default route:

```bash
git submodule update --init --recursive
make test-image/base/build NETWORK_INTERFACE=wlp1s0 DOCKER_CONTEXT=<name>
make test DOCKER_CONTEXT=<name>
```

Networked build and dependency-staging commands ask for a route when one is not
supplied. Choose a listed interface, or `system` for the default route; there is
no default selection, and unattended commands must set `NETWORK_INTERFACE` on
the Make command line. Interface-bound dependency downloads use a temporary
proxy for tools that honor it; it is not an egress firewall. Docker base and
frontend image acquisition uses the engine's route; test-image base bootstrap
uses Podman's default connection.
Image preparation reuses an existing matching image.
`make test DOCKER_CONTEXT=<name>` runs without network, API credentials, weights, or GPUs. It checks
software behavior using fixtures; live provider and model qualification are
separate opt-in operations. `make help` lists preparation and deployment commands.

## CPU/GPU experiment

The [benchmark guide](docs/benchmark.md) explains how to run a comparison.
The [2026-10-15 experiment](benchmark/20261015T065506.188575Z-4331147244aa/README.md)
includes the machine description, methods, raw observations, results, and limits
on their interpretation. Reproduce its calculations offline:

```bash
make benchmark/verify RUN=benchmark/20261015T065506.188575Z-4331147244aa
```

This checks the archived evidence and report without repeating model inference.

## Retained-work diagnostics

For retained-work diagnostics on an already stopped deployment, run
`make provider/deployment/journal/inspect DEPLOYMENT=<name> DOCKER_CONTEXT=<name>`. The command holds
the deployment and provider identity locks and uses the installed provider image
to read its owned journal without network access. It prints canonical JSON with
operation identities, retained phases, fingerprints, restart behavior, and bounded
redacted diagnostic histories that survive obligation retirement; it
does not print request bodies or report delivery to the API. It rejects a running
deployment and does not stop, resume, retire, or modify retained work.

Add `ATTEMPT_REF=execution_attempt:sha256:<digest>` to select an Attempt's full
retained history. Default inspection includes at most one history and reports
how many others were omitted. Each history retains up to 32 events within
256 KiB; its omitted-event count makes truncation explicit. Histories remain
until explicit journal-volume retirement.

The [maximum transparency policy](notes/006_maximum_error_transparency_20261014.txt)
requires useful producer explanations and relevant evidence, with credentials
and other known sensitive material removed. Unverified model claims remain
attributed to the model.

[Safely archive a held report after API-confirmed closure](notes/002_closed_attempt_archival.txt).
