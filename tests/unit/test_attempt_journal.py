"""Preserve exact historical Attempt records independently of live recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
import unittest

from nmrpeak_provider import attempt_journal
from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LatestDiagnostic,
    LocalExecutionPhase,
    StartPending,
    TerminalOperation,
    TerminalPending,
    journal_record_bytes,
    journal_record_name,
    parse_journal_record,
)
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.interpreter import InterpreterUnavailableReason
from nmrpeak_provider.product_input import InputRejectionReason
from tests.fakes.retained_journal import complete_command, failure_command, entered_record, retained_terminal
from nmrpeak_provider.runner_protocol import RunnerRejectionReason


ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64
OTHER_ATTEMPT_REF = "execution_attempt:sha256:" + "b" * 64


class AttemptJournalRecordTests(unittest.TestCase):
    def test_persisted_diagnostic_reason_catalog_is_exhaustive(self) -> None:
        expected = {
            *(reason.value for reason in InputRejectionReason),
            *(reason.value for reason in RunnerRejectionReason),
            *(reason.value for reason in InterpreterUnavailableReason),
            "model_report",
            "runner_candidate_rejected",
        }
        self.assertEqual(attempt_journal._DIAGNOSTIC_REASONS, expected)

    def test_legacy_v1_record_bytes_and_digest_are_unchanged(self) -> None:
        active = active_attempt()
        raw = journal_record_bytes(active)
        self.assertEqual(
            sha256(raw).hexdigest(),
            "75cbff263ac543f6cc82331bef71c6b4dfa32c595ab5dcab87b899734a30ba89",
        )
        self.assertEqual(parse_journal_record(raw), active)
        self.assertEqual(journal_record_bytes(parse_journal_record(raw)), raw)

    def test_v2_diagnostic_and_exact_terminal_bytes_survive_historical_round_trip(self) -> None:
        diagnostic = LatestDiagnostic(
            stage="preparation", kind="candidate_issue", producer="interpreter_candidate",
            reason="unsupported_multiplicity", path="/model_input/spectra/1H/peaks/2/multiplicity",
            endpoint_route=("primary", "fallback"), observed_at="2026-10-04T12:00:00+00:00",
        )
        active = replace(active_attempt(), latest_diagnostic=diagnostic)
        self.assertEqual(json.loads(journal_record_bytes(active))["schema_id"], "nmrpeak.attempt_journal_record.v2")
        self.assertEqual(parse_journal_record(journal_record_bytes(active)), active)
        entered = entered_record(active)
        command = failure_command(
            execution_attempt_ref=ATTEMPT_REF, failure_code="interpretation_failed",
            failure_message="The interpreted candidate was rejected.",
        )
        terminal = retained_terminal(entered, command)
        self.assertEqual(terminal.latest_diagnostic, diagnostic)
        self.assertEqual(terminal.terminal_request_body, command.body)
        for record in (terminal, replace(terminal, terminal_hold_action="do_not_resend",
                              terminal_hold_description="Investigate before resending.")):
            self.assertEqual(parse_journal_record(journal_record_bytes(record)), record)
            self.assertEqual(record.terminal_request_body, command.body)

    def test_v2_diagnostic_shape_is_closed_and_never_contains_source_text(self) -> None:
        diagnostic = LatestDiagnostic(
            stage="preparation", kind="direct_source_issue", producer="provider",
            reason="unsupported_multiplicity", path="/model_input/spectra/1H/peaks/0/multiplicity",
            observed_at="2026-10-04T12:00:00+00:00",
        )
        document = json.loads(journal_record_bytes(replace(active_attempt(), latest_diagnostic=diagnostic)))
        for change in (
            {"latest_diagnostic": None},
            {"latest_diagnostic": document["latest_diagnostic"] | {"source": "secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"path": "/secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"stage": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"kind": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"producer": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"reason": "secret\nvalue"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"reason": "private_secret"}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"endpoint_route": ["bad endpoint"]}},
            {"latest_diagnostic": document["latest_diagnostic"] | {"observed_at": "2026-10-04T12:00:00"}},
        ):
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                parse_journal_record(canonical_json_bytes(document | change))
        with self.assertRaises(ValueError):
            parse_journal_record(canonical_json_bytes(document | {"schema_id": "nmrpeak.attempt_journal_record.v1"}))

    def test_every_record_variant_has_one_canonical_round_trip(self) -> None:
        start = start_pending()
        active = active_attempt()
        entered = entered_record(active)
        terminal = retained_terminal(
            entered,
            complete_command(
                execution_attempt_ref=ATTEMPT_REF,
                result_schema_id="nmrpeak.structure_candidates.result.v1",
                canonical_result=b'{"candidate":"C"}',
            ),
        )
        for record in (start, active, entered, terminal):
            with self.subTest(record_type=type(record).__name__):
                raw = journal_record_bytes(record)
                self.assertEqual(parse_journal_record(raw), record)
                self.assertNotIn(terminal.terminal_request_body, repr(record).encode())
        self.assertEqual("1" * 64 + ".json", journal_record_name(start))

    def test_hold_is_part_of_the_exact_record_identity(self) -> None:
        terminal = retained_terminal(active_attempt(), failure_command(
            execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected",
            failure_message="The input is not supported."))
        held = replace(terminal, terminal_hold_action="do_not_resend",
                       terminal_hold_description="This command cannot be applied.")
        self.assertNotEqual(terminal, held, "Stale callers must not retire a newly held command")
        self.assertEqual(parse_journal_record(journal_record_bytes(held)), held)

    def test_held_record_cannot_downgrade_or_admit_unbounded_evidence(self) -> None:
        terminal = retained_terminal(active_attempt(), failure_command(
            execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected",
            failure_message="The input is not supported."))
        document = json.loads(journal_record_bytes(replace(terminal, terminal_hold_action="do_not_resend",
            terminal_hold_description="This command cannot be applied.")))
        for change in ({"terminal_hold_action": None}, {"terminal_hold_description": None},
                       {"terminal_hold_description": ""}, {"terminal_hold_description": "é" * 2049}):
            with self.subTest(field=next(iter(change))), self.assertRaises((TypeError, ValueError)):
                parse_journal_record(canonical_json_bytes(document | change))
        with self.assertRaises((TypeError, ValueError)):
            replace(terminal, terminal_hold_description="An action is required.")

    def test_record_loader_rejects_shape_version_and_identity_drift(self) -> None:
        document = json.loads(journal_record_bytes(start_pending()))
        cases = (
            document | {"extra": True},
            document | {"schema_id": "nmrpeak.attempt_journal_record.v2"},
            document | {"provider_attempt_key": "foreign-key"},
            document | {"input_fingerprint": "sha256:short"},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    parse_journal_record(canonical_json_bytes(changed))
        with self.assertRaises(ValueError):
            parse_journal_record(journal_record_bytes(start_pending()) + b"\n")

    def test_terminal_loader_binds_digest_operation_and_attempt(self) -> None:
        terminal = retained_terminal(
            active_attempt(),
            failure_command(
                execution_attempt_ref=ATTEMPT_REF,
                failure_code="input_rejected",
                failure_message="The input is not supported.",
            ),
        )
        document = json.loads(journal_record_bytes(terminal))
        cases = (
            document | {"terminal_request_fingerprint": "sha256:" + "0" * 64},
            document | {"terminal_operation": "complete"},
            document | {"execution_attempt_ref": OTHER_ATTEMPT_REF},
            document | {"terminal_request_base64": "not+canonical"},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    parse_journal_record(canonical_json_bytes(changed))


def start_pending() -> StartPending:
    return StartPending(
        job_ref="job:test",
        provider_attempt_key="nmrpeak-provider.v1:" + "1" * 64,
        input_fingerprint="sha256:" + "2" * 64,
        frozen_generation_id="sha256:" + "3" * 64,
    )


def active_attempt() -> ActiveAttempt:
    return ActiveAttempt(
        job_ref="job:test",
        provider_attempt_key="nmrpeak-provider.v1:" + "1" * 64,
        input_fingerprint="sha256:" + "2" * 64,
        frozen_generation_id="sha256:" + "3" * 64,
        execution_attempt_ref=ATTEMPT_REF,
        local_phase=LocalExecutionPhase.PRE_EXECUTION,
    )


if __name__ == "__main__":
    unittest.main()


class TerminalReconciliationJournalTests(unittest.TestCase):
    def test_pending_kind_is_closed_and_canonical_api_evidence_round_trips(self):
        terminal = retained_terminal(active_attempt(), failure_command(
            execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected", failure_message="Input unsupported."))
        pending = replace(terminal, terminal_hold_action="reconcile_state", terminal_hold_description="Reconcile state.",
                          terminal_hold_code="operation_conflict", terminal_hold_detail="A valid\u00a0detail.", terminal_hold_request_id="request:test")
        document = json.loads(journal_record_bytes(pending))
        self.assertEqual(document["record_kind"], "terminal_reconciling")
        self.assertEqual(parse_journal_record(journal_record_bytes(pending)), pending)
        for change in ({"record_kind": "terminal_hold"}, {"terminal_observed_state": "expired"},
                       {"terminal_hold_action": "do_not_resend"}, {"terminal_hold_detail": " leading"},
                       {"terminal_hold_request_id": "bad request"}, {"terminal_hold_code": "unknown_ascii_code"}):
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                parse_journal_record(canonical_json_bytes(document | change))
