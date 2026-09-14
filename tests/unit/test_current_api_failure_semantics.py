"""Prove current API causes and send certainty survive the NMRPeak adapter."""

from __future__ import annotations

import json
import unittest

from nmrpeak_provider.provider_https import ProviderHttpResponse, ProviderOperation
from nmrpeak_provider.provider_outcomes import (
    AttemptMutationCommitPossible,
    AttemptMutationNotCommitted,
    interpret_execution_attempt_complete,
)
from nmrpeak_provider.provider_problems import (
    ProviderProblem,
    ProviderProblemRejected,
    parse_provider_problem,
)
from nmrpeak_provider.provider_requests import prepare_execution_attempt_complete
from tests.unit import test_attempt_lifecycle as lifecycle


# Concrete API 2076ddf3 wire evidence, not a local interpretation policy.
_ADMISSION_DETAIL = (
    "This server has reached its limit for simultaneous requests. "
    "This attempt was rejected before the operation started; it made no changes. "
    "Wait briefly, then try again."
)


def problem_response(status, kind, title, code, detail, *, request_id="current-request"):
    return ProviderHttpResponse(
        status=status, topology="dev-local", content_type="application/problem+json",
        request_id=request_id,
        body=json.dumps({
            "type": "urn:nmr-api:problem:" + kind,
            "title": title, "status": status, "code": code, "detail": detail,
            "request_id": "current-request", "instance": "urn:nmr-api:request:current-request",
        }).encode("utf-8"),
    )


def admission_response():
    return problem_response(503, "service-unavailable", "Service unavailable",
                            "http_exchange_count_exhausted", _ADMISSION_DETAIL)


class CurrentApiFailureSemanticsTests(unittest.TestCase):
    def test_same_503_causes_retain_distinct_send_effects_and_recovery(self):
        cases = (
            (admission_response(), "no_domain_change", AttemptMutationNotCommitted),
            (problem_response(503, "mutation-outcome-unconfirmed", "Change outcome unconfirmed",
                              "service_unavailable", "The mutation outcome could not be confirmed."),
             "unconfirmed", AttemptMutationCommitPossible),
            (problem_response(503, "service-unavailable", "Service unavailable",
                              "database_work_slots_exhausted", "The database work slots are exhausted."),
             "no_assertion", AttemptMutationCommitPossible),
        )
        prepared = prepare_execution_attempt_complete(
            execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
            result_schema_id="nmr.analysis_result.test.v1", canonical_result=b"result",
        )
        for response, effect, outcome_type in cases:
            with self.subTest(effect=effect):
                parsed = parse_provider_problem(prepared.operation, response)
                self.assertIs(type(parsed), ProviderProblem)
                self.assertEqual(parsed.current_send_effect, effect)
                self.assertEqual(parsed.recovery_mode, "exact_completion")
                self.assertTrue(parsed.recovery_description)
                self.assertIs(type(interpret_execution_attempt_complete(prepared, response)), outcome_type)

    def test_status_alone_does_not_prove_no_change(self):
        prepared = prepare_execution_attempt_complete(
            execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
            result_schema_id="nmr.analysis_result.test.v1", canonical_result=b"result",
        )
        for response in (
            problem_response(400, "bad-request", "Bad request", "provider_request_invalid",
                             "Correct the signed provider request."),
            problem_response(403, "authorization-denied", "Authorization denied", "authorization_denied",
                             "This request could not be authorized."),
        ):
            with self.subTest(status=response.status):
                outcome = interpret_execution_attempt_complete(prepared, response)
                self.assertIs(type(outcome), AttemptMutationCommitPossible)
                self.assertIs(type(outcome.evidence), ProviderProblem)
                self.assertEqual(outcome.evidence.current_send_effect, "no_assertion")

    def test_unverified_admission_cannot_supply_recovery_or_send_authority(self):
        response = problem_response(
            503, "service-unavailable", "Service unavailable", "http_exchange_count_exhausted",
            _ADMISSION_DETAIL, request_id="different-request",
        )
        parsed = parse_provider_problem(ProviderOperation.EXECUTION_ATTEMPT_COMPLETE, response)
        self.assertIs(type(parsed), ProviderProblemRejected)
        self.assertIsNotNone(parsed.diagnostic)
        self.assertFalse(parsed.diagnostic.verified)
        self.assertEqual(parsed.diagnostic.detail, _ADMISSION_DETAIL)
        self.assertIsNone(parsed.diagnostic.recovery_mode)
        self.assertIsNone(parsed.diagnostic.current_send_effect)

    def test_expired_snapshot_preserves_unconfirmed_terminal_report(self):
        for operation in lifecycle.TerminalOperation:
            with self.subTest(operation=operation), lifecycle.journal_directory() as root:
                record = lifecycle.terminal_pending(operation)
                api = lifecycle.CapturingApi(lifecycle.success_response(lifecycle.attempt_snapshot(
                    execution_attempt_ref=record.execution_attempt_ref, job_ref=record.job_ref,
                    state="expired", job_state="open")))
                with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                    lifecycle.persist_terminal(journal, record)
                    lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api,
                                               journal=journal, record=record)
                    retained, = journal.records()
                    self.assertEqual(retained.terminal_request_body, record.terminal_request_body)
                    self.assertEqual(retained.execution_attempt_ref, record.execution_attempt_ref)
                    self.assertEqual(retained.terminal_request_fingerprint, record.terminal_request_fingerprint)
                    self.assertIsNotNone(retained.terminal_hold_action)
                    self.assertEqual(retained.terminal_observed_state, "expired")
                from nmrpeak_provider.attempt_lifecycle import terminal_recovery_facts
                with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                    api = lifecycle.CapturingApi()
                    recovered = lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api,
                                                           journal=journal, record=journal.records()[0])
                    self.assertEqual(terminal_recovery_facts(recovered.record)["observed_state"], "expired")
                    self.assertEqual(api.requests, [])

    def test_terminal_refusal_stops_resends_after_journal_reopen(self):
        for operation in lifecycle.TerminalOperation:
            codes = ("execution_attempt_outcome_expired",
                     "execution_attempt_completion_after_failure" if operation is lifecycle.TerminalOperation.COMPLETE
                     else "execution_attempt_failure_after_success",
                     "execution_attempt_completion_replay_mismatch" if operation is lifecycle.TerminalOperation.COMPLETE
                     else "execution_attempt_failure_replay_mismatch")
            for code in codes:
                with self.subTest(operation=operation, code=code), lifecycle.journal_directory() as root:
                    record = lifecycle.terminal_pending(operation)
                    refusal = problem_response(409, "operation-conflict", "Operation conflict", code,
                                               "Reconcile the retained terminal command; do not change its payload.")
                    api = lifecycle.CapturingApi(
                        lifecycle.ProviderRequestUnavailable(lifecycle.RequestDelivery.POSSIBLE), refusal, refusal)
                    with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                        lifecycle.persist_terminal(journal, record)
                        uncertain = lifecycle.deliver_terminal(api=api, journal=journal, record=record)
                        self.assertIs(type(uncertain), AttemptMutationCommitPossible)
                        lifecycle.deliver_terminal(api=api, journal=journal, record=record)
                        self.assertEqual(journal.records()[0].terminal_request_body, record.terminal_request_body)
                        self.assertEqual(api.requests[0].body, api.requests[1].body)
                    with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                        retained = journal.records()[0]
                        lifecycle.deliver_terminal(api=api, journal=journal, record=retained)
                        self.assertEqual(journal.records()[0].terminal_request_body, record.terminal_request_body)
                    self.assertEqual(len(api.requests), 2, "Reopening the journal must not release a terminal publication hold")

    def test_later_no_change_refusal_preserves_uncertain_start_and_terminal(self):
        for terminal_operation in (None, lifecycle.TerminalOperation.COMPLETE,
                                    lifecycle.TerminalOperation.FAIL):
            with self.subTest(operation=terminal_operation), lifecycle.journal_directory() as root:
                generation = lifecycle.chf_generation()
                record = (lifecycle.pending_start(generation) if terminal_operation is None
                          else lifecycle.terminal_pending(terminal_operation))
                api = lifecycle.CapturingApi(
                    lifecycle.ProviderRequestUnavailable(lifecycle.RequestDelivery.POSSIBLE),
                    admission_response(),
                )
                with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                    if terminal_operation is None:
                        journal.admit(record)
                    else:
                        lifecycle.persist_terminal(journal, record)
                    for expected in (AttemptMutationCommitPossible, AttemptMutationNotCommitted):
                        if terminal_operation is None:
                            outcome = lifecycle.start_attempt(
                                lane=lifecycle.CHF_LIFECYCLE_LANE, api=api, journal=journal,
                                generation=generation, frozen_generation_id=lifecycle.FROZEN_GENERATION_ID,
                                record=record,
                            )
                        else:
                            outcome = lifecycle.deliver_terminal(api=api, journal=journal, record=record)
                        self.assertEqual(journal.records(), (record,))
                        self.assertIs(type(outcome), expected)
                    self.assertEqual(api.requests[0].body, api.requests[1].body)
                    self.assertEqual(api.requests[0].operation, api.requests[1].operation)
                with lifecycle.AttemptJournalStore(root, maximum_records=1) as reopened:
                    self.assertEqual(reopened.records(), (record,))

    def test_generic_conflict_recovery_reads_only_across_journal_reopen(self):
        from nmrpeak_provider.attempt_lifecycle import TerminalPublicationHeld
        from nmrpeak_provider.provider_process import _remote_failure_evidence
        record = lifecycle.terminal_pending(lifecycle.TerminalOperation.COMPLETE)
        refusal = problem_response(409, "operation-conflict", "Operation conflict", "operation_conflict", "Reconcile current state.")
        with lifecycle.journal_directory() as root:
            api = lifecycle.CapturingApi(refusal, lifecycle.ProviderRequestUnavailable(lifecycle.RequestDelivery.POSSIBLE))
            with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                lifecycle.persist_terminal(journal, record)
                outcome = lifecycle.deliver_terminal(api=api, journal=journal, record=record)
                self.assertIsNotNone(_remote_failure_evidence(outcome))
                self.assertEqual([request.operation for request in api.requests], [ProviderOperation.EXECUTION_ATTEMPT_COMPLETE, ProviderOperation.EXECUTION_ATTEMPT_READ])
                self.assertEqual(json.loads(next(root.glob("*.json")).read_bytes())["record_kind"], "terminal_reconciling")
            api = lifecycle.CapturingApi(lifecycle.ProviderRequestUnavailable(lifecycle.RequestDelivery.POSSIBLE))
            with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                outcome = lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api, journal=journal, record=journal.records()[0])
                self.assertIsNotNone(_remote_failure_evidence(outcome))
                self.assertEqual([request.operation for request in api.requests], [ProviderOperation.EXECUTION_ATTEMPT_READ])
            api = lifecycle.CapturingApi(lifecycle.success_response(lifecycle.attempt_snapshot(execution_attempt_ref=record.execution_attempt_ref, job_ref=record.job_ref, state="in_progress", job_state="open")))
            with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                outcome = lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api, journal=journal, record=journal.records()[0])
                self.assertIs(type(outcome), TerminalPublicationHeld)
                self.assertEqual(journal.records()[0].terminal_request_body, record.terminal_request_body)
                self.assertEqual([request.operation for request in api.requests], [ProviderOperation.EXECUTION_ATTEMPT_READ])
            with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                api = lifecycle.CapturingApi()
                outcome = lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api, journal=journal, record=journal.records()[0])
                self.assertIs(type(outcome), TerminalPublicationHeld)
                self.assertEqual(api.requests, [])

    def test_explicit_hold_recovery_never_observes_or_resends(self):
        record = lifecycle.terminal_pending(lifecycle.TerminalOperation.COMPLETE)
        with lifecycle.journal_directory() as root:
            with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                lifecycle.persist_terminal(journal, record)
                api = lifecycle.CapturingApi(problem_response(409, "operation-conflict", "Operation conflict", "execution_attempt_outcome_expired", "The Attempt expired."))
                lifecycle.deliver_terminal(api=api, journal=journal, record=record)
            with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                api = lifecycle.CapturingApi()
                lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api, journal=journal, record=journal.records()[0])
                self.assertEqual(api.requests, [])

    def test_pending_read_is_durable_before_network_and_keeps_malformed_read_unverified(self):
        from nmrpeak_provider.attempt_lifecycle import TerminalReconciliationPending, terminal_recovery_facts
        record = lifecycle.terminal_pending(lifecycle.TerminalOperation.COMPLETE)
        refusal = problem_response(409, "operation-conflict", "Operation conflict", "operation_conflict", "Reconcile current state.")
        with lifecycle.journal_directory() as root, lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
            lifecycle.persist_terminal(journal, record)
            class CheckingApi(lifecycle.CapturingApi):
                def send(inner, request):
                    if request.operation is ProviderOperation.EXECUTION_ATTEMPT_READ:
                        self.assertTrue(journal.records()[0].terminal_reconciling)
                    return super().send(request)
            api = CheckingApi(refusal, problem_response(404, "not-found", "Resource not found", "resource_not_found", "Not found.", request_id="mismatch"))
            outcome = lifecycle.deliver_terminal(api=api, journal=journal, record=record)
            self.assertIs(type(outcome), TerminalReconciliationPending)
            self.assertTrue(journal.records()[0].terminal_reconciling)
            facts = terminal_recovery_facts(outcome.record)
            self.assertEqual(facts["execution_attempt_ref"], record.execution_attempt_ref)
            self.assertEqual(facts["command_fingerprint"], record.terminal_request_fingerprint)
            self.assertEqual(facts["delivery"], "unconfirmed")
            self.assertEqual(facts["automatic_reads"], "retry_with_backoff")
            self.assertEqual(facts["code"], "operation_conflict")
            self.assertEqual(facts["request_id"], "current-request")

    def test_reconciliation_persistence_failure_prevents_read(self):
        from unittest.mock import patch
        from nmrpeak_provider.attempt_journal_store import AttemptJournalWriteFailed
        record = lifecycle.terminal_pending(lifecycle.TerminalOperation.COMPLETE)
        with lifecycle.journal_directory() as root, lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
            lifecycle.persist_terminal(journal, record)
            api = lifecycle.CapturingApi(problem_response(409, "operation-conflict", "Operation conflict", "operation_conflict", "Conflict."))
            with patch.object(journal, "replace", side_effect=AttemptJournalWriteFailed("Storage unavailable")):
                with self.assertRaises(AttemptJournalWriteFailed):
                    lifecycle.deliver_terminal(api=api, journal=journal, record=record)
            self.assertEqual([request.operation for request in api.requests], [ProviderOperation.EXECUTION_ATTEMPT_COMPLETE])

    def test_stale_terminal_caller_cannot_override_persisted_hold(self):
        from nmrpeak_provider.attempt_journal_store import AttemptJournalConflict
        record = lifecycle.terminal_pending(lifecycle.TerminalOperation.COMPLETE)
        with lifecycle.journal_directory() as root, lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
            lifecycle.persist_terminal(journal, record)
            api = lifecycle.CapturingApi(problem_response(409, "operation-conflict", "Operation conflict", "execution_attempt_outcome_expired", "Attempt expired."))
            lifecycle.deliver_terminal(api=api, journal=journal, record=record)
            held = journal.records()[0]
            stale_api = lifecycle.CapturingApi()
            with self.assertRaises(AttemptJournalConflict):
                lifecycle.deliver_terminal(api=stale_api, journal=journal, record=record)
            self.assertEqual(stale_api.requests, [])
            self.assertEqual(json.loads(next(root.glob("*.json")).read_bytes())["record_kind"], "terminal_hold")

    def test_retired_readonly_and_closed_journals_cannot_authorize_publication(self):
        record = lifecycle.terminal_pending(lifecycle.TerminalOperation.COMPLETE)
        for state in ("retired", "readonly", "closed"):
            with self.subTest(state=state), lifecycle.journal_directory() as root:
                with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                    lifecycle.persist_terminal(journal, record)
                    if state == "retired":
                        journal.retire(record)
                    if state == "closed":
                        journal.close()
                    if state != "readonly":
                        api = lifecycle.CapturingApi()
                        with self.assertRaises(RuntimeError):
                            lifecycle.deliver_terminal(api=api, journal=journal, record=record)
                        self.assertEqual(api.requests, [])
                if state == "readonly":
                    with lifecycle.AttemptJournalStore(root, maximum_records=1, read_only=True) as journal:
                        api = lifecycle.CapturingApi()
                        with self.assertRaises(RuntimeError):
                            lifecycle.deliver_terminal(api=api, journal=journal, record=record)
                        self.assertEqual(api.requests, [])


    def test_opposite_terminal_snapshot_hold_keeps_observation_after_restart(self):
        from nmrpeak_provider.attempt_lifecycle import terminal_recovery_facts
        for operation in lifecycle.TerminalOperation:
            state = "failed" if operation is lifecycle.TerminalOperation.COMPLETE else "succeeded"
            with self.subTest(operation=operation), lifecycle.journal_directory() as root:
                record = lifecycle.terminal_pending(operation)
                api = lifecycle.CapturingApi(lifecycle.success_response(lifecycle.attempt_snapshot(
                    execution_attempt_ref=record.execution_attempt_ref, job_ref=record.job_ref,
                    state=state, job_state="open")))
                with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                    lifecycle.persist_terminal(journal, record)
                    lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api, journal=journal, record=record)
                    self.assertEqual(journal.records()[0].terminal_observed_state, state)
                with lifecycle.AttemptJournalStore(root, maximum_records=1) as journal:
                    api = lifecycle.CapturingApi()
                    recovered = lifecycle.reconcile_record(runtime=lifecycle.generation_runtime(), api=api,
                                                           journal=journal, record=journal.records()[0])
                    facts = terminal_recovery_facts(recovered.record)
                    self.assertEqual(facts["observed_state"], state)
                    self.assertIsNone(facts["code"])
                    self.assertIsNone(facts["request_id"])
                    self.assertEqual(api.requests, [])
