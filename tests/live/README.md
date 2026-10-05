# Live failure propagation test

This opt-in test creates two persistent Jobs in an existing project, binds them
to one named NMRPeak provider, opens them, and verifies the exact public failure
contract for the HF and CHF lanes. It is excluded from `make test`.

The Make targets run in the same pinned test image as the default suite. They
mount one ignored-file-free source snapshot read-only, the signing credential
read-only, and only the owner-only state directory read-write. The container is
nonroot, capability-free, and given ordinary bridge networking for the selected
HTTPS endpoint. Prepare the image first with `make test-image/base/build
NMRPEAK_WIFI_INTERFACE=<name>`.

Because this container receives a signing key and network access, live targets
require a clean checkout and `LIVE_SOURCE_REVISION` equal to its reviewed HEAD.
Default container tests still admit dirty and explicit untracked source for
development.

Use an absolute path in a dedicated, owner-only directory for `LIVE_STATE`;
that directory may contain only this state file and its matching lock. Supply an
absolute, non-symlink user credential owned by the invoking user with mode 0400
or 0600. The state records exact Job-create
bytes before their first POST, then public references and evidence. It contains
no private key or signature. Keep an unresolved state file and rerun with the
same arguments: create replays are byte-identical, while provider selection and
opening reconcile signed reads before any further write.

```sh
mkdir -m 700 /tmp/nmrpeak-live
make test/live/failure-propagation \
  LIVE_API_ORIGIN=https://api.example.test \
  LIVE_API_TOPOLOGY=web \
  LIVE_USER_CREDENTIAL=/run/secrets/user-signing.private.json \
  LIVE_PROJECT_REF=project:user-demo \
  LIVE_PROVIDER_REF=provider:nmrpeak \
  LIVE_RUN_LABEL=reviewed-deployment-2026-10-05 \
  LIVE_SOURCE_REVISION=$(git rev-parse HEAD) \
  LIVE_STATE=/tmp/nmrpeak-live/state.json \
  CONFIRM_PERSISTENT_JOBS=1
```

`LIVE_RUN_LABEL` is operator evidence only. The test verifies the provider ref
observed on each Attempt, but cannot prove an image digest or that the provider
had no other eligible work. Set `LIVE_CA_CERTIFICATE` for a private CA. Use
`test/live/failure-propagation/observe` to recheck an existing state without
creating or opening Jobs.

## Positive success qualification

The positive companion creates a smaller admitted input in both lanes, waits
for exactly one successful Attempt per Job, follows every Analysis Result page,
reads the bound Result, and verifies its byte length, SHA-256 identity,
canonical JSON, candidate bounds, and lane-specific provider provenance.

```sh
mkdir -m 700 /tmp/nmrpeak-live-success
make test/live/success-propagation \
  LIVE_API_ORIGIN=https://api.example.test \
  LIVE_API_TOPOLOGY=web \
  LIVE_USER_CREDENTIAL=/run/secrets/user-signing.private.json \
  LIVE_PROJECT_REF=project:user-demo \
  LIVE_PROVIDER_REF=provider:nmrpeak \
  LIVE_RUN_LABEL=reviewed-deployment-2026-10-05 \
  LIVE_SOURCE_REVISION=$(git rev-parse HEAD) \
  LIVE_STATE=/tmp/nmrpeak-live-success/state.json \
  LIVE_EXPECTED_HF_CHECKPOINT=sha256:... \
  LIVE_EXPECTED_CHF_CHECKPOINT=sha256:... \
  LIVE_EXPECTED_HF_IMAGE_INPUT_ID=sha256:... \
  LIVE_EXPECTED_CHF_IMAGE_INPUT_ID=sha256:... \
  CONFIRM_PERSISTENT_JOBS=1
```

This lane invokes real model generation and defaults to a 15-minute wait. Its
owner-only state retains references, fingerprints, and candidate counts, but
does not copy result bodies or the signing credential. The four expected
artifact identities are required reviewed inputs; they bind successful output
to the intended checkpoint and runner image inputs instead of accepting any
syntactically valid deployment provenance.

The small input vector is copied from the repository's HF/CHF binding tests.
This proves deployed parsing, runner execution, completion, publication, and
readback. It does not claim that generated candidates are chemically correct;
scientific quality qualification remains a separate model evaluation.
