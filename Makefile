PYTHON ?= python3
REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: check/source checkpoint/import checkpoint/recover provider/image/build release/check release/write runner/image/build runner/lock/apply runner/lock/check runner/lock/stage test test/contract test/repository test/unit

test: test/unit test/contract test/repository

test/unit:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover \
		-s "$(REPOSITORY_ROOT)/tests/unit" \
		-t "$(REPOSITORY_ROOT)" -v

test/contract:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover \
		-s "$(REPOSITORY_ROOT)/tests/contract" \
		-t "$(REPOSITORY_ROOT)" -v

test/repository:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover \
		-s "$(REPOSITORY_ROOT)/tests/repository" \
		-t "$(REPOSITORY_ROOT)" -v

check/source:
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.nmrpeak_source "$(REPOSITORY_ROOT)"

runner/lock/stage runner/lock/check runner/lock/apply:
	@test "$(origin TARGET)" = command\ line || { echo 'TARGET must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/runner-lock.sh" "$(@F)" "$(TARGET)"

runner/image/build:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin TARGET)" = command\ line || { echo 'TARGET must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/runner-image.sh" "$(RUNNER)" "$(TARGET)"

provider/image/build:
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/provider-image.sh"

release/write:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/checkpoint-release.sh" \
		write "$(RUNNER)" "$(RELEASE)" "$(ARCHIVE)"

release/check:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@test "$(origin DECLARATION)" = command\ line || { echo 'DECLARATION must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/checkpoint-release.sh" \
		check "$(RUNNER)" "$(RELEASE)" "$(ARCHIVE)" "$(DECLARATION)"

checkpoint/import:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(RUNNER)" = nmrpeak_chf_v1 || { echo 'checkpoint/import currently supports only nmrpeak_chf_v1' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.chf_checkpoint import \
			--runner "$(RUNNER)" --release "$(RELEASE)" --archive "$(ARCHIVE)"

checkpoint/recover:
	@test "$(origin VOLUME)" = command\ line || { echo 'VOLUME must be set on the make command line' >&2; exit 2; }
	@test "$(origin CONFIRM)" = command\ line || { echo 'CONFIRM must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.chf_checkpoint recover \
			--volume "$(VOLUME)" --confirm "$(CONFIRM)"
