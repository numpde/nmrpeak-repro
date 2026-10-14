# nmrpeak-repro

Deployment repository for an NMRPeak NMR API provider.

[Documentation](https://numpde.github.io/nmrpeak-repro/) — analysis offerings,
operator instructions, scientific references, and licensing.

For retained-work diagnostics on an already stopped deployment, run
`make provider/deployment/journal/inspect DEPLOYMENT=<name>`. The command holds
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
