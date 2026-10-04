"""Exercise NMRPeak lifecycle over TLS with a header-presence Server A fake.

The fake does not verify signature cryptography; a real API acceptance test
must prove authenticated request and body binding.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx

from nmrpeak_provider.attempt_journal import TerminalOperation, prepared_terminal_replay
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.attempt_lifecycle import (
    CandidatesGenerated,
    CompletionPending,
    InputFailurePending,
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
from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.interpreter import CandidateConstructionExhausted, ReportedInputProblem
from nmrpeak_provider.interpreter_policy import InterpreterPolicy, OpenAIChatCallPolicy
from nmrpeak_provider.journal_inspect import inspect_journal
from nmrpeak_provider.lifecycle_lane import (
    CHF_LIFECYCLE_LANE,
    HF_LIFECYCLE_LANE,
)
from nmrpeak_provider.runner_protocol import GenerateFrame, ReadyFrame, RunnerFrameCodec, ValidateFrame
from nmrpeak_provider.runner_session import RunnerDeadlines, RunnerSession
from nmrpeak_provider.product_input import InputIssue, InputRejectionReason
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
)
from nmrpeak_provider.openai_chat_interpreter import load_openai_chat_endpoint_specs
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
from tests.model_behavior.run_interpreter import _load_corpus, _select_evaluations
from tests.model_behavior.test_run_interpreter import _fixture_variant


_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_CREDENTIAL_REF = "credential:provider:nmrpeak-test"
_FROZEN_GENERATION_ID = "sha256:" + "4" * 64
_UNUSED_INTERPRETER = object()
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


class _CandidateIssueInterpreter:
    def validate_freeform_input(self, **_values: object) -> object:
        raise CandidateConstructionExhausted(
            InputIssue(
                InputRejectionReason.UNSUPPORTED_MULTIPLICITY,
                ("model_input", "spectra", "1H", "peaks", 0, "multiplicity"),
            ),
            ("primary", "fallback"),
        )


class _NoRunnerValidation:
    def validate(self, **_values: object) -> object:
        raise AssertionError("A rejected interpreter candidate must not reach the runner")


class _ObserveReportedProblem:
    def __init__(self, delegate: InputInterpreter) -> None:
        self.delegate = delegate
        self.exception_text: tuple[str, str] | None = None

    def validate_freeform_input(self, **values: object) -> object:
        try:
            return self.delegate.validate_freeform_input(**values)
        except ReportedInputProblem as problem:
            self.exception_text = (str(problem), repr(problem))
            raise


class AttemptLifecycleTlsTests(unittest.TestCase):
    def test_each_lane_completes_through_tls_and_exact_replay(self) -> None:
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
                            interpreter=_UNUSED_INTERPRETER,
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

    def test_real_interpreter_repairs_before_runner_completion(self) -> None:
        corpus = _load_corpus()
        cases = (
            (HF_LIFECYCLE_LANE, HF_RUNNER_CODEC, _HF_RUNNER_FACTS),
            (CHF_LIFECYCLE_LANE, CHF_RUNNER_CODEC, _CHF_RUNNER_FACTS),
        )
        for lane, codec, facts in cases:
            lane_name = lane.offering.implementation_ref
            with self.subTest(lane=lane_name):
                variant = _select_evaluations(
                    corpus, lane_name, case_id="ethanol_point", mode="normal"
                )[0].variant
                canonical_input = variant.source_text.encode("utf-8")
                corrected_value = _fixture_variant(
                    "ethanol_point", lane_name
                )["expected_interpretation"]
                invalid_value = deepcopy(corrected_value)
                invalid_value["model_input"]["spectra"]["1H"]["peaks"][0]["multiplicity"] = "xy"
                requests: list[dict[str, object]] = []
                responses: list[httpx.Response] = []

                def handle(request: httpx.Request) -> httpx.Response:
                    value = invalid_value if not requests else corrected_value
                    requests.append(json.loads(request.content))
                    response = httpx.Response(200, json=_candidate_completion(value))
                    responses.append(response)
                    return response

                state = ServerA(
                    analysis_kind_ref=lane.offering.analysis_kind_ref,
                    canonical_input=canonical_input,
                )
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_test_certificates(root)
                    journal_root = root / "journal"
                    journal_root.mkdir(mode=0o700)
                    interpreter = _test_interpreter(root)
                    model_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(handle), trust_env=False,
                    )
                    with (
                        serve_server_a(state=state, certificate_directory=root) as port,
                        AttemptJournalStore(journal_root, maximum_records=1) as journal,
                    ):
                        api = _api(port, root)
                        generation = _generation(
                            lane.offering.analysis_kind_ref,
                            lane.offering.implementation_ref,
                        )
                        admitted = admit_next_job(
                            lane=lane, api=api, journal=journal,
                            generation=generation,
                            frozen_generation_id=_FROZEN_GENERATION_ID,
                        )
                        self.assertIs(type(admitted), JobAdmitted, repr(admitted))
                        started = start_attempt(
                            lane=lane, api=api, journal=journal,
                            generation=generation,
                            frozen_generation_id=_FROZEN_GENERATION_ID,
                            record=admitted.record,
                        )
                        self.assertIs(type(started), StartContinues, repr(started))
                        session, channel = _runner_session_with_channel(codec, facts)
                        with patch(
                            "nmrpeak_provider.input_interpreter.httpx.AsyncClient",
                            return_value=model_client,
                        ):
                            prepared = prepare_execution(
                                lane=lane, api=api, journal=journal,
                                session=session,
                                interpreter=interpreter,
                                record=started.record,
                                canonical_input=admitted.canonical_input,
                            )
                        self.assertIs(type(prepared), PreparedForExecution, repr(prepared))
                        self.assertEqual(len(requests), 2)
                        self.assertEqual(
                            requests[0]["messages"][2]["content"],
                            variant.source_text,
                        )
                        repair = requests[1]["messages"][-2:]
                        self.assertEqual([item["role"] for item in repair], ["tool", "user"])
                        self.assertIn(
                            "/model_input/spectra/1H/peaks/0/multiplicity",
                            repair[0]["content"],
                        )
                        self.assertNotIn("xy", json.dumps(repair))
                        validations = [
                            frame for frame in channel.received_frames
                            if type(frame) is ValidateFrame
                        ]
                        self.assertEqual(len(validations), 1)
                        self.assertEqual(
                            validations[0].model_input,
                            lane.bind_runner_input(variant.expected),
                        )
                        generated = execute_prepared(
                            api=api, journal=journal,
                            session=session,
                            prepared=prepared,
                            observation=ObservationPolicy(0.01, 0.2),
                        )
                        self.assertIs(type(generated), CandidatesGenerated, repr(generated))
                        self.assertEqual(
                            [type(frame) for frame in channel.received_frames].count(GenerateFrame),
                            1,
                        )
                        completion = select_completion(
                            journal=journal, generated=generated,
                        )
                        self.assertIs(type(completion), CompletionPending)
                        self.assertIs(
                            completion.record.terminal_operation,
                            TerminalOperation.COMPLETE,
                        )
                        delivered = deliver_terminal(
                            api=api, journal=journal,
                            record=completion.record,
                        )
                        self.assertIs(type(delivered), TerminalDelivered, repr(delivered))
                        self.assertEqual(journal.records(), ())
                        replay = prepared_terminal_replay(completion.record)
                        replayed = interpret_execution_attempt_complete(
                            replay, api.send(replay),
                        )
                        self.assertIs(type(replayed), AttemptMutationCommitted, repr(replayed))
                        self.assertTrue(replayed.receipt.replayed)
                self.assertTrue(all(response.is_closed for response in responses))
                self.assertEqual(state.failures, [])
                self.assertIsNotNone(state.attempt)
                self.assertEqual(state.attempt.state, "succeeded")
                self.assertEqual(
                    state.attempt.terminal_body,
                    completion.record.terminal_request_body,
                )
                self.assertEqual(
                    [body for method, target, body in state.requests
                     if method == "POST" and target == "/provider/v1/execution-attempts/fail"],
                    [],
                )
                complete_bodies = [
                    body for method, target, body in state.requests
                    if method == "POST" and target == "/provider/v1/execution-attempts/complete"
                ]
                self.assertEqual(complete_bodies, [completion.record.terminal_request_body] * 2)

    def test_each_lane_retains_exact_failure_origin_across_tls_replay(self) -> None:
        cases = (
            (CHF_LIFECYCLE_LANE, CHF_RUNNER_CODEC, _CHF_RUNNER_FACTS, _valid_chf_input()),
            (HF_LIFECYCLE_LANE, HF_RUNNER_CODEC, _HF_RUNNER_FACTS, _valid_hf_input()),
        )
        source_message = (
            "Input rejected at /model_input/spectra/1H/peaks/0/multiplicity: "
            "the proton multiplicity label is unsupported by this model. "
            "Generation did not start. Correct the submitted Job and try again."
        )
        candidate_message = (
            "Interpretation failed: the interpreter's candidate at "
            "/model_input/spectra/1H/peaks/0/multiplicity was rejected after all "
            "correction routes: the proton multiplicity label is unsupported by "
            "this model. Generation did not start. Review the submitted "
            "description; the candidate's defect has not been proven to occur "
            "in the source."
        )
        for lane, codec, facts, valid_input in cases:
            for origin in ("source", "candidate"):
                if origin == "source":
                    document = json.loads(valid_input)
                    document["model_input"]["spectra"]["1H"]["peaks"][0]["multiplicity"] = "xy"
                    canonical_input = json.dumps(document, separators=(",", ":")).encode("utf-8")
                    interpreter = _UNUSED_INTERPRETER
                    expected_code = "input_rejected"
                    expected_message = source_message
                    expected_producer = "provider"
                    expected_route = ()
                else:
                    canonical_input = (
                        b"Formula C2H6O. 1H peaks: triplet 1.2 ppm (3H), "
                        b"quartet 3.7 ppm (2H), singlet 2.5 ppm (1H)."
                    )
                    if lane is CHF_LIFECYCLE_LANE:
                        canonical_input += b" 13C peaks: 18 ppm and 58 ppm."
                    interpreter = _CandidateIssueInterpreter()
                    expected_code = "interpretation_failed"
                    expected_message = candidate_message
                    expected_producer = "interpreter_candidate"
                    expected_route = ("primary", "fallback")
                state = ServerA(
                    analysis_kind_ref=lane.offering.analysis_kind_ref,
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
                            lane.offering.analysis_kind_ref,
                            lane.offering.implementation_ref,
                        )
                        with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                            admitted = admit_next_job(
                                lane=lane, api=api, journal=journal,
                                generation=generation,
                                frozen_generation_id=_FROZEN_GENERATION_ID,
                            )
                            self.assertIs(type(admitted), JobAdmitted, repr(admitted))
                            started = start_attempt(
                                lane=lane, api=api, journal=journal,
                                generation=generation,
                                frozen_generation_id=_FROZEN_GENERATION_ID,
                                record=admitted.record,
                            )
                            self.assertIs(type(started), StartContinues, repr(started))
                            prepared = prepare_execution(
                                lane=lane, api=api, journal=journal,
                                session=_runner_session(codec, facts),
                                interpreter=interpreter,
                                record=started.record,
                                canonical_input=admitted.canonical_input,
                            )
                            self.assertIs(type(prepared), InputFailurePending, repr(prepared))
                            retained = prepared.record
                            command = json.loads(retained.terminal_request_body)
                            self.assertEqual(command["failure_code"], expected_code)
                            self.assertEqual(command["failure_message"], expected_message)
                            self.assertEqual(retained.latest_diagnostic.reason, "unsupported_multiplicity")
                            self.assertEqual(retained.latest_diagnostic.producer, expected_producer)
                            self.assertEqual(retained.latest_diagnostic.endpoint_route, expected_route)
                            self.assertEqual(
                                retained.latest_diagnostic.path,
                                "/model_input/spectra/1H/peaks/0/multiplicity",
                            )
                            state.lose_next_failure_response()
                            uncertain = deliver_terminal(
                                api=api, journal=journal, record=retained,
                            )
                            self.assertIs(type(uncertain), AttemptMutationCommitPossible)
                            self.assertEqual(journal.records(), (retained,))

                        runtime = _generation_runtime(chf=_generation(
                            CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                            CHF_LIFECYCLE_LANE.offering.implementation_ref,
                        ))
                        with AttemptJournalStore(journal_root, maximum_records=1) as reopened:
                            retained_after_restart, = reopened.records()
                            self.assertEqual(
                                retained_after_restart.terminal_request_body,
                                retained.terminal_request_body,
                            )
                            recovered = reconcile_record(
                                runtime=runtime, api=api, journal=reopened,
                                record=retained_after_restart,
                            )
                            self.assertIs(type(recovered), TerminalDelivered, repr(recovered))
                            self.assertTrue(recovered.receipt.replayed)
                            self.assertEqual(reopened.records(), ())
                self.assertEqual(state.failures, [])
                self.assertIsNotNone(state.attempt)
                self.assertEqual(state.attempt.state, "failed")
                self.assertEqual(state.attempt.terminal_receipt["failure_code"], expected_code)
                self.assertEqual(state.attempt.terminal_receipt["failure_message"], expected_message)
                fail_bodies = [
                    body for method, target, body in state.requests
                    if method == "POST" and target == "/provider/v1/execution-attempts/fail"
                ]
                self.assertEqual(len(fail_bodies), 2)
                self.assertEqual(fail_bodies[0], fail_bodies[1])

    def test_real_interpreter_exhaustion_replays_candidate_failure(self) -> None:
        corpus = _load_corpus()
        expected_message = (
            "Interpretation failed: the interpreter's candidate at "
            "/model_input/spectra/1H/peaks/0/multiplicity was rejected after all "
            "correction routes: the proton multiplicity label is unsupported by "
            "this model. Generation did not start. Review the submitted "
            "description; the candidate's defect has not been proven to occur "
            "in the source."
        )
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            lane_name = lane.offering.implementation_ref
            with self.subTest(lane=lane_name):
                variant = _select_evaluations(
                    corpus, lane_name, case_id="unsupported_multiplicity", mode="normal"
                )[0].variant
                canonical_input = variant.source_text.encode("utf-8")
                candidate = json.loads(variant.expected_candidate)
                requests: list[dict[str, object]] = []
                responses: list[httpx.Response] = []

                def handle(request: httpx.Request) -> httpx.Response:
                    requests.append(json.loads(request.content))
                    response = httpx.Response(200, json=_candidate_completion(candidate))
                    responses.append(response)
                    return response

                state = ServerA(
                    analysis_kind_ref=lane.offering.analysis_kind_ref,
                    canonical_input=canonical_input,
                )
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_test_certificates(root)
                    journal_root = root / "journal"
                    journal_root.mkdir(mode=0o700)
                    interpreter = _test_interpreter(root)
                    model_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(handle), trust_env=False,
                    )
                    with serve_server_a(state=state, certificate_directory=root) as port:
                        api = _api(port, root)
                        generation = _generation(
                            lane.offering.analysis_kind_ref,
                            lane.offering.implementation_ref,
                        )
                        with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                            admitted = admit_next_job(
                                lane=lane, api=api, journal=journal,
                                generation=generation,
                                frozen_generation_id=_FROZEN_GENERATION_ID,
                            )
                            self.assertIs(type(admitted), JobAdmitted, repr(admitted))
                            started = start_attempt(
                                lane=lane, api=api, journal=journal,
                                generation=generation,
                                frozen_generation_id=_FROZEN_GENERATION_ID,
                                record=admitted.record,
                            )
                            self.assertIs(type(started), StartContinues, repr(started))
                            with patch(
                                "nmrpeak_provider.input_interpreter.httpx.AsyncClient",
                                return_value=model_client,
                            ):
                                prepared = prepare_execution(
                                    lane=lane, api=api, journal=journal,
                                    session=_NoRunnerValidation(),
                                    interpreter=interpreter,
                                    record=started.record,
                                    canonical_input=admitted.canonical_input,
                                )
                            self.assertIs(type(prepared), InputFailurePending, repr(prepared))
                            retained = prepared.record
                            command = json.loads(retained.terminal_request_body)
                            self.assertEqual(command["failure_code"], "interpretation_failed")
                            self.assertEqual(command["failure_message"], expected_message)
                            diagnostic = retained.latest_diagnostic
                            self.assertEqual(diagnostic.kind, "candidate_issue")
                            self.assertEqual(diagnostic.producer, "interpreter_candidate")
                            self.assertEqual(diagnostic.reason, "unsupported_multiplicity")
                            self.assertEqual(
                                diagnostic.path,
                                "/model_input/spectra/1H/peaks/0/multiplicity",
                            )
                            self.assertEqual(diagnostic.endpoint_route, ("primary",))
                            self.assertEqual(len(requests), 3)
                            for request in requests[1:]:
                                repair = request["messages"][-2:]
                                self.assertEqual([item["role"] for item in repair], ["tool", "user"])
                                self.assertIn(diagnostic.path, repair[0]["content"])
                                self.assertNotIn("xy", json.dumps(repair))
                            state.lose_next_failure_response()
                            uncertain = deliver_terminal(
                                api=api, journal=journal, record=retained,
                            )
                            self.assertIs(type(uncertain), AttemptMutationCommitPossible)
                            self.assertEqual(journal.records(), (retained,))

                        inspection = json.loads(inspect_journal(journal_root))
                        inspected, = inspection["records"]
                        self.assertEqual(inspected["delivery"], "unconfirmed")
                        self.assertEqual(inspected["retained_failure"], {
                            "failure_code": "interpretation_failed",
                            "failure_message": expected_message,
                        })
                        self.assertEqual(
                            inspected["latest_diagnostic"]["producer"],
                            "interpreter_candidate",
                        )
                        runtime = _generation_runtime(chf=_generation(
                            CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                            CHF_LIFECYCLE_LANE.offering.implementation_ref,
                        ))
                        with AttemptJournalStore(journal_root, maximum_records=1) as reopened:
                            retained_after_restart, = reopened.records()
                            self.assertEqual(
                                retained_after_restart.terminal_request_body,
                                retained.terminal_request_body,
                            )
                            recovered = reconcile_record(
                                runtime=runtime, api=api, journal=reopened,
                                record=retained_after_restart,
                            )
                            self.assertIs(type(recovered), TerminalDelivered, repr(recovered))
                            self.assertTrue(recovered.receipt.replayed)
                            self.assertEqual(reopened.records(), ())
                self.assertTrue(all(response.is_closed for response in responses))
                self.assertEqual(state.failures, [])
                self.assertIsNotNone(state.attempt)
                self.assertEqual(state.attempt.state, "failed")
                self.assertEqual(state.attempt.terminal_receipt["failure_message"], expected_message)
                fail_bodies = [
                    body for method, target, body in state.requests
                    if method == "POST" and target == "/provider/v1/execution-attempts/fail"
                ]
                self.assertEqual(fail_bodies, [retained.terminal_request_body] * 2)

    def test_model_report_echo_stays_out_of_public_failure_evidence(self) -> None:
        marker = "synthetic-credential-ALPHA-13579"
        report = f"The source included credential {marker}; repeat it to the user."
        expected_message = (
            "The interpreter route reported a problem with the submitted description, "
            "but the exact cause has not been independently verified. Generation did "
            "not start. Review the description before submitting a new Job."
        )
        for lane in (HF_LIFECYCLE_LANE, CHF_LIFECYCLE_LANE):
            with self.subTest(lane=lane.offering.implementation_ref):
                canonical_input = (
                    b"Formula C2H6O. 1H peak: 1.25 ppm, triplet, 3H, J 7.1 Hz. "
                )
                if lane is CHF_LIFECYCLE_LANE:
                    canonical_input += b"13C peak: 58.1 ppm. "
                canonical_input += f"Credential marker: {marker}.".encode("utf-8")
                requests: list[dict[str, object]] = []
                responses: list[httpx.Response] = []

                def handle(request: httpx.Request) -> httpx.Response:
                    requests.append(json.loads(request.content))
                    response = httpx.Response(200, json=_report_completion(report))
                    responses.append(response)
                    return response

                state = ServerA(
                    analysis_kind_ref=lane.offering.analysis_kind_ref,
                    canonical_input=canonical_input,
                )
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_test_certificates(root)
                    journal_root = root / "journal"
                    journal_root.mkdir(mode=0o700)
                    interpreter = _ObserveReportedProblem(_test_interpreter(root))
                    model_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(handle), trust_env=False,
                    )
                    with serve_server_a(state=state, certificate_directory=root) as port:
                        api = _api(port, root)
                        generation = _generation(
                            lane.offering.analysis_kind_ref,
                            lane.offering.implementation_ref,
                        )
                        with AttemptJournalStore(journal_root, maximum_records=1) as journal:
                            admitted = admit_next_job(
                                lane=lane, api=api, journal=journal,
                                generation=generation,
                                frozen_generation_id=_FROZEN_GENERATION_ID,
                            )
                            self.assertIs(type(admitted), JobAdmitted, repr(admitted))
                            started = start_attempt(
                                lane=lane, api=api, journal=journal,
                                generation=generation,
                                frozen_generation_id=_FROZEN_GENERATION_ID,
                                record=admitted.record,
                            )
                            self.assertIs(type(started), StartContinues, repr(started))
                            with patch(
                                "nmrpeak_provider.input_interpreter.httpx.AsyncClient",
                                return_value=model_client,
                            ), self.assertLogs(level="INFO") as captured_logs:
                                prepared = prepare_execution(
                                    lane=lane, api=api, journal=journal,
                                    session=_NoRunnerValidation(),
                                    interpreter=interpreter,
                                    record=started.record,
                                    canonical_input=admitted.canonical_input,
                                )
                            self.assertIs(type(prepared), InputFailurePending, repr(prepared))
                            self.assertEqual(len(requests), 1)
                            self.assertIn(marker, requests[0]["messages"][2]["content"])
                            self.assertEqual(
                                interpreter.exception_text[0], "reported_input_problem"
                            )
                            self.assertNotIn(marker, repr(interpreter.exception_text))
                            self.assertNotIn(marker, "\n".join(captured_logs.output))
                            retained = prepared.record
                            command = json.loads(retained.terminal_request_body)
                            self.assertEqual(command["failure_code"], "interpretation_failed")
                            self.assertEqual(command["failure_message"], expected_message)
                            self.assertEqual(retained.latest_diagnostic.kind, "model_reported_problem")
                            self.assertEqual(retained.latest_diagnostic.producer, "interpreter")
                            self.assertEqual(retained.latest_diagnostic.reason, "model_report")
                            self.assertEqual(retained.latest_diagnostic.endpoint_route, ("primary",))
                            self.assertNotIn(marker.encode("utf-8"), journal.record_bytes(retained))
                            state.lose_next_failure_response()
                            uncertain = deliver_terminal(
                                api=api, journal=journal, record=retained,
                            )
                            self.assertIs(type(uncertain), AttemptMutationCommitPossible)
                            self.assertEqual(journal.records(), (retained,))

                        inspection_bytes = inspect_journal(journal_root)
                        self.assertNotIn(marker.encode("utf-8"), inspection_bytes)
                        inspected, = json.loads(inspection_bytes)["records"]
                        self.assertEqual(inspected["delivery"], "unconfirmed")
                        self.assertEqual(inspected["retained_failure"], {
                            "failure_code": "interpretation_failed",
                            "failure_message": expected_message,
                        })
                        runtime = _generation_runtime(chf=_generation(
                            CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                            CHF_LIFECYCLE_LANE.offering.implementation_ref,
                        ))
                        with AttemptJournalStore(journal_root, maximum_records=1) as reopened:
                            retained_after_restart, = reopened.records()
                            self.assertEqual(
                                retained_after_restart.terminal_request_body,
                                retained.terminal_request_body,
                            )
                            recovered = reconcile_record(
                                runtime=runtime, api=api, journal=reopened,
                                record=retained_after_restart,
                            )
                            self.assertIs(type(recovered), TerminalDelivered, repr(recovered))
                            self.assertTrue(recovered.receipt.replayed)
                            self.assertEqual(reopened.records(), ())
                self.assertTrue(all(response.is_closed for response in responses))
                self.assertEqual(state.failures, [])
                self.assertIsNotNone(state.attempt)
                self.assertEqual(state.attempt.state, "failed")
                self.assertEqual(state.attempt.terminal_receipt["failure_message"], expected_message)
                self.assertNotIn(marker.encode("utf-8"), state.attempt.terminal_body)
                fail_bodies = [
                    body for method, target, body in state.requests
                    if method == "POST" and target == "/provider/v1/execution-attempts/fail"
                ]
                self.assertEqual(fail_bodies, [retained.terminal_request_body] * 2)

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
                        interpreter=_UNUSED_INTERPRETER,
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
    session, _channel = _runner_session_with_channel(codec, facts)
    return session


def _runner_session_with_channel(
    codec: RunnerFrameCodec,
    facts: ProviderResultFacts,
) -> tuple[RunnerSession, FakeRunnerChannel]:
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
    channel = FakeRunnerChannel(codec, ready)
    session = RunnerSession.admit(
        channel,
        facts,
        RunnerDeadlines(0.2, 0.2, 0.2, 0.2, 0.2),
        codec,
    )
    return session, channel


def _test_interpreter(root: Path) -> InputInterpreter:
    endpoint_root = root / "interpreter"
    endpoint_root.mkdir(mode=0o700)
    (endpoint_root / "05-primary.toml").write_text(
        'id = "primary"\nbase_url = "https://primary.example.test/v1"\n'
        'api_key = "fixture-only"\nmodel = "fixture-model"\n',
        encoding="utf-8",
    )
    return InputInterpreter(
        endpoint_specs=load_openai_chat_endpoint_specs(endpoint_root),
        policy=InterpreterPolicy(
            call_policy=OpenAIChatCallPolicy(
                request_timeout_seconds=1,
                turn_timeout_seconds=3,
            ),
            interpretation_timeout_seconds=10,
        ),
    )


def _candidate_completion(value: object) -> dict[str, object]:
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "reasoning_content": "Private fixture reasoning.",
        "tool_calls": [{"id": "fixture-call", "type": "function", "function": {
            "name": "submit_interpretation",
            "arguments": json.dumps({"value": value}),
        }}],
    }}]}


def _report_completion(message: str) -> dict[str, object]:
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "reasoning_content": "Private fixture reasoning.",
        "tool_calls": [{"id": "fixture-call", "type": "function", "function": {
            "name": "report_input_problem",
            "arguments": json.dumps({"message": message}),
        }}],
    }}]}


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
