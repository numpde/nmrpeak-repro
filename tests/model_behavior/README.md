# NMRPeak interpreter model behavior

This opt-in harness adapts Magnet's `tests/model_behavior/run_interpreter.py` to
the production NMRPeak HF and CHF prompt, tool schema, constructor, endpoint
adapter, repair loop, and timeout policy. Its fixtures are synthetic and contain
only values explicit in the source text. It checks exact admitted values,
missing-data reports, an injected protocol-repair turn, a native constructor
rejection for an unsupported multiplicity, candidate-only exhaustion or a
source-grounded report, and an
instruction embedded in caller text. Offline tests repeat the native rejection
and prove fallback from a failed endpoint to a source-faithful second endpoint.

Validate the local corpus and offline harness without credentials or network calls:

```sh
make test/model-behavior-fixtures
```

This Make target uses the prepared pinned test image described in the
[software-check instructions](../../README.md#check-the-software). It validates
both fixture lists and runs the offline harness tests.

The direct Python entry point requires Python 3.11+ and the provider dependencies
from `requirements.lock` in its environment. With that environment prepared, use
`--list` to validate either lane without network access, or run live against an
explicitly supplied provider-format endpoint configuration directory:

```sh
PYTHONPATH=. python tests/model_behavior/run_interpreter.py \
  --lane hf --config-dir /absolute/path/to/openai-chat-completions.d
```

The runner prints only safe case IDs, outcome classifications, counts, and
durations by default. `--show-model-output` deliberately prints raw assistant
turns to the terminal and should be used only in a suitable private session.
Only fixture validation and offline harness tests run in the default `make test`
lane. Live endpoint calls are opt-in; use `--repeats N` for repeated live
observations. Offline checks do not qualify any live model.
Model-authored unsupported-source reports require semantic review even when
they mention the expected source value. The harness prints `REVIEW` and exits
with status 2 for those cases; status 1 means an automated check failed.
For qualification, enable `--show-model-output` on that same invocation and
capture stdout to a private file created with mode 0600 (for example, with
`mktemp`). The `REVIEW_MESSAGE` line retains the exact model report even when
the adapter's assistant-message envelope omits tool arguments. Inspect it and
record the human judgement.
A `REVIEW` from a run without captured output remains unresolved; a later run
is a different observation.
