PYTHON ?= python3
REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: check/source release/check release/write test test/contract test/repository test/unit

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

release/write:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.chf_release write \
			--runner "$(RUNNER)" --release "$(RELEASE)" --archive "$(ARCHIVE)"

release/check:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@test "$(origin DECLARATION)" = command\ line || { echo 'DECLARATION must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.chf_release check \
			--runner "$(RUNNER)" --release "$(RELEASE)" --archive "$(ARCHIVE)" \
			--declaration "$(DECLARATION)"
