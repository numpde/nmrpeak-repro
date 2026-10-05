override SHELL := bash
override .SHELLFLAGS := -eu -o pipefail -c
PYTHON ?= python3
override REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
override NMRPEAK_TEST_UID := $(shell id -u)
override NMRPEAK_TEST_GID := $(shell id -g)
override NMRPEAK_TEST_CHECKOUT_KEY := $(shell printf '%s' "$(REPOSITORY_ROOT)" | sha256sum | cut -c1-12)
override NMRPEAK_TEST_BASE_KEY := $(shell "$(REPOSITORY_ROOT)/scripts/test-image-key.sh" "$(REPOSITORY_ROOT)")
override NMRPEAK_TEST_IMAGE := nmrpeak-repro/python-test:$(NMRPEAK_TEST_BASE_KEY)
override NMRPEAK_TEST_PROJECT := nmrpeak-repro-test-$(NMRPEAK_TEST_UID)-$(NMRPEAK_TEST_CHECKOUT_KEY)
override NMRPEAK_TEST_COMPOSE := env \
	NMRPEAK_TEST_IMAGE="$(NMRPEAK_TEST_IMAGE)" \
	NMRPEAK_TEST_UID="$(NMRPEAK_TEST_UID)" \
	NMRPEAK_TEST_GID="$(NMRPEAK_TEST_GID)" \
	NMRPEAK_TEST_CHECKOUT="$(REPOSITORY_ROOT)" \
	docker --context default compose --env-file /dev/null \
	-p "$(NMRPEAK_TEST_PROJECT)" -f compose/test.yml
override NMRPEAK_TEST_NON_ROOT_GUARD = if [[ "$(NMRPEAK_TEST_UID)" == 0 || "$(NMRPEAK_TEST_GID)" == 0 || "$(NMRPEAK_TEST_UID)" == 65532 ]]; then printf '%s\n' 'Containerized tests require a non-root invoking user distinct from provider UID 65532.' >&2; exit 2; fi
.DEFAULT_GOAL := help

.PHONY: help check/source check/test-image checkpoint/import checkpoint/recover provider/credential/install provider/deployment/config provider/deployment/config/localhost provider/deployment/down provider/deployment/generation/remove provider/deployment/init provider/deployment/journal/inspect provider/deployment/journal/retire provider/deployment/status provider/deployment/up provider/deployment/up/localhost provider/identity-lock/remove provider/image/build provider/logs release/check release/install release/write runner/image/build runner/lock/apply runner/lock/check runner/lock/stage test test-image/base/build test/contract test/integration test/live/failure-propagation test/live/failure-propagation/observe test/live/success-propagation test/live/success-propagation/observe test/model-behavior-fixtures test/repository test/unit upstream-contracts/check upstream-contracts/write weights/check weights/download

help:
	@printf '%s\n' \
		'NMR API provider' \
		'' \
		'Verify this checkout:' \
		'  make test' \
		'      Run the complete default lane in the pinned, networkless test container.' \
		'      One read-only source snapshot excludes credentials, deployment state, weights, and ignored files.' \
		'  make test-image/base/build NMRPEAK_WIFI_INTERFACE=<name>' \
		'      Prepare the content-keyed dependency image; apt, pip, and base pulls use the Wi-Fi-bound proxy.' \
		'      Ordinary test targets never pull images or use the network.' \
		'  make test/unit' \
		'  make test/contract' \
		'  make test/integration' \
		'  make test/model-behavior-fixtures' \
		'  make test/repository' \
		'      Run one part of the default lane.' \
		'  make test/live/failure-propagation ... CONFIRM_PERSISTENT_JOBS=1' \
		'      Opt in to persistent HF/CHF Jobs against a deployed API; see tests/live/README.md.' \
		'  make test/live/failure-propagation/observe ...' \
		'      Recheck the signed API evidence in an existing owner-only state file.' \
		'  make test/live/success-propagation ... CONFIRM_PERSISTENT_JOBS=1' \
		'      Opt in to successful HF/CHF generation and Analysis Result verification.' \
		'  make test/live/success-propagation/observe ...' \
		'      Recheck successful Attempts and Results without creating or opening Jobs.' \
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
		'  make provider/deployment/up DEPLOYMENT=<name> EXPECTED_PLAN_SHA256=<config-output-sha256>' \
		'      Load the reviewed checkpoints and start signed API activity using public trust.' \
		'  make provider/deployment/config/localhost DEPLOYMENT=<name> LOCALHOST_CA_CERTIFICATE=<path>' \
		'      Validate and render a same-host private-CA deployment without starting it.' \
		'  make provider/deployment/up/localhost DEPLOYMENT=<name> LOCALHOST_CA_CERTIFICATE=<path> EXPECTED_PLAN_SHA256=<config-output-sha256>' \
		'      Load the reviewed checkpoints and start signed API activity using the supplied private CA.' \
		'  make provider/deployment/status DEPLOYMENT=<name>' \
		'      Report the owned provider and runner container state.' \
		'  make provider/deployment/journal/inspect DEPLOYMENT=<name>' \
		'  make provider/deployment/journal/archive-closed DEPLOYMENT=<name> ATTEMPT_REF=<ref> RECORD_DIGEST=<sha256> FROZEN_GENERATION=<sha256> REASON=<text>' \
		'      Archive reviewed held work only after API-confirmed closure; see notes/002_closed_attempt_archival.txt.' \
		'      Inspect retained work in a stopped deployment without publishing or changing it.' \
		'  make provider/logs DEPLOYMENT=<name>' \
		'      Follow logs from the running owned provider.' \
		'  make provider/deployment/down DEPLOYMENT=<name>' \
		'      Stop the deployment; preserve config, credentials, journals, generations, images, checkpoint volumes, and identity locks.' \
		'' \
		'Exceptional removal:' \
		'  make provider/deployment/generation/remove DEPLOYMENT=<name> FROZEN_GENERATION=<id> CONFIRM=<id>' \
		'      Remove one unreferenced frozen generation after exact confirmation.' \
		'  make provider/deployment/journal/retire DEPLOYMENT=<name> CONFIRM=<full-journal-volume-name>' \
		'      Delete the entire stopped journal, including retained commands and archives, after exact volume confirmation.' \
		'  make provider/identity-lock/remove PROVIDER_REF=<provider-ref> CONFIRM=<full-journal-volume-name>' \
		'      Remove one unused provider identity lock after exact confirmation.' \
		'  There is no blanket cleanup target.'

test: check/test-image
	@"$(REPOSITORY_ROOT)/scripts/container-test.sh" \
		"$(REPOSITORY_ROOT)" "$(NMRPEAK_TEST_IMAGE)" "$(NMRPEAK_TEST_PROJECT)" all

test-image/base/build: private export NMRPEAK_WIFI_INTERFACE_INPUT := $(value NMRPEAK_WIFI_INTERFACE)
test-image/base/build:
	@test "$(origin NMRPEAK_WIFI_INTERFACE)" = command\ line -a -n "$$NMRPEAK_WIFI_INTERFACE_INPUT" || { echo 'NMRPEAK_WIFI_INTERFACE must be set on the make command line' >&2; exit 2; }
	@"$(REPOSITORY_ROOT)/scripts/test-image-base.sh" \
		"$(REPOSITORY_ROOT)" "$$NMRPEAK_WIFI_INTERFACE_INPUT"

check/test-image:
	@$(NMRPEAK_TEST_NON_ROOT_GUARD); \
	endpoint="$$(docker --context default context inspect default --format '{{.Endpoints.docker.Host}}')"; \
	case "$$endpoint" in unix:///*) ;; *) echo "Docker context default is not local: $$endpoint" >&2; exit 2 ;; esac; \
	image_id="$$(docker --context default image ls --quiet --no-trunc "$(NMRPEAK_TEST_IMAGE)")"; \
	if [[ -z "$$image_id" ]]; then \
		printf '%s\n' 'The exact local test dependency image is absent.' \
			'Run: make test-image/base/build NMRPEAK_WIFI_INTERFACE=wlp10s0' >&2; exit 2; \
	fi; \
	observed="$$(docker --context default image inspect "$$image_id" --format '{{index .Config.Labels "io.numpde.nmrpeak.test-base-key"}}')"; \
	if [[ "$$observed" != "$(NMRPEAK_TEST_BASE_KEY)" ]]; then \
		echo 'The local test image label does not match its requested dependency identity.' >&2; exit 2; \
	fi; \
	$(NMRPEAK_TEST_COMPOSE) config --quiet

test/unit test/contract test/integration test/repository test/model-behavior-fixtures: check/test-image
	@"$(REPOSITORY_ROOT)/scripts/container-test.sh" \
		"$(REPOSITORY_ROOT)" "$(NMRPEAK_TEST_IMAGE)" "$(NMRPEAK_TEST_PROJECT)" "$(@F)"

test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_API_ORIGIN_INPUT := $(value LIVE_API_ORIGIN)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_API_TOPOLOGY_INPUT := $(value LIVE_API_TOPOLOGY)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_USER_CREDENTIAL_INPUT := $(value LIVE_USER_CREDENTIAL)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_PROJECT_REF_INPUT := $(value LIVE_PROJECT_REF)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_PROVIDER_REF_INPUT := $(value LIVE_PROVIDER_REF)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_RUN_LABEL_INPUT := $(value LIVE_RUN_LABEL)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_STATE_INPUT := $(value LIVE_STATE)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_CA_CERTIFICATE_INPUT := $(value LIVE_CA_CERTIFICATE)
test/live/failure-propagation test/live/failure-propagation/observe: private export LIVE_SOURCE_REVISION_INPUT := $(value LIVE_SOURCE_REVISION)
test/live/failure-propagation test/live/failure-propagation/observe: private export CONFIRM_PERSISTENT_JOBS_INPUT := $(value CONFIRM_PERSISTENT_JOBS)
test/live/failure-propagation test/live/failure-propagation/observe: check/test-image
test/live/failure-propagation test/live/failure-propagation/observe:
	@test "$(origin LIVE_API_ORIGIN)" = command\ line || { echo 'LIVE_API_ORIGIN must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_API_TOPOLOGY)" = command\ line || { echo 'LIVE_API_TOPOLOGY must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_USER_CREDENTIAL)" = command\ line || { echo 'LIVE_USER_CREDENTIAL must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_PROJECT_REF)" = command\ line || { echo 'LIVE_PROJECT_REF must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_PROVIDER_REF)" = command\ line || { echo 'LIVE_PROVIDER_REF must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_RUN_LABEL)" = command\ line || { echo 'LIVE_RUN_LABEL must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_STATE)" = command\ line || { echo 'LIVE_STATE must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_SOURCE_REVISION)" = command\ line || { echo 'LIVE_SOURCE_REVISION must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_CA_CERTIFICATE)" = undefined -o "$(origin LIVE_CA_CERTIFICATE)" = command\ line || { echo 'LIVE_CA_CERTIFICATE must be set on the make command line' >&2; exit 2; }
	@if test "$@" = test/live/failure-propagation; then \
		test "$(origin CONFIRM_PERSISTENT_JOBS)" = command\ line -a "$$CONFIRM_PERSISTENT_JOBS_INPUT" = 1 || { echo 'set CONFIRM_PERSISTENT_JOBS=1 to create persistent Jobs' >&2; exit 2; }; \
	fi
	@set -- "$$(test "$@" = test/live/failure-propagation && printf run || printf observe)" \
		--api-origin "$$LIVE_API_ORIGIN_INPUT" --expected-topology "$$LIVE_API_TOPOLOGY_INPUT" \
		--credential /run/nmrpeak-live/credential.json --project-ref "$$LIVE_PROJECT_REF_INPUT" \
		--provider-ref "$$LIVE_PROVIDER_REF_INPUT" --run-label "$$LIVE_RUN_LABEL_INPUT" \
		--state "/run/nmrpeak-live/state/$$(basename -- "$$LIVE_STATE_INPUT")"; \
	if test -n "$$LIVE_CA_CERTIFICATE_INPUT"; then set -- "$$@" --ca-certificate /run/nmrpeak-live/ca.pem; fi; \
	if test "$@" = test/live/failure-propagation; then set -- "$$@" --confirm-persistent-jobs; fi; \
	"$(REPOSITORY_ROOT)/scripts/container-live-test.sh" \
		"$(REPOSITORY_ROOT)" "$(NMRPEAK_TEST_IMAGE)" "$(NMRPEAK_TEST_PROJECT)" \
		"$$LIVE_SOURCE_REVISION_INPUT" "$$LIVE_USER_CREDENTIAL_INPUT" \
		"$$LIVE_STATE_INPUT" "$$LIVE_CA_CERTIFICATE_INPUT" \
		tests.live.failure_propagation -- "$$@"

test/live/success-propagation test/live/success-propagation/observe: private export LIVE_API_ORIGIN_INPUT := $(value LIVE_API_ORIGIN)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_API_TOPOLOGY_INPUT := $(value LIVE_API_TOPOLOGY)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_USER_CREDENTIAL_INPUT := $(value LIVE_USER_CREDENTIAL)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_PROJECT_REF_INPUT := $(value LIVE_PROJECT_REF)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_PROVIDER_REF_INPUT := $(value LIVE_PROVIDER_REF)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_RUN_LABEL_INPUT := $(value LIVE_RUN_LABEL)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_STATE_INPUT := $(value LIVE_STATE)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_CA_CERTIFICATE_INPUT := $(value LIVE_CA_CERTIFICATE)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_EXPECTED_HF_CHECKPOINT_INPUT := $(value LIVE_EXPECTED_HF_CHECKPOINT)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_EXPECTED_CHF_CHECKPOINT_INPUT := $(value LIVE_EXPECTED_CHF_CHECKPOINT)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_EXPECTED_HF_IMAGE_INPUT_ID_INPUT := $(value LIVE_EXPECTED_HF_IMAGE_INPUT_ID)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_EXPECTED_CHF_IMAGE_INPUT_ID_INPUT := $(value LIVE_EXPECTED_CHF_IMAGE_INPUT_ID)
test/live/success-propagation test/live/success-propagation/observe: private export LIVE_SOURCE_REVISION_INPUT := $(value LIVE_SOURCE_REVISION)
test/live/success-propagation test/live/success-propagation/observe: private export CONFIRM_PERSISTENT_JOBS_INPUT := $(value CONFIRM_PERSISTENT_JOBS)
test/live/success-propagation test/live/success-propagation/observe: check/test-image
test/live/success-propagation test/live/success-propagation/observe:
	@test "$(origin LIVE_API_ORIGIN)" = command\ line || { echo 'LIVE_API_ORIGIN must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_API_TOPOLOGY)" = command\ line || { echo 'LIVE_API_TOPOLOGY must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_USER_CREDENTIAL)" = command\ line || { echo 'LIVE_USER_CREDENTIAL must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_PROJECT_REF)" = command\ line || { echo 'LIVE_PROJECT_REF must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_PROVIDER_REF)" = command\ line || { echo 'LIVE_PROVIDER_REF must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_RUN_LABEL)" = command\ line || { echo 'LIVE_RUN_LABEL must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_STATE)" = command\ line || { echo 'LIVE_STATE must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_SOURCE_REVISION)" = command\ line || { echo 'LIVE_SOURCE_REVISION must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_EXPECTED_HF_CHECKPOINT)" = command\ line || { echo 'LIVE_EXPECTED_HF_CHECKPOINT must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_EXPECTED_CHF_CHECKPOINT)" = command\ line || { echo 'LIVE_EXPECTED_CHF_CHECKPOINT must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_EXPECTED_HF_IMAGE_INPUT_ID)" = command\ line || { echo 'LIVE_EXPECTED_HF_IMAGE_INPUT_ID must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_EXPECTED_CHF_IMAGE_INPUT_ID)" = command\ line || { echo 'LIVE_EXPECTED_CHF_IMAGE_INPUT_ID must be set on the make command line' >&2; exit 2; }
	@test "$(origin LIVE_CA_CERTIFICATE)" = undefined -o "$(origin LIVE_CA_CERTIFICATE)" = command\ line || { echo 'LIVE_CA_CERTIFICATE must be set on the make command line' >&2; exit 2; }
	@if test "$@" = test/live/success-propagation; then \
		test "$(origin CONFIRM_PERSISTENT_JOBS)" = command\ line -a "$$CONFIRM_PERSISTENT_JOBS_INPUT" = 1 || { echo 'set CONFIRM_PERSISTENT_JOBS=1 to create persistent Jobs' >&2; exit 2; }; \
	fi
	@set -- "$$(test "$@" = test/live/success-propagation && printf run || printf observe)" \
		--api-origin "$$LIVE_API_ORIGIN_INPUT" --expected-topology "$$LIVE_API_TOPOLOGY_INPUT" \
		--credential /run/nmrpeak-live/credential.json --project-ref "$$LIVE_PROJECT_REF_INPUT" \
		--provider-ref "$$LIVE_PROVIDER_REF_INPUT" --run-label "$$LIVE_RUN_LABEL_INPUT" \
		--state "/run/nmrpeak-live/state/$$(basename -- "$$LIVE_STATE_INPUT")" \
		--expected-hf-checkpoint "$$LIVE_EXPECTED_HF_CHECKPOINT_INPUT" \
		--expected-chf-checkpoint "$$LIVE_EXPECTED_CHF_CHECKPOINT_INPUT" \
		--expected-hf-image-input "$$LIVE_EXPECTED_HF_IMAGE_INPUT_ID_INPUT" \
		--expected-chf-image-input "$$LIVE_EXPECTED_CHF_IMAGE_INPUT_ID_INPUT"; \
	if test -n "$$LIVE_CA_CERTIFICATE_INPUT"; then set -- "$$@" --ca-certificate /run/nmrpeak-live/ca.pem; fi; \
	if test "$@" = test/live/success-propagation; then set -- "$$@" --confirm-persistent-jobs; fi; \
	"$(REPOSITORY_ROOT)/scripts/container-live-test.sh" \
		"$(REPOSITORY_ROOT)" "$(NMRPEAK_TEST_IMAGE)" "$(NMRPEAK_TEST_PROJECT)" \
		"$$LIVE_SOURCE_REVISION_INPUT" "$$LIVE_USER_CREDENTIAL_INPUT" \
		"$$LIVE_STATE_INPUT" "$$LIVE_CA_CERTIFICATE_INPUT" \
		tests.live.success_propagation -- "$$@"

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

runner/lock/stage runner/lock/check runner/lock/apply: private export NMRPEAK_WIFI_INTERFACE_INPUT := $(value NMRPEAK_WIFI_INTERFACE)
runner/lock/stage runner/lock/check runner/lock/apply:
	@test "$(origin TARGET)" = command\ line || { echo 'TARGET must be set on the make command line' >&2; exit 2; }
	@test "$(origin NMRPEAK_WIFI_INTERFACE)" = undefined -o "$(origin NMRPEAK_WIFI_INTERFACE)" = command\ line || { echo 'NMRPEAK_WIFI_INTERFACE must be set on the make command line' >&2; exit 2; }
	@test "$(origin NMRPEAK_WIFI_INTERFACE)" = undefined -o -n "$$NMRPEAK_WIFI_INTERFACE_INPUT" || { echo 'NMRPEAK_WIFI_INTERFACE must not be empty when supplied' >&2; exit 2; }
	@NMRPEAK_WIFI_INTERFACE="$$NMRPEAK_WIFI_INTERFACE_INPUT" PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/runner-lock.sh" "$(@F)" "$(TARGET)"

runner/image/build: private export NMRPEAK_WIFI_INTERFACE_INPUT := $(value NMRPEAK_WIFI_INTERFACE)
runner/image/build:
	@test "$(origin RUNNER)" = command\ line || { echo 'RUNNER must be set on the make command line' >&2; exit 2; }
	@test "$(origin TARGET)" = command\ line || { echo 'TARGET must be set on the make command line' >&2; exit 2; }
	@test "$(origin NMRPEAK_WIFI_INTERFACE)" = undefined -o "$(origin NMRPEAK_WIFI_INTERFACE)" = command\ line || { echo 'NMRPEAK_WIFI_INTERFACE must be set on the make command line' >&2; exit 2; }
	@test "$(origin NMRPEAK_WIFI_INTERFACE)" = undefined -o -n "$$NMRPEAK_WIFI_INTERFACE_INPUT" || { echo 'NMRPEAK_WIFI_INTERFACE must not be empty when supplied' >&2; exit 2; }
	@NMRPEAK_WIFI_INTERFACE="$$NMRPEAK_WIFI_INTERFACE_INPUT" PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/runner-image.sh" "$(RUNNER)" "$(TARGET)"

provider/image/build: private export NMRPEAK_WIFI_INTERFACE_INPUT := $(value NMRPEAK_WIFI_INTERFACE)
provider/image/build:
	@test "$(origin NMRPEAK_WIFI_INTERFACE)" = undefined -o "$(origin NMRPEAK_WIFI_INTERFACE)" = command\ line || { echo 'NMRPEAK_WIFI_INTERFACE must be set on the make command line' >&2; exit 2; }
	@test "$(origin NMRPEAK_WIFI_INTERFACE)" = undefined -o -n "$$NMRPEAK_WIFI_INTERFACE_INPUT" || { echo 'NMRPEAK_WIFI_INTERFACE must not be empty when supplied' >&2; exit 2; }
	@NMRPEAK_WIFI_INTERFACE="$$NMRPEAK_WIFI_INTERFACE_INPUT" PYTHON="$(PYTHON)" "$(REPOSITORY_ROOT)/scripts/provider-image.sh"

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
provider/deployment/up: private export EXPECTED_PLAN_SHA256_INPUT := $(value EXPECTED_PLAN_SHA256)
provider/deployment/up:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@test "$(origin EXPECTED_PLAN_SHA256)" = command\ line || { echo 'EXPECTED_PLAN_SHA256 must be set on the make command line from the reviewed config output' >&2; exit 2; }
	@test "$(origin LOCALHOST_CA_CERTIFICATE)" != command\ line || { echo 'LOCALHOST_CA_CERTIFICATE is accepted only by provider/deployment/config/localhost or provider/deployment/up/localhost' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment up "$$DEPLOYMENT_INPUT" \
		--expected-plan-sha256 "$$EXPECTED_PLAN_SHA256_INPUT"

provider/deployment/config/localhost provider/deployment/up/localhost: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/config/localhost provider/deployment/up/localhost: private export LOCALHOST_CA_CERTIFICATE_INPUT := $(value LOCALHOST_CA_CERTIFICATE)
provider/deployment/up/localhost: private export EXPECTED_PLAN_SHA256_INPUT := $(value EXPECTED_PLAN_SHA256)
provider/deployment/config/localhost provider/deployment/up/localhost:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@test "$(origin LOCALHOST_CA_CERTIFICATE)" = command\ line || { echo 'LOCALHOST_CA_CERTIFICATE must be set on the make command line' >&2; exit 2; }
	@test "$@" != provider/deployment/up/localhost || test "$(origin EXPECTED_PLAN_SHA256)" = command\ line || { echo 'EXPECTED_PLAN_SHA256 must be set on the make command line from the reviewed config output' >&2; exit 2; }
	@operation="$(notdir $(@D))"; \
		set -- "$$operation" "$$DEPLOYMENT_INPUT" --localhost-ca-certificate "$$LOCALHOST_CA_CERTIFICATE_INPUT"; \
		if [ "$$operation" = up ]; then set -- "$$@" --expected-plan-sha256 "$$EXPECTED_PLAN_SHA256_INPUT"; fi; \
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment "$$@"

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

provider/deployment/journal/inspect: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/journal/inspect:
	@test "$(origin DEPLOYMENT)" = command\ line || { echo 'DEPLOYMENT must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(REPOSITORY_ROOT)" \
		$(PYTHON) -m deployment.provider_deployment journal-inspect "$$DEPLOYMENT_INPUT"

.PHONY: provider/deployment/journal/archive-closed
provider/deployment/journal/archive-closed: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/journal/archive-closed: private export ATTEMPT_REF_INPUT := $(value ATTEMPT_REF)
provider/deployment/journal/archive-closed: private export RECORD_DIGEST_INPUT := $(value RECORD_DIGEST)
provider/deployment/journal/archive-closed: private export ARCHIVE_REASON_INPUT := $(value REASON)
provider/deployment/journal/archive-closed: private export FROZEN_GENERATION_INPUT := $(value FROZEN_GENERATION)
provider/deployment/journal/archive-closed: private export LOCALHOST_CA_INPUT := $(value LOCALHOST_CA_CERTIFICATE)
provider/deployment/journal/archive-closed:
	@cd "$(CURDIR)" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(CURDIR)" \
		$(PYTHON) -m deployment.provider_deployment journal-archive-closed "$$DEPLOYMENT_INPUT" \
		--execution-attempt-ref "$$ATTEMPT_REF_INPUT" --record-digest "$$RECORD_DIGEST_INPUT" \
		--reason "$$ARCHIVE_REASON_INPUT" --frozen-generation "$$FROZEN_GENERATION_INPUT" \
		$${LOCALHOST_CA_INPUT:+--localhost-ca-certificate "$${LOCALHOST_CA_INPUT}"}

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

# Current shared failure interpretation has its own source and API revisions;
# the historical RELEASE projection above keeps its original meaning.
.PHONY: provider-client/check provider-client/write
provider-client/check provider-client/write: private export NMR_API_V1_DIR_INPUT := $(value NMR_API_V1_DIR)
provider-client/check provider-client/write:
	@test "$(origin NMR_API_V1_DIR)" = command\ line || { echo 'NMR_API_V1_DIR must be set on the make command line' >&2; exit 2; }
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) "$$NMR_API_V1_DIR_INPUT/clients/provider_python/project.py" "$(@F)" \
		--api-repository "$$NMR_API_V1_DIR_INPUT" \
		--package "$(REPOSITORY_ROOT)/nmrpeak_provider" \
		--manifest "$(REPOSITORY_ROOT)/contracts/upstream/provider_client.json"
