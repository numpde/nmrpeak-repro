PYTHON ?= python3
REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: check/source checkpoint/import checkpoint/recover provider/credential/install provider/deployment/config provider/deployment/down provider/deployment/generation/remove provider/deployment/init provider/deployment/journal/retire provider/deployment/status provider/deployment/up provider/identity-lock/remove provider/image/build provider/logs release/check release/write runner/image/build runner/lock/apply runner/lock/check runner/lock/stage test test/contract test/repository test/unit upstream-contracts/check upstream-contracts/write weights/check weights/download

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

weights/check:
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.nmrpeak_weights check "$(REPOSITORY_ROOT)"

weights/download: private export INTERFACE_INPUT := $(value INTERFACE)
weights/download:
	@test "$(origin INTERFACE)" = undefined -o "$(origin INTERFACE)" = command\ line || { echo 'INTERFACE must be set on the make command line' >&2; exit 2; }
	@test "$(origin INTERFACE)" = undefined -o -n "$$INTERFACE_INPUT" || { echo 'INTERFACE must not be empty when supplied' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.nmrpeak_weights download \
		"$(REPOSITORY_ROOT)" --interface "$$INTERFACE_INPUT"

upstream-contracts/check upstream-contracts/write: private export NMR_API_V1_DIR_INPUT := $(value NMR_API_V1_DIR)
upstream-contracts/check upstream-contracts/write: private export RELEASE_INPUT := $(value RELEASE)
upstream-contracts/check upstream-contracts/write:
	@test "$(origin NMR_API_V1_DIR)" = command\ line || { echo 'NMR_API_V1_DIR must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m repository_checks.nmr_api_projection "$(@F)" \
		"$(REPOSITORY_ROOT)" "$$NMR_API_V1_DIR_INPUT" "$$RELEASE_INPUT"

runner/lock/stage runner/lock/check runner/lock/apply:
	@test "$(origin TARGET)" = command\ line || { echo 'TARGET must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/runner-lock.sh" "$(@F)" "$(TARGET)"

runner/image/build:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin TARGET)" = command\ line || { echo 'TARGET must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/runner-image.sh" "$(RUNNER)" "$(TARGET)"

provider/image/build:
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/provider-image.sh"

provider/deployment/init: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/init:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment init "$$DEPLOYMENT_INPUT"

provider/deployment/config: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/config:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment config "$$DEPLOYMENT_INPUT"

provider/deployment/up: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/up:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment up "$$DEPLOYMENT_INPUT"

provider/deployment/status provider/deployment/down: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/status provider/deployment/down:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment "$(@F)" "$$DEPLOYMENT_INPUT"

provider/logs: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/logs:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment logs "$$DEPLOYMENT_INPUT"

provider/credential/install: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/credential/install: private export NMR_API_V1_DIR_INPUT := $(value NMR_API_V1_DIR)
provider/credential/install: private export REPLACE_INPUT := $(value REPLACE)
provider/credential/install:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@test "$(origin NMR_API_V1_DIR)" = command\ line || { echo 'NMR_API_V1_DIR must be set on the make command line' >&2; exit 2; }
	@test "$(origin REPLACE)" = undefined -o "$(origin REPLACE)" = command\ line || { echo 'REPLACE must be set on the make command line' >&2; exit 2; }
	@replace_flag=''; \
	if test -n "$$REPLACE_INPUT"; then \
		test "$$REPLACE_INPUT" = 1 || { echo 'REPLACE must be 1 when supplied' >&2; exit 2; }; \
		replace_flag=--replace; \
	fi; \
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment credential-install \
		"$$DEPLOYMENT_INPUT" --nmr-api-v1 "$$NMR_API_V1_DIR_INPUT" $$replace_flag

provider/identity-lock/remove: private export PROVIDER_REF_INPUT := $(value PROVIDER_REF)
provider/identity-lock/remove: private export CONFIRM_INPUT := $(value CONFIRM)
provider/identity-lock/remove:
	@test "$(origin PROVIDER_REF)" = command\ line || { echo 'PROVIDER_REF must be set on the make command line' >&2; exit 2; }
	@test "$(origin CONFIRM)" = command\ line || { echo 'CONFIRM must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_volumes identity-lock-remove \
		"$$PROVIDER_REF_INPUT" "$$CONFIRM_INPUT"

provider/deployment/generation/remove: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/generation/remove: private export FROZEN_GENERATION_INPUT := $(value FROZEN_GENERATION)
provider/deployment/generation/remove: private export CONFIRM_INPUT := $(value CONFIRM)
provider/deployment/generation/remove:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@test "$(origin FROZEN_GENERATION)" = command\ line || { echo 'FROZEN_GENERATION must be set on the make command line' >&2; exit 2; }
	@test "$(origin CONFIRM)" = command\ line || { echo 'CONFIRM must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment generation-remove \
		"$$DEPLOYMENT_INPUT" --frozen-generation "$$FROZEN_GENERATION_INPUT" \
		--confirm "$$CONFIRM_INPUT"

provider/deployment/journal/retire: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/journal/retire: private export CONFIRM_INPUT := $(value CONFIRM)
provider/deployment/journal/retire:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@test "$(origin CONFIRM)" = command\ line || { echo 'CONFIRM must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment journal-retire \
		"$$DEPLOYMENT_INPUT" --confirm "$$CONFIRM_INPUT"

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
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/checkpoint-volume.sh" \
		import "$(RUNNER)" "$(RELEASE)" "$(ARCHIVE)"

checkpoint/recover:
	@test "$(origin VOLUME)" = command\ line || { echo 'VOLUME must be set on the make command line' >&2; exit 2; }
	@test "$(origin CONFIRM)" = command\ line || { echo 'CONFIRM must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/checkpoint-volume.sh" \
		recover "$(VOLUME)" "$(CONFIRM)"
