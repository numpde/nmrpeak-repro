PYTHON ?= python3
REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
.DEFAULT_GOAL := help

.PHONY: help check/source checkpoint/import checkpoint/recover provider/credential/install provider/deployment/config provider/deployment/config/localhost provider/deployment/down provider/deployment/generation/remove provider/deployment/init provider/deployment/journal/retire provider/deployment/status provider/deployment/up provider/deployment/up/localhost provider/identity-lock/remove provider/image/build provider/logs release/check release/install release/write runner/image/build runner/lock/apply runner/lock/check runner/lock/stage test test/contract test/repository test/unit upstream-contracts/check upstream-contracts/write weights/check weights/download

help:
	@printf '%s\n' \
		'NMR API provider' \
		'' \
		'Verify this checkout:' \
		'  make test [PYTHON=<prepared-python>]' \
		'      Run the networkless, credential-free, checkpoint-free default lane.' \
		'      Requires the dependencies in requirements.lock to be installed; does not install them.' \
		'  make test/unit' \
		'  make test/contract' \
		'  make test/repository' \
		'      Run one part of the default lane.' \
		'  make check/source' \
		'      Verify the pinned NMRPeak and Uni-Core source closure.' \
		'  make upstream-contracts/check NMR_API_V1_DIR=<path> RELEASE=<revision>' \
		'      Check the committed NMR API contract projection.' \
		'  make upstream-contracts/write NMR_API_V1_DIR=<path> RELEASE=<revision>' \
		'      Replace that projection after review of the selected API revision.' \
		'' \
		'Prepare public checkpoints:' \
		'  make weights/download [INTERFACE=<name>]' \
		'      Resume the pinned Zenodo download and verify its size and MD5; omit INTERFACE for normal routing.' \
		'  make weights/check' \
		'      Verify the complete local archive without network access.' \
		'  make release/write RUNNER=<runner> RELEASE=<name> ARCHIVE=<zip>' \
		'      Print a candidate declaration without changing the checkout.' \
		'  make release/check RUNNER=<runner> RELEASE=<name> ARCHIVE=<zip> DECLARATION=<json>' \
		'      Verify the named release declaration and its selected archive member.' \
		'  make release/install RUNNER=<runner> RELEASE=<name> ARCHIVE=<zip> DECLARATION=<json>' \
		'      Install a reviewed declaration without replacement.' \
		'  make checkpoint/import RUNNER=<runner> RELEASE=<name> ARCHIVE=<zip>' \
		'      Stream the checkpoint named by the installed release into its Docker volume without loading it.' \
		'  make checkpoint/recover VOLUME=<volume> CONFIRM=<volume>' \
		'      Repair an interrupted checkpoint volume after exact confirmation.' \
		'' \
		'Build provider and runner images:' \
		'  make provider/image/build [NMRPEAK_WIFI_INTERFACE=<name>]' \
		'      Build the provider from a clean committed checkout; an explicit interface binds dependency downloads to Wi-Fi.' \
		'  make runner/lock/stage TARGET=<target> [NMRPEAK_WIFI_INTERFACE=<name>]' \
		'      Resolve dependencies and stage a candidate outside the checkout; an explicit interface binds downloads to Wi-Fi.' \
		'  make runner/lock/check TARGET=<target>' \
		'      Verify the committed dependency lock without network access.' \
		'  make runner/lock/apply TARGET=<target>' \
		'      Replace the committed lock with the verified staged candidate.' \
		'  make runner/image/build RUNNER=<runner> TARGET=<target> [NMRPEAK_WIFI_INTERFACE=<name>]' \
		'      Build one runner image from committed inputs; an explicit interface binds dependency downloads to Wi-Fi.' \
		'' \
		'Operate a named deployment:' \
		'  make provider/deployment/init DEPLOYMENT=<name>' \
		'      Create the configuration and private credential scaffold without installing a credential.' \
		'  make provider/credential/install DEPLOYMENT=<name> NMR_API_V1_DIR=<path> [REPLACE=1]' \
		'      Install the matching API-issued private provider credential.' \
		'  make provider/deployment/config DEPLOYMENT=<name>' \
		'      Validate and render a public-trust deployment without starting it.' \
		'  make provider/deployment/up DEPLOYMENT=<name>' \
		'      Load the reviewed checkpoints and start signed API activity using public trust.' \
		'  make provider/deployment/config/localhost DEPLOYMENT=<name> LOCALHOST_CA_CERTIFICATE=<path>' \
		'      Validate and render a same-host private-CA deployment without starting it.' \
		'  make provider/deployment/up/localhost DEPLOYMENT=<name> LOCALHOST_CA_CERTIFICATE=<path>' \
		'      Load the reviewed checkpoints and start signed API activity using the supplied private CA.' \
		'  make provider/deployment/status DEPLOYMENT=<name>' \
		'      Report the owned provider and runner container state.' \
		'  make provider/logs DEPLOYMENT=<name>' \
		'      Follow logs from the running owned provider.' \
		'  make provider/deployment/down DEPLOYMENT=<name>' \
		'      Stop the deployment; preserve config, credentials, journals, generations, images, checkpoint volumes, and identity locks.' \
		'' \
		'Exceptional removal:' \
		'  make provider/deployment/generation/remove DEPLOYMENT=<name> FROZEN_GENERATION=<id> CONFIRM=<id>' \
		'      Remove one unreferenced frozen generation after exact confirmation.' \
		'  make provider/deployment/journal/retire DEPLOYMENT=<name> CONFIRM=<provider-ref>' \
		'      Remove one empty stopped deployment journal after exact confirmation.' \
		'  make provider/identity-lock/remove PROVIDER_REF=<provider-ref> CONFIRM=<provider-ref>' \
		'      Remove one unused provider identity lock after exact confirmation.' \
		'  There is no blanket cleanup target.'

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
	@test "$(origin LOCALHOST_CA_CERTIFICATE)" != command\ line || { echo 'LOCALHOST_CA_CERTIFICATE is accepted only by provider/deployment/config/localhost or provider/deployment/up/localhost' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment config "$$DEPLOYMENT_INPUT"

provider/deployment/up: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/up:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@test "$(origin LOCALHOST_CA_CERTIFICATE)" != command\ line || { echo 'LOCALHOST_CA_CERTIFICATE is accepted only by provider/deployment/config/localhost or provider/deployment/up/localhost' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment up "$$DEPLOYMENT_INPUT"

provider/deployment/config/localhost provider/deployment/up/localhost: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/config/localhost provider/deployment/up/localhost: private export LOCALHOST_CA_CERTIFICATE_INPUT := $(value LOCALHOST_CA_CERTIFICATE)
provider/deployment/config/localhost provider/deployment/up/localhost:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@test "$(origin LOCALHOST_CA_CERTIFICATE)" = command\ line || { echo 'LOCALHOST_CA_CERTIFICATE must be set on the make command line' >&2; exit 2; }
	@operation="$(notdir $(@D))"; \
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment "$$operation" "$$DEPLOYMENT_INPUT" \
		--localhost-ca-certificate "$$LOCALHOST_CA_CERTIFICATE_INPUT"

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

release/write release/check release/install: private export RUNNER_INPUT := $(value RUNNER)
release/write release/check release/install: private export RELEASE_INPUT := $(value RELEASE)
release/write release/check release/install: private export ARCHIVE_INPUT := $(value ARCHIVE)
release/check release/install: private export DECLARATION_INPUT := $(value DECLARATION)

release/write:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/checkpoint-release.sh" \
		write "$$RUNNER_INPUT" "$$RELEASE_INPUT" "$$ARCHIVE_INPUT"

release/check:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@test "$(origin DECLARATION)" = command\ line || { echo 'DECLARATION must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/checkpoint-release.sh" \
		check "$$RUNNER_INPUT" "$$RELEASE_INPUT" "$$ARCHIVE_INPUT" "$$DECLARATION_INPUT"

release/install:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin RELEASE)" = command\ line || { echo 'RELEASE must be set on the make command line' >&2; exit 2; }
	@test "$(origin ARCHIVE)" = command\ line || { echo 'ARCHIVE must be set on the make command line' >&2; exit 2; }
	@test "$(origin DECLARATION)" = command\ line || { echo 'DECLARATION must be set on the make command line' >&2; exit 2; }
	@PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/checkpoint-release.sh" \
		install "$$RUNNER_INPUT" "$$RELEASE_INPUT" "$$ARCHIVE_INPUT" "$$DECLARATION_INPUT"

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
