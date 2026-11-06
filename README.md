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
`make test DOCKER_CONTEXT=<name>`. Otherwise, choose a local Unix-socket Docker
context and an available network interface, or use `system` for the default
route. Create the wheelhouse directory first and have the pinned Python base
image available on that Docker engine; the acquisition target does not pull it:

```bash
git submodule update --init --recursive
make wheelhouse/tool WHEELHOUSE=/absolute/path/to/wheelhouse NETWORK_INTERFACE=system
make provider/wheelhouse/acquire WHEELHOUSE=/absolute/path/to/wheelhouse NETWORK_INTERFACE=system DOCKER_CONTEXT=<name>
make test-image/base/build WHEELHOUSE=/absolute/path/to/wheelhouse NETWORK_INTERFACE=system DOCKER_CONTEXT=<name>
make test DOCKER_CONTEXT=<name>
```

Builds and dependency acquisition ask for a route when one is needed and not
supplied. A complete wheelhouse can be checked without a network route. Choose
a listed interface, or `system` for the default route; there is no default
selection. Unattended commands that need a route must set `NETWORK_INTERFACE`
on the Make command line. Interface-bound dependency downloads use a temporary
proxy for tools that honor it; it is not an egress firewall. Docker base and
frontend image acquisition uses the engine's route; test-image base bootstrap
uses Podman's default connection.
Image preparation reuses an existing matching image.
`make test DOCKER_CONTEXT=<name>` runs without network, API credentials, weights, or GPUs. It checks
software behavior using fixtures; live provider and model qualification are
separate opt-in operations. `make help` lists preparation and deployment commands.

`make test/integration DOCKER_CONTEXT=<name>` includes NMRPeak's normal stop
while its prose interpreter is waiting for an HTTPS response. To qualify a
candidate `provider-core` wheel that starts `provider_core.chat_worker`, run
`python -m unittest -v tests.sdk_candidate.prose_http_helper_shutdown` from the
checkout in a test environment with that wheel installed. This also stops
NMRPeak during helper startup and holds the helper's exit notification to
check that NMRPeak waits for it before reporting shutdown complete.

To build and update one named deployment, run
`make provider/deployment/update DEPLOYMENT=<name> WHEELHOUSE=<directory> DOCKER_CONTEXT=<name>`.
The command builds from the clean checkout on the selected local Docker engine,
prints the resolved plan, drains the provider, then starts that exact plan. It
requires the provider to exit cleanly before stopping runners or removing their
temporary sessions. It retains the shared proxy and outbox, credentials,
provider journal, checkpoints, and frozen generations, then reports the image
IDs verified on the running provider and runners. Plain `provider/deployment/up`
does not build and still requires the reviewed plan hash.

Interpreter endpoints are read in filename order from
`secrets/deployments/<name>/openai-chat-completions.d`. Each `.toml` file names
the complete chat-completions URL, model, and a private sibling key file:

```toml
url = "https://model.example/v1/chat/completions"
model = "model-name"
key_file = "model.key"
```

An endpoint may also specify `ca_file` and `reasoning_effort`. Key and CA
references must be sibling filenames. The deployment captures only endpoint
files and the files they reference. Migrate older endpoint files containing
`id`, `base_url`, or an inline `api_key` to this format before updating the
deployment. The `[interpreter]` section in `provider.toml` now sets the single
`interpretation_timeout_seconds`; remove the old request and turn timeouts.

`make packages/images/retire DOCKER_CONTEXT=<name>` removes obsolete owned
images from the selected local engine. It retains current inputs, image IDs
recorded by retained generations and build captures, images used by running or
stopped containers, and images with unfamiliar aliases or digests. It also keeps
images created in the last 24 hours. Creation time is an age buffer, not a
guarantee that a recently tagged cached image is reserved for 24 hours. Use a
deployment pin or a separate intentional alias to retain an image across source
changes.

Retirement coordinates with maintained image work in this checkout. Other
checkouts and direct Docker operations do not take that lock and must be kept
out of the maintenance window. Interrupted builds may leave diagnostic captures
in `.work/image-builds`; preserve them until their outcome is understood.

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
