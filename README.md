# nmrpeak-repro

Deployment repository for an NMRPeak NMR API provider.

[Documentation](https://numpde.github.io/nmrpeak-repro/) — analysis offerings,
operator instructions, scientific references, and licensing.

For retained-work diagnostics on an already stopped deployment, run
`make provider/deployment/journal/inspect DEPLOYMENT=<name>`. The command holds
the deployment and provider identity locks and uses the installed provider image
to read its owned journal without network access. It prints canonical JSON with
operation identities, retained phases, fingerprints, and restart behavior; it
does not print request bodies or report delivery to the API. It rejects a running
deployment and does not stop, resume, retire, or modify retained work.

[Safely archive a held report after API-confirmed closure](notes/002_closed_attempt_archival.txt).
