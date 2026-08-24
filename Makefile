PYTHON ?= python3
REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: check/source test test/contract test/repository test/unit

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
