PYTHON ?= python3
REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: test test/contract

test: test/contract

test/contract:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover \
		-s "$(REPOSITORY_ROOT)/tests/contract" \
		-t "$(REPOSITORY_ROOT)" -v
