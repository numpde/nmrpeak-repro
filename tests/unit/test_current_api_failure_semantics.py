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
