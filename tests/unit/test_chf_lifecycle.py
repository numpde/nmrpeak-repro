"""Prove CHF admission becomes durable before any Attempt start can exist."""

from __future__ import annotations

from base64 import b64encode
from contextlib import contextmanager
from datetime import datetime, UTC
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nmrpeak_provider.attempt_identity import derive_provider_attempt_key
from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LocalExecutionPhase,
    StartPending,
)
from nmrpeak_provider.attempt_journal_store import (
    AttemptJournalAdmissionRejected,
    AttemptJournalStore,
)
from nmrpeak_provider.chf_lifecycle import (
    ChfFeedReadFailed,
    ChfInputReadFailed,
    ChfJobAdmitted,
    ChfPageExhausted,
    ChfStartContinues,
    ChfStartResolved,
    admit_next_chf_job,
    start_chf_attempt,
)
from nmrpeak_provider.provider_outcomes import (
    AttemptMutationCommitPossible,
    AttemptMutationNotCommitted,
)
from nmrpeak_provider.provider_https import (
    ProviderHttpResponse,
    ProviderOperation,
    ProviderRequestUnavailable,
    RequestDelivery,
)
from nmrpeak_provider.provider_success import (
    ProviderSuccessRejected,
    SuccessRejection,
)
from nmrpeak_provider.run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    run_generation_fingerprint,
)


FROZEN_GENERATION_ID = "sha256:" + "4" * 64


class CapturingApi:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    def send(self, request: object) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


class ChfLifecycleTests(unittest.TestCase):
    def test_first_in_window_job_is_durably_admitted_without_starting(self) -> None:
        generation = chf_generation()
        canonical_input = b"{}"
        fingerprint = fingerprint_of(canonical_input)
        api = CapturingApi(
            success_response(
                jobs_page(
                    job_item("job:old", "2026-08-23T23:59:59Z", "0"),
                    job_item("job:selected", "2026-08-24T00:00:00Z", fingerprint),
                    job_item("job:later", "2026-08-25T00:00:00Z", "1"),
                )
            ),
            success_response(job_input("job:selected", canonical_input)),
        )

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                outcome = admit_next_chf_job(
                    api=api,
                    journal=journal,
                    generation=generation,
                    frozen_generation_id=FROZEN_GENERATION_ID,
                )
            expected = StartPending(
                job_ref="job:selected",
                provider_attempt_key=derive_provider_attempt_key(
                    provider_ref=generation.provider_ref,
                    run_generation_fingerprint=run_generation_fingerprint(generation),
                    job_ref="job:selected",
                    input_fingerprint=fingerprint,
                ),
                input_fingerprint=fingerprint,
                frozen_generation_id=FROZEN_GENERATION_ID,
            )
            self.assertEqual(outcome, ChfJobAdmitted(expected, canonical_input))
            with AttemptJournalStore(root, maximum_records=1) as reopened:
                self.assertEqual(reopened.records(), (expected,))

        self.assertEqual(
            [request.operation for request in api.requests],
            [ProviderOperation.JOBS_LIST, ProviderOperation.JOB_INPUT_READ],
        )
        self.assertEqual(
            api.requests[0].query,
            "analysis_kind_ref=mol_from_1h_13c_formula"
            "&has_provider_execution_attempt=false&limit=50",
        )

    def test_page_exhaustion_preserves_cursor_and_exclusive_window_end(self) -> None:
        api = CapturingApi(
            success_response(
                jobs_page(
                    job_item("job:at-end", "2026-08-26T00:00:00Z", "0"),
                    next_cursor="bmV4dA",
                )
            )
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                outcome = admit_next_chf_job(
                    api=api,
                    journal=journal,
                    generation=chf_generation(),
                    frozen_generation_id=FROZEN_GENERATION_ID,
                )
                self.assertEqual(journal.records(), ())

        self.assertEqual(outcome, ChfPageExhausted("bmV4dA"))
        self.assertEqual(len(api.requests), 1)

    def test_feed_or_input_drift_never_reaches_journal_admission(self) -> None:
        valid_input = b"{}"
        fingerprint = fingerprint_of(valid_input)
        scenarios = (
            (
                CapturingApi(
                    success_response(
                        jobs_page(
                            job_item(
                                "job:selected",
                                "2026-08-24T00:00:00Z",
                                fingerprint,
                            )
                        )
                        | {"analysis_kind_ref": "mol_from_1h_peaks"}
                    )
                ),
                ChfFeedReadFailed(
                    ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT)
                ),
                1,
            ),
            (
                CapturingApi(
                    success_response(
                        jobs_page(
                            job_item(
                                "job:selected",
                                "2026-08-24T00:00:00Z",
                                fingerprint,
                            )
                        )
                    ),
                    success_response(
                        job_input("job:selected", valid_input)
                        | {"input_byte_length": len(valid_input) + 1}
                    ),
                ),
                ChfInputReadFailed(
                    ProviderSuccessRejected(SuccessRejection.INVALID_FIELD)
                ),
                2,
            ),
        )
        for api, expected, request_count in scenarios:
            with self.subTest(expected=expected), journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    self.assertEqual(
                        admit_next_chf_job(
                            api=api,
                            journal=journal,
                            generation=chf_generation(),
                            frozen_generation_id=FROZEN_GENERATION_ID,
                        ),
                        expected,
                    )
                    self.assertEqual(journal.records(), ())
                self.assertEqual(len(api.requests), request_count)

    def test_journal_capacity_rejection_is_not_disguised_as_api_success(self) -> None:
        canonical_input = b"{}"
        fingerprint = fingerprint_of(canonical_input)
        api = CapturingApi(
            success_response(
                jobs_page(
                    job_item(
                        "job:selected",
                        "2026-08-24T00:00:00Z",
                        fingerprint,
                    )
                )
            ),
            success_response(job_input("job:selected", canonical_input)),
        )
        existing = StartPending(
            job_ref="job:existing",
            provider_attempt_key="nmrpeak-provider.v1:" + "a" * 64,
            input_fingerprint="sha256:" + "b" * 64,
            frozen_generation_id=FROZEN_GENERATION_ID,
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(existing)
                with self.assertRaises(AttemptJournalAdmissionRejected):
                    admit_next_chf_job(
                        api=api,
                        journal=journal,
                        generation=chf_generation(),
                        frozen_generation_id=FROZEN_GENERATION_ID,
                    )
                self.assertEqual(journal.records(), (existing,))

        self.assertTrue(
            all(
                request.operation is not ProviderOperation.EXECUTION_ATTEMPT_START
                for request in api.requests
            )
        )

    def test_transport_failure_is_returned_as_feed_evidence(self) -> None:
        unavailable = ProviderRequestUnavailable(RequestDelivery.NOT_SENT)
        api = CapturingApi(unavailable)
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                self.assertEqual(
                    admit_next_chf_job(
                        api=api,
                        journal=journal,
                        generation=chf_generation(),
                        frozen_generation_id=FROZEN_GENERATION_ID,
                    ),
                    ChfFeedReadFailed(unavailable),
                )
                self.assertEqual(journal.records(), ())

    def test_invalid_frozen_generation_fails_before_the_feed_read(self) -> None:
        api = CapturingApi()
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                with self.assertRaisesRegex(ValueError, "frozen generation"):
                    admit_next_chf_job(
                        api=api,
                        journal=journal,
                        generation=chf_generation(),
                        frozen_generation_id="not-a-content-address",
                    )
                self.assertEqual(journal.records(), ())
        self.assertEqual(api.requests, [])

    def test_fresh_and_replayed_in_progress_starts_become_durable(self) -> None:
        for replayed in (False, True):
            with self.subTest(replayed=replayed), journal_directory() as root:
                generation = chf_generation()
                pending = pending_start(generation)
                api = CapturingApi(
                    success_response(start_receipt("in_progress", replayed=replayed))
                )
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    journal.admit(pending)
                    outcome = start_chf_attempt(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=FROZEN_GENERATION_ID,
                        record=pending,
                    )
                active = ActiveAttempt(
                    job_ref=pending.job_ref,
                    provider_attempt_key=pending.provider_attempt_key,
                    input_fingerprint=pending.input_fingerprint,
                    frozen_generation_id=pending.frozen_generation_id,
                    execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
                    local_phase=LocalExecutionPhase.PRE_EXECUTION,
                )
                self.assertEqual(outcome, ChfStartContinues(active))
                with AttemptJournalStore(root, maximum_records=1) as reopened:
                    self.assertEqual(reopened.records(), (active,))
                self.assertEqual(
                    [request.operation for request in api.requests],
                    [ProviderOperation.EXECUTION_ATTEMPT_START],
                )

    def test_replayed_terminal_start_retires_without_local_execution(self) -> None:
        for state in ("succeeded", "failed", "expired"):
            with self.subTest(state=state), journal_directory() as root:
                generation = chf_generation()
                pending = pending_start(generation)
                api = CapturingApi(success_response(start_receipt(state, replayed=True)))
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    journal.admit(pending)
                    outcome = start_chf_attempt(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=FROZEN_GENERATION_ID,
                        record=pending,
                    )
                    self.assertEqual(journal.records(), ())
                self.assertIs(type(outcome), ChfStartResolved)
                self.assertEqual(outcome.receipt.state.value, state)

    def test_uncertain_start_outcomes_retain_the_exact_pending_record(self) -> None:
        cases = (
            (
                ProviderRequestUnavailable(RequestDelivery.NOT_SENT),
                AttemptMutationNotCommitted,
            ),
            (
                ProviderRequestUnavailable(RequestDelivery.POSSIBLE),
                AttemptMutationCommitPossible,
            ),
        )
        for evidence, expected_type in cases:
            with self.subTest(expected_type=expected_type), journal_directory() as root:
                generation = chf_generation()
                pending = pending_start(generation)
                api = CapturingApi(evidence)
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    journal.admit(pending)
                    outcome = start_chf_attempt(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=FROZEN_GENERATION_ID,
                        record=pending,
                    )
                    self.assertIs(type(outcome), expected_type)
                    self.assertEqual(journal.records(), (pending,))
                self.assertEqual(len(api.requests), 1)

    def test_fresh_terminal_receipt_requires_reconciliation(self) -> None:
        generation = chf_generation()
        pending = pending_start(generation)
        api = CapturingApi(success_response(start_receipt("succeeded", replayed=False)))
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending)
                outcome = start_chf_attempt(
                    api=api,
                    journal=journal,
                    generation=generation,
                    frozen_generation_id=FROZEN_GENERATION_ID,
                    record=pending,
                )
                self.assertIs(type(outcome), AttemptMutationCommitPossible)
                self.assertEqual(journal.records(), (pending,))

    def test_wrong_start_campaign_fails_before_network_activity(self) -> None:
        generation = chf_generation()
        pending = StartPending(
            job_ref="job:selected",
            provider_attempt_key="nmrpeak-provider.v1:" + "f" * 64,
            input_fingerprint="sha256:" + "b" * 64,
            frozen_generation_id=FROZEN_GENERATION_ID,
        )
        api = CapturingApi()
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending)
                with self.assertRaisesRegex(ValueError, "run generation"):
                    start_chf_attempt(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=FROZEN_GENERATION_ID,
                        record=pending,
                    )
                self.assertEqual(journal.records(), (pending,))
        self.assertEqual(api.requests, [])


def chf_generation() -> RunGenerationIdentity:
    return RunGenerationIdentity(
        provider_ref="provider:nmrpeak",
        analysis_kind_ref="mol_from_1h_13c_formula",
        generation_id="chf-generation",
        scope=CreatedAtWindow(
            datetime(2026, 8, 24, tzinfo=UTC),
            datetime(2026, 8, 26, tzinfo=UTC),
        ),
    )


def pending_start(generation: RunGenerationIdentity) -> StartPending:
    input_fingerprint = "sha256:" + "b" * 64
    return StartPending(
        job_ref="job:selected",
        provider_attempt_key=derive_provider_attempt_key(
            provider_ref=generation.provider_ref,
            run_generation_fingerprint=run_generation_fingerprint(generation),
            job_ref="job:selected",
            input_fingerprint=input_fingerprint,
        ),
        input_fingerprint=input_fingerprint,
        frozen_generation_id=FROZEN_GENERATION_ID,
    )


def start_receipt(state: str, *, replayed: bool) -> dict[str, object]:
    return {
        "schema_id": "nmr.provider.execution_attempt_start_response.v1",
        "execution_attempt_ref": "execution_attempt:sha256:" + "a" * 64,
        "job_ref": "job:selected",
        "analysis_kind_ref": "mol_from_1h_13c_formula",
        "provider_ref": "provider:nmrpeak",
        "state": state,
        "started_at": "2026-08-24T12:00:00Z",
        "replayed": replayed,
    }


def jobs_page(
    *jobs: dict[str, object],
    next_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "schema_id": "nmr.provider.jobs.list.response.v1",
        "analysis_kind_ref": "mol_from_1h_13c_formula",
        "has_provider_execution_attempt": False,
        "jobs": list(jobs),
        "next_cursor": next_cursor,
    }


def job_item(
    job_ref: str,
    created_at: str,
    fingerprint: str,
) -> dict[str, object]:
    normalized_fingerprint = fingerprint
    if not fingerprint.startswith("sha256:"):
        normalized_fingerprint = "sha256:" + fingerprint * 64
    return {
        "job_ref": job_ref,
        "analysis_kind_ref": "mol_from_1h_13c_formula",
        "input_fingerprint": normalized_fingerprint,
        "input_schema_id": "nmr.job.specification.text.v1",
        "input_byte_length": 2,
        "created_at": created_at,
    }


def job_input(job_ref: str, canonical_input: bytes) -> dict[str, object]:
    return {
        "schema_id": "nmr.provider.job_input.read.response.v1",
        "job_ref": job_ref,
        "input_fingerprint": fingerprint_of(canonical_input),
        "input_schema_id": "nmr.job.specification.text.v1",
        "input_byte_length": len(canonical_input),
        "canonical_input_base64": b64encode(canonical_input).decode("ascii"),
    }


def fingerprint_of(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def success_response(document: dict[str, object]) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        status=200,
        topology="dev-local",
        content_type="application/json",
        request_id=None,
        body=json.dumps(document, separators=(",", ":")).encode("utf-8"),
    )


@contextmanager
def journal_directory():
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "journal"
        path.mkdir(mode=0o700)
        yield path


if __name__ == "__main__":
    unittest.main()
