#!/bin/sh
set -eu

# The previous lock is a preference seed, not a constraint: reviewed intent
# changes may replace direct pins while unrelated resolved versions stay stable.
cp /seed/requirements.lock /out/requirements.lock
exec uv pip compile "$@"
