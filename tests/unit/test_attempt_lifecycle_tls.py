"""Prove one complete NMRPeak lifecycle across the released signed TLS boundary."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nmrpeak_provider.attempt_journal import prepared_terminal_replay
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.attempt_lifecycle import (
    CandidatesGenerated,
    CompletionPending,
    JobAdmitted,
    ObservationPolicy,
    PreparedForExecution,
    StartContinues,
    TerminalDelivered,
    admit_next_job,
    deliver_terminal,
    execute_prepared,
    prepare_execution,
    reconcile_record,
    select_completion,
    start_attempt,
)
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CODEC,
    CHF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.hf_runner_protocol import (
    HF_RUNNER_CODEC,
    HF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.generation_runtime import GenerationLane, GenerationRuntime
from nmrpeak_provider.lifecycle_lane import (
    CHF_LIFECYCLE_LANE,
    HF_LIFECYCLE_LANE,
)
from nmrpeak_provider.runner_protocol import ReadyFrame, RunnerFrameCodec
from nmrpeak_provider.runner_session import RunnerDeadlines, RunnerSession
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
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
from tests.fakes.runner import FakeRunnerChannel
from tests.fakes.provider_server import ServerA, serve_server_a
from tests.fakes.tls_certificates import write_test_certificates


_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_CREDENTIAL_REF = "credential:provider:nmrpeak-test"
_FROZEN_GENERATION_ID = "sha256:" + "4" * 64
_CHF_RUNNER_FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "5" * 64,
    image_input_ref="sha256:" + "6" * 64,
)
_HF_RUNNER_FACTS = ProviderResultFacts(
    identity=HF_RESULT_IDENTITY,
    runner_contract_id=HF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "7" * 64,
    image_input_ref="sha256:" + "8" * 64,
)


class AttemptLifecycleTlsTests(unittest.TestCase):
    def test_each_lane_completes_through_signed_tls_and_exact_replay(self) -> None:
        cases = (
            (
                CHF_LIFECYCLE_LANE,
                CHF_RUNNER_CODEC,
                _CHF_RUNNER_FACTS,
                _valid_chf_input(),
            ),
            (
                HF_LIFECYCLE_LANE,
                HF_RUNNER_CODEC,
                _HF_RUNNER_FACTS,
                _valid_hf_input(),
            ),
        )
        for lane, codec, facts, canonical_input in cases:
            with self.subTest(implementation=lane.offering.implementation_ref):
                state = ServerA(
                    analysis_kind_ref=lane.offering.analysis_kind_ref,
                    canonical_input=canonical_input,
                )
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_test_certificates(root)
                    journal_root = root / "journal"
                    journal_root.mkdir(mode=0o700)
                    with (
                        serve_server_a(
                            state=state,
                            certificate_directory=root,
                        ) as port,
                        AttemptJournalStore(
                            journal_root,
                            maximum_records=1,
                        ) as journal,
                    ):
                        api = _api(port, root)
                        generation = _generation(
                            lane.offering.analysis_kind_ref,
                            lane.offering.implementation_ref,
                        )
                        admitted = admit_next_job(
                            lane=lane,
                            api=api,
                            journal=journal,
                            generation=generation,
                            frozen_generation_id=_FROZEN_GENERATION_ID,
                        )
                        self.assertIs(type(admitted), JobAdmitted, repr(admitted))
                        started = start_attempt(
                            lane=lane,
                            api=api,
                            journal=journal,
                            generation=generation,
                            frozen_generation_id=_FROZEN_GENERATION_ID,
                            record=admitted.record,
                        )
                        self.assertIs(type(started), StartContinues, repr(started))

                        session = _runner_session(codec, facts)
                        prepared = prepare_execution(
                            lane=lane,
                            api=api,
                            journal=journal,
                            session=session,
                            record=started.record,
                            canonical_input=admitted.canonical_input,
                        )
                        self.assertIs(
                            type(prepared),
                            PreparedForExecution,
                            repr(prepared),
                        )
                        generated = execute_prepared(
                            api=api,
                            journal=journal,
                            session=session,
                            prepared=prepared,
                            observation=ObservationPolicy(0.01, 0.2),
                        )
                        self.assertIs(
                            type(generated),
                            CandidatesGenerated,
                            repr(generated),
                        )
                        completion = select_completion(
                            journal=journal,
                            generated=generated,
                        )
                        self.assertIs(type(completion), CompletionPending)
                        delivered = deliver_terminal(
                            api=api,
                            journal=journal,
                            record=completion.record,
                        )
                        self.assertIs(
                            type(delivered),
                            TerminalDelivered,
                            repr(delivered),
                        )
                        self.assertEqual(journal.records(), ())

                        replay = prepared_terminal_replay(completion.record)
                        replayed = interpret_execution_attempt_complete(
                            replay,
                            api.send(replay),
                        )
                        self.assertIs(
                            type(replayed),
                            AttemptMutationCommitted,
                            repr(replayed),
                        )
                        self.assertTrue(replayed.receipt.replayed)

                self.assertEqual(state.failures, [])
                self.assertIsNotNone(state.attempt)
                self.assertEqual(state.attempt.state, "succeeded")
                self.assertEqual(state.attempt.job_state, "closed")
                self.assertEqual(state.attempt.progress_phase, "running")
                terminal_requests = [
                    body
                    for method, target, body in state.requests
                    if method == "POST"
                    and target == "/provider/v1/execution-attempts/complete"
                ]
                self.assertEqual(len(terminal_requests), 2)
                self.assertEqual(terminal_requests[0], terminal_requests[1])

    def test_lost_mutation_responses_reconcile_after_journal_reopen(self) -> None:
        canonical_input = _valid_chf_input()
        state = ServerA(
            analysis_kind_ref=CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
            canonical_input=canonical_input,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_test_certificates(root)
            journal_root = root / "journal"
            journal_root.mkdir(mode=0o700)
            with serve_server_a(state=state, certificate_directory=root) as port:
                api = _api(port, root)
                generation = _generation(
                    CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                    CHF_LIFECYCLE_LANE.offering.implementation_ref,
                )
                runtime = _generation_runtime(chf=generation)
                with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                    admitted = admit_next_job(
                        lane=CHF_LIFECYCLE_LANE,
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=_FROZEN_GENERATION_ID,
                    )
                    self.assertIs(type(admitted), JobAdmitted, repr(admitted))
                    state.lose_next_start_response()
                    uncertain_start = start_attempt(
                        lane=CHF_LIFECYCLE_LANE,
                        api=api,
                        journal=journal,
                        generation=generation,
                        frozen_generation_id=_FROZEN_GENERATION_ID,
                        record=admitted.record,
                    )
                    self.assertIs(type(uncertain_start), AttemptMutationCommitPossible)
                    self.assertEqual(journal.records(), (admitted.record,))

                with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                    resumed = reconcile_record(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        record=journal.records()[0],
                    )
                    self.assertIs(type(resumed), StartContinues, repr(resumed))
                    session = _runner_session(
                        CHF_RUNNER_CODEC,
                        _CHF_RUNNER_FACTS,
                    )
                    prepared = prepare_execution(
                        lane=CHF_LIFECYCLE_LANE,
                        api=api,
                        journal=journal,
                        session=session,
                        record=resumed.record,
                        canonical_input=admitted.canonical_input,
                    )
                    self.assertIs(type(prepared), PreparedForExecution, repr(prepared))
                    generated = execute_prepared(
                        api=api,
                        journal=journal,
                        session=session,
                        prepared=prepared,
                        observation=ObservationPolicy(0.01, 0.2),
                    )
                    self.assertIs(type(generated), CandidatesGenerated, repr(generated))
                    completion = select_completion(
                        journal=journal,
                        generated=generated,
                    )
                    state.lose_next_completion_response()
                    uncertain_completion = deliver_terminal(
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
                    recovered = reconcile_record(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        record=journal.records()[0],
                    )
                    self.assertIs(type(recovered), TerminalDelivered, repr(recovered))
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


def _generation(
    analysis_kind_ref: str,
    implementation_ref: str,
) -> RunGenerationIdentity:
    return RunGenerationIdentity(
        provider_ref="provider:nmrpeak",
        analysis_kind_ref=analysis_kind_ref,
        generation_id=f"{implementation_ref}-generation",
        scope=CreatedAtWindow(
            datetime(2026, 8, 24, tzinfo=UTC),
            datetime(2026, 8, 26, tzinfo=UTC),
        ),
    )


def _generation_runtime(*, chf: RunGenerationIdentity) -> GenerationRuntime:
    return GenerationRuntime(
        frozen_generation_id=_FROZEN_GENERATION_ID,
        hf=GenerationLane(
            lane=HF_LIFECYCLE_LANE,
            generation=_generation(
                HF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                HF_LIFECYCLE_LANE.offering.implementation_ref,
            ),
            result_facts=_HF_RUNNER_FACTS,
            runner_codec=HF_RUNNER_CODEC,
        ),
        chf=GenerationLane(
            lane=CHF_LIFECYCLE_LANE,
            generation=chf,
            result_facts=_CHF_RUNNER_FACTS,
            runner_codec=CHF_RUNNER_CODEC,
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


def _runner_session(
    codec: RunnerFrameCodec,
    facts: ProviderResultFacts,
) -> RunnerSession:
    ready = ReadyFrame(
        boot_generation="boot:" + "1" * 32,
        runner_ref=facts.identity.runner_ref,
        runner_contract_id=facts.runner_contract_id,
        release_sha256=facts.checkpoint_ref,
        source_closure_sha256=NMRPEAK_SOURCE_CLOSURE_REF,
        image_input_id=facts.image_input_ref,
        target="cpu-x86_64",
        device="cpu",
        decode_policy_id=facts.identity.decode_policy.decode_policy_id,
    )
    return RunnerSession.admit(
        FakeRunnerChannel(codec, ready),
        facts,
        RunnerDeadlines(0.2, 0.2, 0.2, 0.2, 0.2),
        codec,
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


def _valid_hf_input() -> bytes:
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
                },
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
