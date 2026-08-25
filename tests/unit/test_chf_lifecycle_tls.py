"""Prove one complete CHF lifecycle across the released signed TLS boundary."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nmrpeak_provider.attempt_journal import prepared_terminal_replay
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.chf_lifecycle import (
    ChfCandidatesGenerated,
    ChfCompletionPending,
    ChfJobAdmitted,
    ChfObservationPolicy,
    ChfPreparedForExecution,
    ChfStartContinues,
    ChfTerminalDelivered,
    admit_next_chf_job,
    deliver_chf_terminal,
    execute_prepared_chf,
    prepare_chf_execution,
    reconcile_chf_record,
    select_chf_completion,
    start_chf_attempt,
)
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CONTRACT_ID
from nmrpeak_provider.runner_protocol import ReadyFrame
from nmrpeak_provider.chf_runner_session import ChfRunnerDeadlines, ChfRunnerSession
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
)
from nmrpeak_provider.provider_api import ProviderApiClient
from nmrpeak_provider.provider_https import ProviderHttpsEndpoint
from nmrpeak_provider.provider_outcomes import (
    AttemptMutationCommitPossible,
    AttemptMutationCommitted,
    interpret_execution_attempt_complete,
)
from nmrpeak_provider.run_generation import CreatedAtWindow, RunGenerationIdentity
from tests.fakes.chf_runner import FakeChfRunnerChannel
from tests.fakes.provider_server import ChfServerA, serve_chf_server_a
from tests.fakes.tls_certificates import write_test_certificates


_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_CREDENTIAL_REF = "credential:provider:nmrpeak-test"
_FROZEN_GENERATION_ID = "sha256:" + "4" * 64
_RUNNER_FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "5" * 64,
    image_input_ref="sha256:" + "6" * 64,
)


class ChfLifecycleTlsTests(unittest.TestCase):
    def test_selected_job_completes_through_signed_tls_and_exact_replay(self) -> None:
        canonical_input = _valid_chf_input()
        state = ChfServerA(
            canonical_input=canonical_input,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_certificates(root)
            journal_root = root / "journal"
            journal_root.mkdir(mode=0o700)
            with (
                serve_chf_server_a(
                    state=state,
                    certificate_directory=root,
                ) as port,
                AttemptJournalStore(journal_root, maximum_records=1) as journal,
            ):
                api = _api(port, root)
                generation = _generation()
                admitted = admit_next_chf_job(
                    api=api,
                    journal=journal,
                    generation=generation,
                    frozen_generation_id=_FROZEN_GENERATION_ID,
                )
                self.assertIs(type(admitted), ChfJobAdmitted, repr(admitted))
                started = start_chf_attempt(
                    api=api,
                    journal=journal,
                    generation=generation,
                    frozen_generation_id=_FROZEN_GENERATION_ID,
                    record=admitted.record,
                )
                self.assertIs(type(started), ChfStartContinues, repr(started))

                session = _runner_session()
                prepared = prepare_chf_execution(
                    api=api,
                    journal=journal,
                    session=session,
                    record=started.record,
                    canonical_input=admitted.canonical_input,
                )
                self.assertIs(type(prepared), ChfPreparedForExecution, repr(prepared))
                generated = execute_prepared_chf(
                    api=api,
                    journal=journal,
                    session=session,
                    prepared=prepared,
                    observation=ChfObservationPolicy(0.01, 0.2),
                )
                self.assertIs(type(generated), ChfCandidatesGenerated, repr(generated))
                completion = select_chf_completion(
                    journal=journal,
                    generated=generated,
                )
                self.assertIs(type(completion), ChfCompletionPending)
                delivered = deliver_chf_terminal(
                    api=api,
                    journal=journal,
                    record=completion.record,
                )
                self.assertIs(type(delivered), ChfTerminalDelivered, repr(delivered))
                self.assertEqual(journal.records(), ())

                replay = prepared_terminal_replay(completion.record)
                replayed = interpret_execution_attempt_complete(replay, api.send(replay))
                self.assertIs(type(replayed), AttemptMutationCommitted, repr(replayed))
                self.assertTrue(replayed.receipt.replayed)

        self.assertEqual(state.failures, [])
        self.assertIsNotNone(state.attempt)
        self.assertEqual(state.attempt.state, "succeeded")
        self.assertEqual(state.attempt.job_state, "closed")
        self.assertEqual(state.attempt.progress_phase, "running")
        terminal_requests = [
            body
            for method, target, body in state.requests
            if method == "POST" and target == "/provider/v1/execution-attempts/complete"
        ]
        self.assertEqual(len(terminal_requests), 2)
        self.assertEqual(terminal_requests[0], terminal_requests[1])

    def test_lost_mutation_responses_reconcile_after_journal_reopen(self) -> None:
        canonical_input = _valid_chf_input()
        state = ChfServerA(canonical_input=canonical_input)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_certificates(root)
            journal_root = root / "journal"
            journal_root.mkdir(mode=0o700)
            with serve_chf_server_a(state=state, certificate_directory=root) as port:
                api = _api(port, root)
                generation = _generation()
                with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                    admitted = admit_next_chf_job(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=_FROZEN_GENERATION_ID,
                    )
                    self.assertIs(type(admitted), ChfJobAdmitted, repr(admitted))
                    state.lose_next_start_response()
                    uncertain_start = start_chf_attempt(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=_FROZEN_GENERATION_ID,
                        record=admitted.record,
                    )
                    self.assertIs(type(uncertain_start), AttemptMutationCommitPossible)
                    self.assertEqual(journal.records(), (admitted.record,))

                with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                    resumed = reconcile_chf_record(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=_FROZEN_GENERATION_ID,
                        record=journal.records()[0],
                    )
                    self.assertIs(type(resumed), ChfStartContinues, repr(resumed))
                    session = _runner_session()
                    prepared = prepare_chf_execution(
                        api=api,
                        journal=journal,
                        session=session,
                        record=resumed.record,
                        canonical_input=admitted.canonical_input,
                    )
                    self.assertIs(type(prepared), ChfPreparedForExecution, repr(prepared))
                    generated = execute_prepared_chf(
                        api=api,
                        journal=journal,
                        session=session,
                        prepared=prepared,
                        observation=ChfObservationPolicy(0.01, 0.2),
                    )
                    self.assertIs(type(generated), ChfCandidatesGenerated, repr(generated))
                    completion = select_chf_completion(
                        journal=journal,
                        generated=generated,
                    )
                    state.lose_next_completion_response()
                    uncertain_completion = deliver_chf_terminal(
                        api=api,
                        journal=journal,
                        record=completion.record,
                    )
                    self.assertIs(
                        type(uncertain_completion),
                        AttemptMutationCommitPossible,
                    )
                    self.assertEqual(journal.records(), (completion.record,))

                with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                    recovered = reconcile_chf_record(
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=_FROZEN_GENERATION_ID,
                        record=journal.records()[0],
                    )
                    self.assertIs(type(recovered), ChfTerminalDelivered, repr(recovered))
                    self.assertTrue(recovered.receipt.replayed)
                    self.assertEqual(journal.records(), ())

        self.assertEqual(state.failures, [])
        start_bodies = [
            body
            for method, target, body in state.requests
            if method == "POST" and target == "/provider/v1/execution-attempts/start"
        ]
        completion_bodies = [
            body
            for method, target, body in state.requests
            if method == "POST" and target == "/provider/v1/execution-attempts/complete"
        ]
        self.assertEqual(len(start_bodies), 2)
        self.assertEqual(start_bodies[0], start_bodies[1])
        self.assertEqual(len(completion_bodies), 2)
        self.assertEqual(completion_bodies[0], completion_bodies[1])


def _generation() -> RunGenerationIdentity:
    return RunGenerationIdentity(
        provider_ref="provider:nmrpeak",
        analysis_kind_ref="mol_from_1h_13c_formula",
        generation_id="chf-generation",
        scope=CreatedAtWindow(
            datetime(2026, 8, 24, tzinfo=UTC),
            datetime(2026, 8, 26, tzinfo=UTC),
        ),
    )


def _api(port: int, root: Path) -> ProviderApiClient:
    return ProviderApiClient(
        endpoint=ProviderHttpsEndpoint(
            origin=f"https://localhost:{port}",
            expected_topology="dev-local",
            connect_timeout_seconds=1,
            io_deadline_seconds=1,
            ca_file=root / "ca.pem",
        ),
        credential_ref=_CREDENTIAL_REF,
        private_key=_PRIVATE_KEY,
    )


def _runner_session() -> ChfRunnerSession:
    ready = ReadyFrame(
        boot_generation="boot:" + "1" * 32,
        runner_ref="nmrpeak_chf_v1",
        runner_contract_id=_RUNNER_FACTS.runner_contract_id,
        release_sha256=_RUNNER_FACTS.checkpoint_ref,
        source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
        image_input_id=_RUNNER_FACTS.image_input_ref,
        target="cpu-x86_64",
        device="cpu",
        decode_policy_id="nmrpeak_chf_decode_v1",
    )
    return ChfRunnerSession.admit(
        FakeChfRunnerChannel(ready),
        _RUNNER_FACTS,
        ChfRunnerDeadlines(0.2, 0.2, 0.2, 0.2, 0.2),
    )


def _valid_chf_input() -> bytes:
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


if __name__ == "__main__":
    unittest.main()
