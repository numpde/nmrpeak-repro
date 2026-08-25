"""Prove startup sees one complete journal-owned Attempt inventory."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import unittest

from nmrpeak_provider.attempt_identity import derive_provider_attempt_key
from nmrpeak_provider.attempt_inventory import (
    AttemptInventory,
    AttemptInventoryReadFailed,
    AttemptInventoryRejected,
    read_attempt_inventory,
    validate_startup_inventory,
)
from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LocalExecutionPhase,
    StartPending,
)
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CODEC,
    CHF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.generation_runtime import GenerationLane, GenerationRuntime
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CODEC, HF_RUNNER_CONTRACT_ID
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    ProviderResultFacts,
)
from nmrpeak_provider.provider_https import (
    ProviderHttpResponse,
    ProviderRequestUnavailable,
    RequestDelivery,
)
from nmrpeak_provider.provider_success import InProgressAttempt
from nmrpeak_provider.run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    run_generation_fingerprint,
)


FROZEN_GENERATION_ID = "sha256:" + "1" * 64
HF_FACTS = ProviderResultFacts(
    identity=HF_RESULT_IDENTITY,
    runner_contract_id=HF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "2" * 64,
    image_input_ref="sha256:" + "3" * 64,
)
CHF_FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "4" * 64,
    image_input_ref="sha256:" + "5" * 64,
)


class CapturingApi:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    def send(self, request: object) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


class AttemptInventoryTests(unittest.TestCase):
    def test_reads_every_page_without_returning_partial_failure(self) -> None:
        runtime = generation_runtime()
        hf = pending_record(runtime.hf.generation, "job:hf", "6")
        chf = pending_record(runtime.chf.generation, "job:chf", "7")
        api = CapturingApi(
            inventory_response((live_attempt(runtime, hf, "8"),), "AAAA"),
            inventory_response((live_attempt(runtime, chf, "9"),), None),
        )

        inventory = read_attempt_inventory(api=api, maximum_pages=2)

        self.assertIs(type(inventory), AttemptInventory)
        self.assertEqual(len(inventory.attempts), 2)
        self.assertEqual(api.requests[0].query, "state=in_progress&limit=50")
        self.assertEqual(
            api.requests[1].query,
            "state=in_progress&limit=50&cursor=AAAA",
        )

        failed = read_attempt_inventory(
            api=CapturingApi(
                inventory_response((), "AAAA"),
                ProviderRequestUnavailable(RequestDelivery.NOT_SENT),
            ),
            maximum_pages=2,
        )
        self.assertIs(type(failed), AttemptInventoryReadFailed)

    def test_rejects_duplicate_identity_cursor_and_page_exhaustion(self) -> None:
        runtime = generation_runtime()
        record = pending_record(runtime.hf.generation, "job:hf", "6")
        attempt = live_attempt(runtime, record, "8")
        cases = (
            (
                CapturingApi(
                    inventory_response((attempt,), "AAAA"),
                    inventory_response((attempt,), None),
                ),
                2,
                "repeats an Attempt identity",
            ),
            (
                CapturingApi(
                    inventory_response((), "AAAA"),
                    inventory_response((), "AAAA"),
                ),
                2,
                "repeats a page cursor",
            ),
            (
                CapturingApi(inventory_response((), "AAAA")),
                1,
                "exceeds its configured page bound",
            ),
        )
        for api, maximum_pages, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                AttemptInventoryRejected,
                message,
            ):
                read_attempt_inventory(api=api, maximum_pages=maximum_pages)

    def test_startup_inventory_accepts_owned_work_and_missing_bound_work(self) -> None:
        runtime = generation_runtime()
        hf = pending_record(runtime.hf.generation, "job:hf", "6")
        chf_pending = pending_record(runtime.chf.generation, "job:chf", "7")
        chf = ActiveAttempt(
            job_ref=chf_pending.job_ref,
            provider_attempt_key=chf_pending.provider_attempt_key,
            input_fingerprint=chf_pending.input_fingerprint,
            frozen_generation_id=chf_pending.frozen_generation_id,
            execution_attempt_ref="execution_attempt:sha256:" + "9" * 64,
            local_phase=LocalExecutionPhase.PRE_EXECUTION,
        )
        inventory = AttemptInventory((live_attempt(runtime, hf, "8"),))

        validate_startup_inventory(
            runtime=runtime,
            records=(hf, chf),
            inventory=inventory,
        )

    def test_startup_inventory_rejects_server_only_and_drifted_work(self) -> None:
        runtime = generation_runtime()
        record = pending_record(runtime.hf.generation, "job:hf", "6")
        owned = live_attempt(runtime, record, "8")
        cases = (
            AttemptInventory((
                InProgressAttempt(
                    owned.analysis_kind_ref,
                    owned.execution_attempt_ref,
                    owned.job_ref,
                    "nmrpeak-provider.v1:" + "a" * 64,
                    owned.started_at,
                ),
            )),
            AttemptInventory((
                InProgressAttempt(
                    CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                    owned.execution_attempt_ref,
                    owned.job_ref,
                    owned.provider_attempt_key,
                    owned.started_at,
                ),
            )),
        )
        for inventory in cases:
            with self.subTest(inventory=inventory), self.assertRaises(
                AttemptInventoryRejected
            ):
                validate_startup_inventory(
                    runtime=runtime,
                    records=(record,),
                    inventory=inventory,
                )


def generation_runtime() -> GenerationRuntime:
    return GenerationRuntime(
        frozen_generation_id=FROZEN_GENERATION_ID,
        hf=GenerationLane(
            HF_LIFECYCLE_LANE,
            generation(HF_LIFECYCLE_LANE.offering.analysis_kind_ref, "hf-generation"),
            HF_FACTS,
            HF_RUNNER_CODEC,
        ),
        chf=GenerationLane(
            CHF_LIFECYCLE_LANE,
            generation(CHF_LIFECYCLE_LANE.offering.analysis_kind_ref, "chf-generation"),
            CHF_FACTS,
            CHF_RUNNER_CODEC,
        ),
    )


def generation(analysis_kind_ref: str, generation_id: str) -> RunGenerationIdentity:
    return RunGenerationIdentity(
        provider_ref="provider:nmrpeak",
        analysis_kind_ref=analysis_kind_ref,
        generation_id=generation_id,
        scope=CreatedAtWindow(datetime(2026, 8, 24, tzinfo=UTC)),
    )


def pending_record(
    generation: RunGenerationIdentity,
    job_ref: str,
    fingerprint_digit: str,
) -> StartPending:
    input_fingerprint = "sha256:" + fingerprint_digit * 64
    return StartPending(
        job_ref=job_ref,
        provider_attempt_key=derive_provider_attempt_key(
            provider_ref=generation.provider_ref,
            run_generation_fingerprint=run_generation_fingerprint(generation),
            job_ref=job_ref,
            input_fingerprint=input_fingerprint,
        ),
        input_fingerprint=input_fingerprint,
        frozen_generation_id=FROZEN_GENERATION_ID,
    )


def live_attempt(
    runtime: GenerationRuntime,
    record: StartPending,
    reference_digit: str,
) -> InProgressAttempt:
    resolved = runtime.resolve(record)
    return InProgressAttempt(
        analysis_kind_ref=resolved.lane.offering.analysis_kind_ref,
        execution_attempt_ref="execution_attempt:sha256:" + reference_digit * 64,
        job_ref=record.job_ref,
        provider_attempt_key=record.provider_attempt_key,
        started_at="2026-08-24T12:00:00Z",
    )


def inventory_response(
    attempts: tuple[InProgressAttempt, ...],
    next_cursor: str | None,
) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        status=200,
        topology="dev-local",
        content_type="application/json",
        request_id=None,
        body=json.dumps(
            {
                "schema_id": "nmr.provider.execution_attempts.list.response.v1",
                "attempts": [
                    {
                        "analysis_kind_ref": attempt.analysis_kind_ref,
                        "execution_attempt_ref": attempt.execution_attempt_ref,
                        "job_ref": attempt.job_ref,
                        "provider_attempt_key": attempt.provider_attempt_key,
                        "state": "in_progress",
                        "started_at": attempt.started_at,
                    }
                    for attempt in attempts
                ],
                "next_cursor": next_cursor,
            },
            separators=(",", ":"),
        ).encode(),
    )


if __name__ == "__main__":
    unittest.main()
