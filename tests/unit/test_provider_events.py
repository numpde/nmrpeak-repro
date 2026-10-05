"""Prove the copied operator-event boundary is closed and bounded."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import unittest

from nmrpeak_provider import provider_events as events


_CASES = (
    (
        events.InterpreterEndpointFailed(
            execution_attempt_ref="attempt:one",
            configuration_id="primary", failure_kind="transport",
            failure_reason="request_failed", http_status=503,
        ),
        "interpreter_event='endpoint_failed'; configuration_id='primary'; "
        "failure_kind='transport'; failure_reason='request_failed'; "
        "execution_attempt_ref='attempt:one'; http_status=503",
    ),
    (
        events.InterpreterRoute(
            execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
            disposition="accepted", configuration_id="fallback",
            attempted_configuration_ids=("primary", "fallback"),
        ),
        "interpreter_event='route'; execution_attempt_ref='execution_attempt:sha256:"
        + "a" * 64
        + "'; disposition='accepted'; configuration_id='fallback'; "
        "attempted_configuration_ids=('primary', 'fallback')",
    ),
    (
        events.PreparationFailurePolicyDrift(
            job_ref="job:one", execution_attempt_ref="attempt:one",
            failure_kind="candidate_issue", reason="unknown_classified_failure_kind",
        ),
        "provider_event='preparation_failure_policy_drift'; job_ref='job:one'; "
        "execution_attempt_ref='attempt:one'; failure_kind='candidate_issue'; "
        "reason='unknown_classified_failure_kind'",
    ),
    (
        events.PreparationFailureRetained(
            job_ref="job:one", execution_attempt_ref="attempt:one",
            failure_kind="direct_source_issue", failure_code="input_rejected",
            reason="invalid_json", path="root", endpoint_route=(),
        ),
        "provider_event='preparation_failure_retained'; job_ref='job:one'; "
        "execution_attempt_ref='attempt:one'; failure_kind='direct_source_issue'; "
        "failure_code='input_rejected'; reason='invalid_json'; path='root'; "
        "endpoint_route=()",
    ),
    (
        events.AttemptConditionConfirmed(
            job_ref="job:one", execution_attempt_ref="attempt:one",
            context="interpreter",
            condition_code="interpreter_deadline_exceeded",
            updated_at="2026-10-05T20:00:00Z",
        ),
        "provider_event='attempt_condition_confirmed'; job_ref='job:one'; "
        "execution_attempt_ref='attempt:one'; "
        "context='interpreter'; "
        "condition_code='interpreter_deadline_exceeded'; "
        "updated_at='2026-10-05T20:00:00Z'",
    ),
    (
        events.AttemptConditionUnconfirmed(
            job_ref="job:one", execution_attempt_ref="attempt:one",
            context="interpreter",
            condition_code="interpreter_deadline_exceeded",
            outcome_type="AttemptMutationCommitPossible",
            evidence_type="ProviderRequestUnavailable", delivery="possible",
        ),
        "provider_event='attempt_condition_unconfirmed'; job_ref='job:one'; "
        "execution_attempt_ref='attempt:one'; "
        "context='interpreter'; "
        "condition_code='interpreter_deadline_exceeded'; "
        "outcome_type='AttemptMutationCommitPossible'; "
        "evidence_type='ProviderRequestUnavailable'; delivery='possible'",
    ),
    (
        events.ExecutionObservationLost(
            job_ref="job:one", execution_attempt_ref="attempt:one",
            evidence_type="ProviderRequestUnavailable", delivery="possible",
        ),
        "provider_event='execution_observation_lost'; job_ref='job:one'; "
        "execution_attempt_ref='attempt:one'; "
        "evidence_type='ProviderRequestUnavailable'; delivery='possible'",
    ),
    (
        events.TerminalRecoveryHeld(
            job_ref="job:one", execution_attempt_ref="attempt:one",
            operation="fail", command_fingerprint="sha256:" + "b" * 64,
            delivery="unconfirmed", automatic_resends="stopped_including_restart",
            automatic_reads="stopped", new_work_for_attempt="stopped",
            action="do_not_resend",
            description="Attempt expired.", observed_state="expired",
            next_actor="provider_operator", next_action="reconcile original command",
        ),
        "provider_event='terminal_recovery_held'; job_ref='job:one'; "
        "execution_attempt_ref='attempt:one'; operation='fail'; "
        "command_fingerprint='sha256:" + "b" * 64 + "'; "
        "delivery='unconfirmed'; automatic_resends='stopped_including_restart'; "
        "automatic_reads='stopped'; new_work_for_attempt='stopped'; "
        "action='do_not_resend'; "
        "description='Attempt expired.'; observed_state='expired'; "
        "next_actor='provider_operator'; next_action='reconcile original command'",
    ),
)


class ProviderEventTests(unittest.TestCase):
    def test_catalogue_is_exhaustive_and_every_message_is_bounded(self) -> None:
        self.assertEqual(
            tuple(type(event) for event, _prefix in _CASES),
            events.PROVIDER_EVENT_TYPES,
        )
        for event, prefix in _CASES:
            with self.subTest(event=type(event).__name__):
                rendered = events.render_provider_event(event)
                self.assertEqual(rendered, prefix)
                self.assertLessEqual(
                    len(rendered.encode("utf-8")), events.MAX_PROVIDER_EVENT_BYTES
                )
                self.assertFalse(hasattr(event, "__dict__"))
                if fields(event):
                    with self.assertRaises(FrozenInstanceError):
                        setattr(event, fields(event)[0].name, "changed")

    def test_optional_none_is_omitted_and_observed_none_is_explicit(self) -> None:
        endpoint = events.render_provider_event(_CASES[0][0])
        self.assertNotIn("failure_state", endpoint)
        route = events.render_provider_event(events.InterpreterRoute(
            execution_attempt_ref="attempt:one", disposition="unavailable",
            configuration_id=None, attempted_configuration_ids=(),
        ))
        self.assertIn("configuration_id=None", route)

    def test_foreign_types_wrong_scalars_and_unbounded_text_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            class ForeignEvent(events.ProviderEvent):
                EVENT_CODE = "foreign"
        with self.assertRaises(events.ProviderEventError):
            events.InterpreterEndpointFailed(
                execution_attempt_ref="attempt:one",
                configuration_id=1,  # type: ignore[arg-type]
                failure_kind="transport", failure_reason="failed",
            )
        with self.assertRaises(events.ProviderEventError):
            events.InterpreterEndpointFailed(
                execution_attempt_ref="attempt:one",
                configuration_id="x" * 4097,
                failure_kind="transport", failure_reason="failed",
            )


if __name__ == "__main__":
    unittest.main()
