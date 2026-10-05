# Live failure propagation test

This opt-in test creates two persistent Jobs in an existing project, binds them
to one named NMRPeak provider, opens them, and verifies the exact public failure
contract for the HF and CHF lanes. It is excluded from `make test`.

Use an owner-only directory for `LIVE_STATE`. The state records exact Job-create
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
  LIVE_STATE=/tmp/nmrpeak-live/state.json \
  CONFIRM_PERSISTENT_JOBS=1
```

`LIVE_RUN_LABEL` is operator evidence only. The test verifies the provider ref
observed on each Attempt, but cannot prove an image digest or that the provider
had no other eligible work. Set `LIVE_CA_CERTIFICATE` for a private CA. Use
`test/live/failure-propagation/observe` to recheck an existing state without
creating or opening Jobs.
