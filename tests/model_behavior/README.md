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
PYTHONPATH=. python tests/model_behavior/run_interpreter.py --lane hf --list
PYTHONPATH=. python tests/model_behavior/run_interpreter.py --lane chf --list
make test/model-behavior-fixtures
```

Run against an explicitly supplied provider-format endpoint configuration
directory:

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
