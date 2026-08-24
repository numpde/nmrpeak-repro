# Isolated NMRPeak test environment

This setup treats both the upstream source and every `.pt` checkpoint as
untrusted. Nothing in these instructions loads a checkpoint on the host.

## Safety boundary

The default container has no network, a read-only root filesystem, no Linux
capabilities, `no-new-privileges`, bounded processes/CPU/memory, and only
ephemeral `tmpfs` storage. The NMRPeak checkout is baked into the image rather
than bind-mounted. The default smoke test does not mount the downloaded archive
or extracted checkpoints and replaces common pickle/PyTorch loaders with a
function that raises.

Checkpoint services are opt-in. They mount extracted models read-only and can
write only to a Docker-managed output volume. They have no host-directory bind
mounts and no network.

Containers reduce risk but are not a perfect VM boundary. The CPU checkpoint
service exposes less host attack surface. The GPU service additionally exposes
the NVIDIA driver; use a disposable VM instead if the checkpoint may be
actively malicious.

## Build and smoke-test without weights

Compose deliberately does not change host routes. For large build-time package
downloads, the optional wrapper starts a temporary loopback proxy whose
outbound sockets are bound to `wlp10s0`. Only that build invocation uses the
proxy, and the proxy is stopped automatically afterward:

```bash
./bin/docker-compose-build-wifi smoke
```

Set `NMRPEAK_WIFI_INTERFACE` to use another interface. Docker's own small base
image/BuildKit metadata pulls occur before Dockerfile build steps and therefore
still follow the daemon route; the large PyTorch and Python dependency downloads
inside the build use the bound proxy. Runtime services are networkless.

```bash
docker compose build
docker compose run --rm smoke
```

Building installs CPU-only PyTorch, the upstream dependency set, and pinned
Uni-Core inside image build steps. It does not include `weights/` in the build
context. The much larger CUDA dependency set is downloaded only when the GPU
service is explicitly built.

## Extract weights without touching them on the host

The guarded extractor validates member paths and types, rejects ZIP traversal,
symlinks and oversized members, and writes only the allowlisted HF generation
checkpoint into a Docker-managed volume:

```bash
docker compose --profile setup run --rm extract-weights
```

A failed partial extraction is intentionally not reused; recreate only the
`nmrpeak-hf-weights` volume before retrying.

## Run checkpoint code explicitly

First run the same non-loading smoke test with the extracted volume attached:

```bash
docker compose --profile checkpoint run --rm checkpoint-cpu
```

Any command that actually invokes NMRPeak checkpoint loading must be explicit.
For example, start a networkless CPU shell inside the boundary:

```bash
docker compose --profile checkpoint run --rm checkpoint-cpu bash
```

Inside it, checkpoints are under `/models/current`, output belongs under
`/output`, and the source is `/opt/nmrpeak`. Do not copy checkpoints back out
and load them on the host.

GPU access is a separate opt-in profile:

```bash
docker compose build checkpoint-gpu
docker compose --profile gpu run --rm checkpoint-gpu bash
```

Adjust resource ceilings with `NMRPEAK_MEMORY_LIMIT` and `NMRPEAK_CPU_LIMIT`.
For example:

```bash
NMRPEAK_MEMORY_LIMIT=48g NMRPEAK_CPU_LIMIT=12 docker compose run --rm smoke
```
