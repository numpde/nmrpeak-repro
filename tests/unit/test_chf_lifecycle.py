"""Prove CHF admission becomes durable before any Attempt start can exist."""

from __future__ import annotations

from base64 import b64decode, b64encode
from contextlib import contextmanager
from datetime import datetime, UTC
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from nmrpeak_provider.attempt_identity import derive_provider_attempt_key
from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LocalExecutionPhase,
    ObserveUntilExpiry,
    RetainTerminalConflict,
    StartPending,
    TerminalOperation,
    TerminalPending,
    retain_terminal_command,
)
from nmrpeak_provider.attempt_journal_store import (
    AttemptJournalAdmissionRejected,
    AttemptJournalStore,
)
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.chf_lifecycle import (
    ChfCandidatesGenerated,
    ChfCompletionPending,
    ChfExecutionCutOff,
    ChfExecutionResolved,
    ChfExecutionShutdownFailed,
    ChfFeedReadFailed,
    ChfAttemptObserved,
    ChfAttemptObservationFailed,
    ChfInputReadFailed,
    ChfInterruptedFailurePending,
    ChfObservationLost,
    ChfObservationPolicy,
    ChfJobAdmitted,
    ChfPageExhausted,
    ChfInputFailurePending,
    ChfPreparedForExecution,
    ChfRecoveryResolved,
    ChfRecoveryResumes,
    ChfStartContinues,
    ChfStartResolved,
    ChfTerminalDelivered,
    admit_next_chf_job,
    deliver_chf_terminal,
    execute_prepared_chf,
    observe_chf_attempt,
    prepare_chf_execution,
    reconcile_chf_record,
    select_chf_completion,
    start_chf_attempt,
)
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CODEC,
    CHF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.runner_protocol import (
    ReadyFrame,
    ValidateFrame,
)
from nmrpeak_provider.runner_session import (
    RunnerDeadlines,
    RunnerSession,
    GeneratedRunnerCandidates,
    ValidatedRunnerRequest,
)
from nmrpeak_provider.chf_binding import (
    ChfRunnerCarbonPeak,
    ChfRunnerInput,
)
from nmrpeak_provider.nmrpeak_binding import RunnerProtonPeak
from nmrpeak_provider.product_input import InputRejected
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
    RESULT_SCHEMA_ID,
    RunnerResultRejected,
)
from nmrpeak_provider.provider_outcomes import (
    AttemptMutationCommitPossible,
    AttemptMutationNotCommitted,
)
from nmrpeak_provider.provider_requests import (
    prepare_execution_attempt_complete,
    prepare_execution_attempt_fail,
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
from tests.fakes.runner import FakeRunnerChannel, FakeRunnerFault


FROZEN_GENERATION_ID = "sha256:" + "4" * 64
RUNNER_FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "5" * 64,
    image_input_ref="sha256:" + "6" * 64,
)


class CapturingApi:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    def send(self, request: object) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


class UnusedSession:
    def validate(self, **values: object) -> object:
        raise AssertionError("CHF validation must not occur")


class NonStoppingSession:
    def __init__(self) -> None:
        self.release = Event()
        self.finished = Event()

    def generate(self, request: object) -> object:
        try:
            self.release.wait()
            return object()
        finally:
            self.finished.set()

    def cancel(self) -> None:
        pass


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

    def test_preparing_receipt_then_runner_validation_stops_before_generation(self) -> None:
        canonical_input = valid_chf_input()
        active = active_attempt(canonical_input)
        api = CapturingApi(success_response(progress_receipt()))
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame())
        session = RunnerSession.admit(
            channel,
            RUNNER_FACTS,
            RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1),
            CHF_RUNNER_CODEC,
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(active))
                journal.replace(pending_from_active(active), active)
                outcome = prepare_chf_execution(
                    api=api,
                    journal=journal,
                    session=session,
                    record=active,
                    canonical_input=canonical_input,
                )
                self.assertEqual(journal.records(), (active,))

        self.assertIs(type(outcome), ChfPreparedForExecution)
        self.assertEqual(outcome.record, active)
        self.assertEqual(
            [request.operation for request in api.requests],
            [ProviderOperation.EXECUTION_ATTEMPT_PROGRESS],
        )
        self.assertEqual(
            api.requests[0].body,
            canonical_json_bytes({
                "schema_id": "nmr.provider.execution_attempt_progress_request.v1",
                "phase": "preparing",
                "condition_code": None,
            }),
        )
        self.assertEqual(len(channel.received_frames), 1)
        self.assertIs(type(channel.received_frames[0]), ValidateFrame)

    def test_product_rejection_becomes_one_durable_fixed_failure(self) -> None:
        canonical_input = b"{}"
        active = active_attempt(canonical_input)
        api = CapturingApi()
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(active))
                journal.replace(pending_from_active(active), active)
                outcome = prepare_chf_execution(
                    api=api,
                    journal=journal,
                    session=UnusedSession(),
                    record=active,
                    canonical_input=canonical_input,
                )
            self.assertIs(type(outcome), ChfInputFailurePending)
            with AttemptJournalStore(root, maximum_records=1) as reopened:
                self.assertEqual(reopened.records(), (outcome.record,))
        terminal_body = json.loads(outcome.record.terminal_request_body)
        self.assertEqual(terminal_body["failure_code"], "input_rejected")
        self.assertEqual(
            terminal_body["failure_message"],
            InputRejected.public_message,
        )
        self.assertEqual(api.requests, [])

    def test_runner_rejection_uses_the_same_durable_failure_policy(self) -> None:
        canonical_input = valid_chf_input()
        active = active_attempt(canonical_input)
        api = CapturingApi(success_response(progress_receipt()))
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame(), rejected_validations=1)
        session = RunnerSession.admit(
            channel,
            RUNNER_FACTS,
            RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1),
            CHF_RUNNER_CODEC,
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(active))
                journal.replace(pending_from_active(active), active)
                outcome = prepare_chf_execution(
                    api=api,
                    journal=journal,
                    session=session,
                    record=active,
                    canonical_input=canonical_input,
                )
                self.assertEqual(journal.records(), (outcome.record,))
        self.assertIs(type(outcome), ChfInputFailurePending)
        self.assertEqual(len(channel.received_frames), 1)

    def test_uncertain_preparing_progress_does_not_reach_the_runner(self) -> None:
        canonical_input = valid_chf_input()
        active = active_attempt(canonical_input)
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
                api = CapturingApi(evidence)
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    journal.admit(pending_from_active(active))
                    journal.replace(pending_from_active(active), active)
                    outcome = prepare_chf_execution(
                        api=api,
                        journal=journal,
                        session=UnusedSession(),
                        record=active,
                        canonical_input=canonical_input,
                    )
                    self.assertIs(type(outcome), expected_type)
                    self.assertEqual(journal.records(), (active,))

    def test_preparation_checks_phase_and_input_binding_before_effects(self) -> None:
        canonical_input = valid_chf_input()
        pre_execution = active_attempt(canonical_input)
        entered = ActiveAttempt(
            job_ref=pre_execution.job_ref,
            provider_attempt_key=pre_execution.provider_attempt_key,
            input_fingerprint=pre_execution.input_fingerprint,
            frozen_generation_id=pre_execution.frozen_generation_id,
            execution_attempt_ref=pre_execution.execution_attempt_ref,
            local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
        )
        cases = (
            (pre_execution, canonical_input + b" ", "input does not match"),
            (entered, canonical_input, "pre-execution"),
        )
        for record, supplied_input, message in cases:
            with self.subTest(message=message), journal_directory() as root:
                api = CapturingApi()
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    journal.admit(pending_from_active(record))
                    journal.replace(pending_from_active(record), record)
                    with self.assertRaisesRegex(ValueError, message):
                        prepare_chf_execution(
                            api=api,
                            journal=journal,
                            session=UnusedSession(),
                            record=record,
                            canonical_input=supplied_input,
                        )
                    self.assertEqual(journal.records(), (record,))
                self.assertEqual(api.requests, [])

    def test_point_observation_binds_the_retained_attempt_and_job(self) -> None:
        active = active_attempt(valid_chf_input())
        api = CapturingApi(
            success_response(
                attempt_snapshot(
                    execution_attempt_ref=active.execution_attempt_ref,
                    job_ref=active.job_ref,
                    state="in_progress",
                    job_state="open",
                )
            )
        )
        outcome = observe_chf_attempt(api=api, record=active)

        self.assertIs(type(outcome), ChfAttemptObserved)
        self.assertEqual(
            outcome.snapshot.execution_attempt_ref,
            active.execution_attempt_ref,
        )
        self.assertEqual(outcome.snapshot.job_ref, active.job_ref)
        self.assertEqual(
            [request.operation for request in api.requests],
            [ProviderOperation.EXECUTION_ATTEMPT_READ],
        )

    def test_point_observation_preserves_drift_and_transport_evidence(self) -> None:
        active = active_attempt(valid_chf_input())
        cases = (
            (
                success_response(
                    attempt_snapshot(
                        execution_attempt_ref=active.execution_attempt_ref,
                        job_ref="job:another",
                        state="in_progress",
                        job_state="open",
                    )
                ),
                ProviderSuccessRejected(SuccessRejection.RESPONSE_DRIFT),
            ),
            (
                ProviderRequestUnavailable(RequestDelivery.POSSIBLE),
                ProviderRequestUnavailable(RequestDelivery.POSSIBLE),
            ),
        )
        for response, evidence in cases:
            with self.subTest(evidence=evidence):
                outcome = observe_chf_attempt(
                    api=CapturingApi(response),
                    record=active,
                )
                self.assertEqual(
                    outcome,
                    ChfAttemptObservationFailed(evidence),
                )

    def test_generation_requires_running_durability_and_open_final_state(self) -> None:
        active = active_attempt(valid_chf_input())
        session, channel, prepared = validated_execution(active)
        open_snapshot = success_response(
            attempt_snapshot(
                execution_attempt_ref=active.execution_attempt_ref,
                job_ref=active.job_ref,
                state="in_progress",
                job_state="open",
            )
        )
        api = CapturingApi(
            success_response(progress_receipt(phase="running")),
            *([open_snapshot] * 10),
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(active))
                journal.replace(pending_from_active(active), active)
                outcome = execute_prepared_chf(
                    api=api,
                    journal=journal,
                    session=session,
                    prepared=prepared,
                    observation=ChfObservationPolicy(0.01, 0.2),
                )
                self.assertEqual(journal.records(), (outcome.record,))

        self.assertIs(type(outcome), ChfCandidatesGenerated)
        self.assertEqual(outcome.candidates.value, ["CCO", "OCC"])
        self.assertIs(
            outcome.record.local_phase,
            LocalExecutionPhase.EXECUTION_ENTERED,
        )
        self.assertEqual(api.requests[0].operation, ProviderOperation.EXECUTION_ATTEMPT_PROGRESS)
        self.assertTrue(
            all(
                request.operation is ProviderOperation.EXECUTION_ATTEMPT_READ
                for request in api.requests[1:]
            )
        )
        self.assertEqual(type(channel.received_frames[-1]).__name__, "GenerateFrame")

    def test_initial_cutoff_or_terminal_snapshot_prevents_generation(self) -> None:
        cases = (
            ("in_progress", "cancelled", ChfExecutionCutOff, True),
            ("expired", "closed", ChfExecutionResolved, False),
        )
        for state, job_state, expected_type, retained in cases:
            with self.subTest(state=state), journal_directory() as root:
                active = active_attempt(valid_chf_input())
                session, channel, prepared = validated_execution(active)
                api = CapturingApi(
                    success_response(progress_receipt(phase="running")),
                    success_response(
                        attempt_snapshot(
                            execution_attempt_ref=active.execution_attempt_ref,
                            job_ref=active.job_ref,
                            state=state,
                            job_state=job_state,
                        )
                    ),
                )
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    journal.admit(pending_from_active(active))
                    journal.replace(pending_from_active(active), active)
                    outcome = execute_prepared_chf(
                        api=api,
                        journal=journal,
                        session=session,
                        prepared=prepared,
                        observation=ChfObservationPolicy(0.01, 0.2),
                    )
                    self.assertIs(type(outcome), expected_type)
                    self.assertEqual(bool(journal.records()), retained)
                self.assertTrue(channel.closed)
                self.assertEqual(len(channel.received_frames), 1)

    def test_observer_cancels_blocked_generation_on_cutoff_or_lost_read(self) -> None:
        cases = (
            (
                success_response(
                    attempt_snapshot(
                        execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
                        job_ref="job:selected",
                        state="in_progress",
                        job_state="closed",
                    )
                ),
                ChfExecutionCutOff,
            ),
            (
                ProviderRequestUnavailable(RequestDelivery.POSSIBLE),
                ChfObservationLost,
            ),
        )
        for stopping_response, expected_type in cases:
            with self.subTest(expected_type=expected_type), journal_directory() as root:
                active = active_attempt(valid_chf_input())
                session, channel, prepared = validated_execution(
                    active,
                    fault=FakeRunnerFault.BLOCK_GENERATION,
                )
                api = CapturingApi(
                    success_response(progress_receipt(phase="running")),
                    success_response(
                        attempt_snapshot(
                            execution_attempt_ref=active.execution_attempt_ref,
                            job_ref=active.job_ref,
                            state="in_progress",
                            job_state="open",
                        )
                    ),
                    stopping_response,
                )
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    journal.admit(pending_from_active(active))
                    journal.replace(pending_from_active(active), active)
                    outcome = execute_prepared_chf(
                        api=api,
                        journal=journal,
                        session=session,
                        prepared=prepared,
                        observation=ChfObservationPolicy(0.01, 0.2),
                    )
                    self.assertIs(type(outcome), expected_type)
                    self.assertIs(
                        journal.records()[0].local_phase,
                        LocalExecutionPhase.EXECUTION_ENTERED,
                    )
                self.assertTrue(channel.closed)

    def test_uncertain_running_progress_cancels_before_execution_entry(self) -> None:
        active = active_attempt(valid_chf_input())
        session, channel, prepared = validated_execution(active)
        api = CapturingApi(ProviderRequestUnavailable(RequestDelivery.POSSIBLE))
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(active))
                journal.replace(pending_from_active(active), active)
                outcome = execute_prepared_chf(
                    api=api,
                    journal=journal,
                    session=session,
                    prepared=prepared,
                    observation=ChfObservationPolicy(0.01, 0.2),
                )
                self.assertIs(type(outcome), AttemptMutationCommitPossible)
                self.assertEqual(journal.records(), (active,))
        self.assertTrue(channel.closed)
        self.assertEqual(len(channel.received_frames), 1)

    def test_non_stopping_worker_is_reported_as_process_fatal(self) -> None:
        active = active_attempt(valid_chf_input())
        session = NonStoppingSession()
        prepared = ChfPreparedForExecution(active, object())
        api = CapturingApi(
            success_response(progress_receipt(phase="running")),
            success_response(
                attempt_snapshot(
                    execution_attempt_ref=active.execution_attempt_ref,
                    job_ref=active.job_ref,
                    state="in_progress",
                    job_state="open",
                )
            ),
            success_response(
                attempt_snapshot(
                    execution_attempt_ref=active.execution_attempt_ref,
                    job_ref=active.job_ref,
                    state="in_progress",
                    job_state="cancelled",
                )
            ),
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(active))
                journal.replace(pending_from_active(active), active)
                try:
                    with self.assertRaisesRegex(
                        ChfExecutionShutdownFailed,
                        "confirmed stopped state",
                    ):
                        execute_prepared_chf(
                            api=api,
                            journal=journal,
                            session=session,
                            prepared=prepared,
                            observation=ChfObservationPolicy(0.01, 0.01),
                        )
                finally:
                    session.release.set()
                    self.assertTrue(session.finished.wait(timeout=0.2))
                self.assertIs(
                    journal.records()[0].local_phase,
                    LocalExecutionPhase.EXECUTION_ENTERED,
                )

    def test_generated_candidates_become_one_durable_completion(self) -> None:
        entered = entered_attempt(valid_chf_input())
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame())
        session = RunnerSession.admit(
            channel,
            RUNNER_FACTS,
            RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1),
            CHF_RUNNER_CODEC,
        )
        generated = ChfCandidatesGenerated(
            entered,
            generated_candidates(session, entered),
            session,
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(entered))
                journal.replace(pending_from_active(entered), entered)
                outcome = select_chf_completion(
                    journal=journal,
                    generated=generated,
                )
            self.assertIs(type(outcome), ChfCompletionPending)
            with AttemptJournalStore(root, maximum_records=1) as reopened:
                self.assertEqual(reopened.records(), (outcome.record,))

        self.assertIs(
            outcome.record.terminal_operation,
            TerminalOperation.COMPLETE,
        )
        command = json.loads(outcome.record.terminal_request_body)
        self.assertEqual(command["execution_attempt_ref"], entered.execution_attempt_ref)
        self.assertEqual(command["result_schema_id"], RESULT_SCHEMA_ID)

    def test_invalid_runner_candidates_retire_boot_without_caller_failure(self) -> None:
        entered = entered_attempt(valid_chf_input())
        channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame(), candidates=())
        session = RunnerSession.admit(
            channel,
            RUNNER_FACTS,
            RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1),
            CHF_RUNNER_CODEC,
        )
        generated = ChfCandidatesGenerated(
            entered,
            generated_candidates(session, entered),
            session,
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(entered))
                journal.replace(pending_from_active(entered), entered)
                with self.assertRaises(RunnerResultRejected):
                    select_chf_completion(
                        journal=journal,
                        generated=generated,
                    )
                self.assertEqual(journal.records(), (entered,))
        self.assertTrue(channel.closed)

    def test_completion_rejects_candidates_from_another_attempt(self) -> None:
        generated_for = entered_attempt(valid_chf_input())
        selected_record = ActiveAttempt(
            job_ref=generated_for.job_ref,
            provider_attempt_key=generated_for.provider_attempt_key,
            input_fingerprint=generated_for.input_fingerprint,
            frozen_generation_id=generated_for.frozen_generation_id,
            execution_attempt_ref="execution_attempt:sha256:" + "b" * 64,
            local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
        )
        session = RunnerSession.admit(
            FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame()),
            RUNNER_FACTS,
            RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1),
            CHF_RUNNER_CODEC,
        )
        generated = ChfCandidatesGenerated(
            selected_record,
            generated_candidates(session, generated_for),
            session,
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(selected_record))
                journal.replace(pending_from_active(selected_record), selected_record)
                with self.assertRaisesRegex(ValueError, "retained Attempt"):
                    select_chf_completion(
                        journal=journal,
                        generated=generated,
                    )
                self.assertEqual(journal.records(), (selected_record,))

    def test_command_bound_terminal_receipts_retire_complete_and_fail(self) -> None:
        for operation in (TerminalOperation.COMPLETE, TerminalOperation.FAIL):
            for replayed in (False, True):
                with (
                    self.subTest(operation=operation, replayed=replayed),
                    journal_directory() as root,
                ):
                    terminal = terminal_pending(operation)
                    api = CapturingApi(
                        success_response(
                            terminal_receipt(terminal, replayed=replayed)
                        )
                    )
                    with AttemptJournalStore(root, maximum_records=1) as journal:
                        persist_terminal(journal, terminal)
                        outcome = deliver_chf_terminal(
                            api=api,
                            journal=journal,
                            record=terminal,
                        )
                        self.assertEqual(journal.records(), ())
                    self.assertIs(type(outcome), ChfTerminalDelivered)
                    self.assertEqual(api.requests[0].body, terminal.terminal_request_body)

    def test_uncertain_terminal_delivery_retains_exact_command(self) -> None:
        terminal = terminal_pending(TerminalOperation.COMPLETE)
        responses = (
            ProviderRequestUnavailable(RequestDelivery.NOT_SENT),
            ProviderRequestUnavailable(RequestDelivery.POSSIBLE),
            success_response({"schema_id": "wrong"}),
        )
        expected_types = (
            AttemptMutationNotCommitted,
            AttemptMutationCommitPossible,
            AttemptMutationCommitPossible,
        )
        for response, expected_type in zip(responses, expected_types, strict=True):
            with self.subTest(expected_type=expected_type), journal_directory() as root:
                api = CapturingApi(response)
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    persist_terminal(journal, terminal)
                    outcome = deliver_chf_terminal(
                        api=api,
                        journal=journal,
                        record=terminal,
                    )
                    self.assertIs(type(outcome), expected_type)
                    self.assertEqual(journal.records(), (terminal,))
                self.assertIs(api.requests[0].body, terminal.terminal_request_body)

    def test_restart_resumes_pre_execution_from_the_retained_input_binding(self) -> None:
        active = active_attempt(valid_chf_input())
        api = CapturingApi(
            success_response(
                attempt_snapshot(
                    execution_attempt_ref=active.execution_attempt_ref,
                    job_ref=active.job_ref,
                    state="in_progress",
                    job_state="open",
                )
            ),
            success_response(job_input(active.job_ref, valid_chf_input())),
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(active))
                journal.replace(pending_from_active(active), active)
                outcome = reconcile_chf_record(
                    api=api,
                    journal=journal,
                    generation=chf_generation(),
                    frozen_generation_id=FROZEN_GENERATION_ID,
                    record=active,
                )
                self.assertEqual(journal.records(), (active,))
        self.assertEqual(outcome, ChfRecoveryResumes(active, valid_chf_input()))

    def test_restart_replays_a_pending_start_without_changing_its_key(self) -> None:
        pending = active_attempt(valid_chf_input())
        record = pending_from_active(pending)
        api = CapturingApi(success_response(start_receipt("in_progress", replayed=True)))
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(record)
                outcome = reconcile_chf_record(
                    api=api,
                    journal=journal,
                    generation=chf_generation(),
                    frozen_generation_id=FROZEN_GENERATION_ID,
                    record=record,
                )
                self.assertIs(type(outcome), ChfStartContinues)
                self.assertEqual(outcome.record.provider_attempt_key, record.provider_attempt_key)
                self.assertEqual(journal.records(), (outcome.record,))

    def test_restart_converts_entered_execution_to_one_durable_failure(self) -> None:
        entered = entered_attempt(valid_chf_input())
        api = CapturingApi(
            success_response(
                attempt_snapshot(
                    execution_attempt_ref=entered.execution_attempt_ref,
                    job_ref=entered.job_ref,
                    state="in_progress",
                    job_state="open",
                )
            )
        )
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=1) as journal:
                journal.admit(pending_from_active(entered))
                journal.replace(pending_from_active(entered), entered)
                outcome = reconcile_chf_record(
                    api=api,
                    journal=journal,
                    generation=chf_generation(),
                    frozen_generation_id=FROZEN_GENERATION_ID,
                    record=entered,
                )
                self.assertIs(type(outcome), ChfInterruptedFailurePending)
                self.assertEqual(journal.records(), (outcome.record,))
        command = json.loads(outcome.record.terminal_request_body)
        self.assertEqual(command["failure_code"], "provider_execution_interrupted")
        self.assertEqual(
            command["failure_message"],
            "The provider process was interrupted before this execution completed.",
        )

    def test_restart_retains_cutoff_and_conflicting_terminal_obligations(self) -> None:
        entered = entered_attempt(valid_chf_input())
        complete = terminal_pending(TerminalOperation.COMPLETE)
        cases = (
            (
                entered,
                "in_progress",
                "cancelled",
                ObserveUntilExpiry,
            ),
            (
                complete,
                "failed",
                "closed",
                RetainTerminalConflict,
            ),
        )
        for record, state, job_state, expected_type in cases:
            with self.subTest(expected_type=expected_type), journal_directory() as root:
                api = CapturingApi(
                    success_response(
                        attempt_snapshot(
                            execution_attempt_ref=record.execution_attempt_ref,
                            job_ref=record.job_ref,
                            state=state,
                            job_state=job_state,
                        )
                    )
                )
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    if type(record) is TerminalPending:
                        persist_terminal(journal, record)
                    else:
                        journal.admit(pending_from_active(record))
                        journal.replace(pending_from_active(record), record)
                    outcome = reconcile_chf_record(
                        api=api,
                        journal=journal,
                        generation=chf_generation(),
                        frozen_generation_id=FROZEN_GENERATION_ID,
                        record=record,
                    )
                    self.assertIs(type(outcome), expected_type)
                    self.assertEqual(journal.records(), (record,))

    def test_restart_replays_terminal_or_retires_authoritatively_resolved_work(self) -> None:
        terminal = terminal_pending(TerminalOperation.COMPLETE)
        entered = entered_attempt(valid_chf_input())
        cases = (
            (
                terminal,
                "in_progress",
                success_response(terminal_receipt(terminal, replayed=True)),
                ChfTerminalDelivered,
            ),
            (entered, "expired", None, ChfRecoveryResolved),
        )
        for record, state, terminal_response, expected_type in cases:
            with self.subTest(expected_type=expected_type), journal_directory() as root:
                responses = [
                    success_response(
                        attempt_snapshot(
                            execution_attempt_ref=record.execution_attempt_ref,
                            job_ref=record.job_ref,
                            state=state,
                            job_state="closed" if state != "in_progress" else "open",
                        )
                    )
                ]
                if terminal_response is not None:
                    responses.append(terminal_response)
                api = CapturingApi(*responses)
                with AttemptJournalStore(root, maximum_records=1) as journal:
                    if type(record) is TerminalPending:
                        persist_terminal(journal, record)
                    else:
                        journal.admit(pending_from_active(record))
                        journal.replace(pending_from_active(record), record)
                    outcome = reconcile_chf_record(
                        api=api,
                        journal=journal,
                        generation=chf_generation(),
                        frozen_generation_id=FROZEN_GENERATION_ID,
                        record=record,
                    )
                    self.assertIs(type(outcome), expected_type)
                    self.assertEqual(journal.records(), ())


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


def active_attempt(canonical_input: bytes) -> ActiveAttempt:
    input_fingerprint = fingerprint_of(canonical_input)
    generation = chf_generation()
    return ActiveAttempt(
        job_ref="job:selected",
        provider_attempt_key=derive_provider_attempt_key(
            provider_ref=generation.provider_ref,
            run_generation_fingerprint=run_generation_fingerprint(generation),
            job_ref="job:selected",
            input_fingerprint=input_fingerprint,
        ),
        input_fingerprint=input_fingerprint,
        frozen_generation_id=FROZEN_GENERATION_ID,
        execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
        local_phase=LocalExecutionPhase.PRE_EXECUTION,
    )


def entered_attempt(canonical_input: bytes) -> ActiveAttempt:
    active = active_attempt(canonical_input)
    return ActiveAttempt(
        job_ref=active.job_ref,
        provider_attempt_key=active.provider_attempt_key,
        input_fingerprint=active.input_fingerprint,
        frozen_generation_id=active.frozen_generation_id,
        execution_attempt_ref=active.execution_attempt_ref,
        local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
    )


def pending_from_active(active: ActiveAttempt) -> StartPending:
    return StartPending(
        job_ref=active.job_ref,
        provider_attempt_key=active.provider_attempt_key,
        input_fingerprint=active.input_fingerprint,
        frozen_generation_id=active.frozen_generation_id,
    )


def valid_chf_input() -> bytes:
    return json.dumps(
        {
            "schema_id": "nmrpeak.structure_generation.request.v1",
            "model_input": {
                "formula": "C2H6O",
                "spectra": {
                    "1H": {
                        "peaks": [
                            {
                                "shift_lo": "1.20",
                                "shift_hi": "1.30",
                                "integral": "3",
                                "multiplicity": "t",
                                "j_hz": ["7.1"],
                            }
                        ]
                    },
                    "13C": {"peaks": [{"shift": "70.4"}]},
                },
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def progress_receipt(*, phase: str = "preparing") -> dict[str, object]:
    return {
        "schema_id": "nmr.provider.execution_attempt_progress_response.v1",
        "execution_attempt_ref": "execution_attempt:sha256:" + "a" * 64,
        "phase": phase,
        "condition_code": None,
        "updated_at": "2026-08-24T12:01:00Z",
    }


def attempt_snapshot(
    *,
    execution_attempt_ref: str,
    job_ref: str,
    state: str,
    job_state: str,
) -> dict[str, object]:
    return {
        "schema_id": "nmr.provider.execution_attempt_read_response.v1",
        "execution_attempt_ref": execution_attempt_ref,
        "job_ref": job_ref,
        "state": state,
        "job_state": job_state,
    }


def ready_frame() -> ReadyFrame:
    return ReadyFrame(
        boot_generation="boot:" + "1" * 32,
        runner_ref="nmrpeak_chf_v1",
        runner_contract_id=RUNNER_FACTS.runner_contract_id,
        release_sha256=RUNNER_FACTS.checkpoint_ref,
        source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
        image_input_id=RUNNER_FACTS.image_input_ref,
        target="cpu-x86_64",
        device="cpu",
        decode_policy_id="nmrpeak_chf_decode_v1",
    )


def validated_execution(
    active: ActiveAttempt,
    *,
    fault: FakeRunnerFault | None = None,
) -> tuple[RunnerSession, FakeRunnerChannel, ChfPreparedForExecution]:
    channel = FakeRunnerChannel(CHF_RUNNER_CODEC, ready_frame(), fault=fault)
    session = RunnerSession.admit(
        channel,
        RUNNER_FACTS,
        RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1),
        CHF_RUNNER_CODEC,
    )
    request = session.validate(
        execution_attempt_ref=active.execution_attempt_ref,
        provider_attempt_key=active.provider_attempt_key,
        model_input=ChfRunnerInput(
            "C2H6O",
            (RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
            (ChfRunnerCarbonPeak("70.4"),),
        ),
    )
    if type(request) is not ValidatedRunnerRequest:
        raise AssertionError("test runner unexpectedly rejected validation")
    return session, channel, ChfPreparedForExecution(active, request)


def generated_candidates(
    session: RunnerSession,
    active: ActiveAttempt,
) -> GeneratedRunnerCandidates:
    request = session.validate(
        execution_attempt_ref=active.execution_attempt_ref,
        provider_attempt_key=active.provider_attempt_key,
        model_input=ChfRunnerInput(
            "C2H6O",
            (RunnerProtonPeak("1.25", 3, "t", "7.1_"),),
            (ChfRunnerCarbonPeak("70.4"),),
        ),
    )
    if type(request) is not ValidatedRunnerRequest:
        raise AssertionError("test runner unexpectedly rejected validation")
    return session.generate(request)


def terminal_pending(operation: TerminalOperation) -> TerminalPending:
    entered = entered_attempt(valid_chf_input())
    prepared = (
        prepare_execution_attempt_complete(
            execution_attempt_ref=entered.execution_attempt_ref,
            result_schema_id=RESULT_SCHEMA_ID,
            canonical_result=b'{"candidates":[{"generated_smiles":"CCO"}]}',
        )
        if operation is TerminalOperation.COMPLETE
        else prepare_execution_attempt_fail(
            execution_attempt_ref=entered.execution_attempt_ref,
            failure_code="input_rejected",
            failure_message=InputRejected.public_message,
        )
    )
    return retain_terminal_command(entered, prepared)


def persist_terminal(
    journal: AttemptJournalStore,
    terminal: TerminalPending,
) -> None:
    pending = StartPending(
        job_ref=terminal.job_ref,
        provider_attempt_key=terminal.provider_attempt_key,
        input_fingerprint=terminal.input_fingerprint,
        frozen_generation_id=terminal.frozen_generation_id,
    )
    entered = ActiveAttempt(
        job_ref=terminal.job_ref,
        provider_attempt_key=terminal.provider_attempt_key,
        input_fingerprint=terminal.input_fingerprint,
        frozen_generation_id=terminal.frozen_generation_id,
        execution_attempt_ref=terminal.execution_attempt_ref,
        local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
    )
    journal.admit(pending)
    journal.replace(pending, entered)
    journal.replace(entered, terminal)


def terminal_receipt(
    terminal: TerminalPending,
    *,
    replayed: bool,
) -> dict[str, object]:
    command = json.loads(terminal.terminal_request_body)
    if terminal.terminal_operation is TerminalOperation.COMPLETE:
        result = b64decode(command["canonical_result_base64"], validate=True)
        return {
            "schema_id": "nmr.provider.execution_attempt_complete_response.v1",
            "execution_attempt_ref": terminal.execution_attempt_ref,
            "analysis_result_ref": "analysis_result:sha256:" + "c" * 64,
            "result_schema_id": command["result_schema_id"],
            "result_fingerprint": fingerprint_of(result),
            "result_byte_length": len(result),
            "committed_at": "2026-08-24T12:02:00Z",
            "replayed": replayed,
        }
    return {
        "schema_id": "nmr.provider.execution_attempt_fail_response.v1",
        "execution_attempt_ref": terminal.execution_attempt_ref,
        "failure_code": command["failure_code"],
        "failure_message": command["failure_message"],
        "committed_at": "2026-08-24T12:02:00Z",
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
