"""Prove the fixed two-lane process recovers before concurrent admission."""

from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from threading import (
    Barrier,
    BrokenBarrierError,
    Event,
    Lock,
    Thread,
    enumerate as threads,
)
import unittest
from unittest.mock import patch

from nmrpeak_provider.attempt_identity import derive_provider_attempt_key
from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LocalExecutionPhase,
    StartPending,
    TerminalPending,
    retain_terminal_command,
)
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.attempt_lifecycle import ObservationPolicy
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
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
    RESULT_SCHEMA_ID,
)
from nmrpeak_provider.provider_https import (
    ProviderHttpResponse,
    ProviderOperation,
    ProviderRequestUnavailable,
    RequestDelivery,
)
from nmrpeak_provider.provider_process import (
    ProviderLaneFailed,
    ProviderLaneUnavailable,
    ProviderProcessPolicy,
    ProviderProtocolFailed,
    ProviderShutdownFailed,
    run_provider_process,
)
from nmrpeak_provider.provider_requests import (
    HelloOffering,
    prepare_execution_attempt_complete,
    prepare_provider_hello,
)
from nmrpeak_provider.run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    run_generation_fingerprint,
)
from nmrpeak_provider.runner_protocol import ReadyFrame
from nmrpeak_provider.runner_session import RunnerDeadlines, RunnerSession
from tests.fakes.runner import FakeRunnerChannel


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


class ConcurrentProviderApi:
    def __init__(
        self,
        *,
        stop: Event,
        terminal: TerminalPending | None = None,
        failing_analysis: str | None = None,
        fatal_analysis: str | None = None,
        unavailable_analysis: str | None = None,
        unavailable_input_analysis: str | None = None,
        unavailable_feed_failures: int | None = None,
    ) -> None:
        self.stop = stop
        self.terminal = terminal
        self.failing_analysis = failing_analysis
        self.fatal_analysis = fatal_analysis
        self.unavailable_analysis = unavailable_analysis
        self.unavailable_input_analysis = unavailable_input_analysis
        self.unavailable_feed_failures = unavailable_feed_failures
        self.feed_barrier = Barrier(2)
        self.requests: list[object] = []
        self._unavailable_feed_reads = 0
        self._lock = Lock()

    def send(self, request: object) -> object:
        with self._lock:
            self.requests.append(request)
        if request.operation is ProviderOperation.PROVIDER_HELLO:
            return hello_response()
        if request.operation is ProviderOperation.EXECUTION_ATTEMPTS_LIST:
            attempts = () if self.terminal is None else (self.terminal,)
            return inventory_response(attempts)
        if request.operation is ProviderOperation.EXECUTION_ATTEMPT_READ:
            assert self.terminal is not None
            return response(
                {
                    "schema_id": "nmr.provider.execution_attempt_read_response.v1",
                    "execution_attempt_ref": self.terminal.execution_attempt_ref,
                    "job_ref": self.terminal.job_ref,
                    "state": "in_progress",
                    "job_state": "open",
                }
            )
        if request.operation is ProviderOperation.EXECUTION_ATTEMPT_COMPLETE:
            return completion_receipt(request)
        if request.operation is ProviderOperation.JOBS_LIST:
            analysis_kind = query_value(request.query, "analysis_kind_ref")
            with self._lock:
                if analysis_kind == self.unavailable_analysis:
                    self._unavailable_feed_reads += 1
                unavailable_feed_reads = self._unavailable_feed_reads
            try:
                self.feed_barrier.wait(timeout=1)
            except BrokenBarrierError as error:
                raise AssertionError("HF and CHF feed reads were not concurrent") from error
            if analysis_kind == self.failing_analysis:
                raise RuntimeError("lane failure")
            if analysis_kind == self.fatal_analysis:
                return ProviderHttpResponse(
                    200,
                    "dev-local",
                    "application/json",
                    None,
                    b"{",
                )
            if analysis_kind == self.unavailable_analysis:
                if (
                    self.unavailable_feed_failures is None
                    or unavailable_feed_reads <= self.unavailable_feed_failures
                ):
                    return ProviderRequestUnavailable(
                        RequestDelivery.RESPONSE_RECEIVED,
                        status=502,
                    )
                self.stop.set()
            if (
                self.failing_analysis is None
                and self.fatal_analysis is None
                and self.unavailable_analysis is None
                and self.unavailable_input_analysis is None
            ):
                self.stop.set()
            if analysis_kind == self.unavailable_input_analysis:
                return jobs_response(analysis_kind, job_item(analysis_kind))
            return jobs_response(analysis_kind)
        if request.operation is ProviderOperation.JOB_INPUT_READ:
            analysis_kind = query_value(request.query, "analysis_kind_ref")
            if analysis_kind != self.unavailable_input_analysis:
                raise AssertionError("unexpected Job input read")
            return ProviderRequestUnavailable(
                RequestDelivery.RESPONSE_RECEIVED,
                status=502,
            )
        raise AssertionError(f"unexpected provider operation {request.operation}")


class BlockingFeedApi:
    def __init__(self, stop: Event, release: Event) -> None:
        self.stop = stop
        self.release = release
        self.feed_barrier = Barrier(2)

    def send(self, request: object) -> object:
        if request.operation is ProviderOperation.PROVIDER_HELLO:
            return hello_response()
        if request.operation is ProviderOperation.EXECUTION_ATTEMPTS_LIST:
            return inventory_response(())
        if request.operation is ProviderOperation.JOBS_LIST:
            self.feed_barrier.wait(timeout=1)
            self.stop.set()
            self.release.wait()
            return jobs_response(query_value(request.query, "analysis_kind_ref"))
        raise AssertionError(f"unexpected provider operation {request.operation}")


class PeriodicHelloApi:
    def __init__(self, stop: Event, *, fatal_second_hello: bool) -> None:
        self.stop = stop
        self.fatal_second_hello = fatal_second_hello
        self.hello_count = 0

    def send(self, request: object) -> object:
        if request.operation is ProviderOperation.EXECUTION_ATTEMPTS_LIST:
            return inventory_response(())
        if request.operation is ProviderOperation.PROVIDER_HELLO:
            self.hello_count += 1
            if self.hello_count == 1 and not self.fatal_second_hello:
                return ProviderRequestUnavailable(RequestDelivery.NOT_SENT)
            if self.hello_count == 2:
                if self.fatal_second_hello:
                    return response({"schema_id": "wrong"})
                self.stop.set()
            return hello_response()
        if request.operation is ProviderOperation.JOBS_LIST:
            return jobs_response(query_value(request.query, "analysis_kind_ref"))
        raise AssertionError(f"unexpected provider operation {request.operation}")


class ProviderProcessTests(unittest.TestCase):
    _unused_interpreter = object()

    def test_recovers_retained_terminal_before_concurrent_lane_feeds(self) -> None:
        runtime = generation_runtime()
        terminal = completion_pending(runtime.chf.generation)
        stop = Event()
        api = ConcurrentProviderApi(stop=stop, terminal=terminal)
        readiness_operations: list[ProviderOperation] = []

        def publish_readiness() -> None:
            readiness_operations.extend(request.operation for request in api.requests)

        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                persist_terminal(journal, terminal)
                run_provider_process(
                    runtime=runtime,
                    api=api,
                    journal=journal,
                    interpreter=self._unused_interpreter,
                    hf_session=hf_session,
                    chf_session=chf_session,
                    hello=hello_request(),
                    policy=process_policy(),
                    stop=stop,
                    on_ready=publish_readiness,
                )
                self.assertEqual(journal.records(), ())

        operations = [request.operation for request in api.requests]
        first_feed = operations.index(ProviderOperation.JOBS_LIST)
        self.assertEqual(
            operations[:first_feed],
            [
                ProviderOperation.EXECUTION_ATTEMPTS_LIST,
                ProviderOperation.EXECUTION_ATTEMPT_READ,
                ProviderOperation.EXECUTION_ATTEMPT_COMPLETE,
                ProviderOperation.PROVIDER_HELLO,
            ],
        )
        self.assertEqual(
            readiness_operations[:first_feed],
            operations[:first_feed],
        )
        self.assertEqual(
            operations.count(ProviderOperation.JOBS_LIST),
            2,
        )
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

    def test_one_lane_failure_stops_and_joins_its_sibling(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = ConcurrentProviderApi(
            stop=stop,
            failing_analysis=HF_LIFECYCLE_LANE.offering.analysis_kind_ref,
        )
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                with self.assertRaisesRegex(
                    ProviderLaneFailed,
                    "hf provider lane stopped unexpectedly",
                ) as raised:
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=hf_session,
                        chf_session=chf_session,
                        hello=hello_request(),
                        policy=process_policy(),
                        stop=stop,
                        on_ready=lambda: None,
                    )

        self.assertIs(type(raised.exception.__cause__), RuntimeError)
        self.assertTrue(stop.is_set())
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

    def test_preexisting_stop_retires_both_sessions_without_api_work(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        stop.set()
        api = ConcurrentProviderApi(stop=stop)
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                run_provider_process(
                    runtime=runtime,
                    api=api,
                    journal=journal,
                    interpreter=self._unused_interpreter,
                    hf_session=hf_session,
                    chf_session=chf_session,
                    hello=hello_request(),
                    policy=process_policy(),
                    stop=stop,
                    on_ready=lambda: None,
                )

        self.assertEqual(api.requests, [])
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

    def test_partial_thread_start_stops_and_joins_the_started_lane(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = ConcurrentProviderApi(stop=stop)
        api.feed_barrier.abort()
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)
        original_start = Thread.start
        starts = 0
        readiness_published = False

        def publish_readiness() -> None:
            nonlocal readiness_published
            readiness_published = True

        def fail_second_start(thread: Thread) -> None:
            nonlocal starts
            starts += 1
            if starts == 2:
                raise RuntimeError("thread start failed")
            original_start(thread)

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                with (
                    patch(
                        "nmrpeak_provider.provider_process.Thread.start",
                        new=fail_second_start,
                    ),
                    self.assertRaisesRegex(RuntimeError, "thread start failed"),
                ):
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=hf_session,
                        chf_session=chf_session,
                        hello=hello_request(),
                        policy=process_policy(),
                        stop=stop,
                        on_ready=publish_readiness,
                    )

        self.assertTrue(stop.is_set())
        self.assertFalse(readiness_published)
        self.assertFalse(
            any(thread.name.startswith("nmrpeak-") for thread in threads())
        )
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

    def test_readiness_publication_failure_stops_and_joins_both_lanes(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = ConcurrentProviderApi(stop=stop)
        api.feed_barrier.abort()
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)

        def fail_readiness() -> None:
            raise RuntimeError("readiness failed")

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                with self.assertRaisesRegex(RuntimeError, "readiness failed"):
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=hf_session,
                        chf_session=chf_session,
                        hello=hello_request(),
                        policy=process_policy(),
                        stop=stop,
                        on_ready=fail_readiness,
                    )

        self.assertTrue(stop.is_set())
        self.assertFalse(
            any(thread.name.startswith("nmrpeak-") for thread in threads())
        )
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

    def test_session_drift_and_malformed_response_fail_before_more_work(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = ConcurrentProviderApi(stop=stop)
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                with self.assertRaisesRegex(ValueError, "another lane generation"):
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=chf_session,
                        chf_session=hf_session,
                        hello=hello_request(),
                        policy=process_policy(),
                        stop=stop,
                        on_ready=lambda: None,
                    )
        self.assertEqual(api.requests, [])
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

        stop = Event()
        api = ConcurrentProviderApi(
            stop=stop,
            fatal_analysis=HF_LIFECYCLE_LANE.offering.analysis_kind_ref,
        )
        hf_session, _ = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, _ = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                with self.assertRaises(ProviderLaneFailed) as raised:
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=hf_session,
                        chf_session=chf_session,
                        hello=hello_request(),
                        policy=process_policy(),
                        stop=stop,
                        on_ready=lambda: None,
                    )
        cause = raised.exception.__cause__
        self.assertIs(type(cause), ProviderProtocolFailed)
        self.assertIn("Cannot read the Job feed", str(cause))
        self.assertIn(
            "HTTP 200 success response failed validation (invalid_json)",
            str(cause),
        )
        self.assertIsInstance(cause.__cause__, ValueError)
        self.assertNotIn("authentication or contract evidence", str(cause))

    def test_job_feed_retries_past_the_operation_unavailability_budget(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = ConcurrentProviderApi(
            stop=stop,
            unavailable_analysis=HF_LIFECYCLE_LANE.offering.analysis_kind_ref,
            unavailable_feed_failures=3,
        )
        hf_session, _ = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, _ = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)
        with self.assertLogs(
            "nmrpeak_provider.provider_process",
            level="WARNING",
        ) as captured:
            with journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=2) as journal:
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=hf_session,
                        chf_session=chf_session,
                        hello=hello_request(),
                        policy=process_policy(),
                        stop=stop,
                        on_ready=lambda: None,
                    )

        hf_feeds = [
            request
            for request in api.requests
            if request.operation is ProviderOperation.JOBS_LIST
            and query_value(request.query, "analysis_kind_ref")
            == HF_LIFECYCLE_LANE.offering.analysis_kind_ref
        ]
        self.assertEqual(len(hf_feeds), 4)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("HTTP 502", captured.output[0])

    def test_required_job_work_stops_at_the_unavailability_budget(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = ConcurrentProviderApi(
            stop=stop,
            unavailable_input_analysis=(
                HF_LIFECYCLE_LANE.offering.analysis_kind_ref
            ),
        )
        hf_session, _ = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, _ = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)
        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                with self.assertRaises(ProviderLaneFailed) as raised:
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=hf_session,
                        chf_session=chf_session,
                        hello=hello_request(),
                        policy=process_policy(),
                        stop=stop,
                        on_ready=lambda: None,
                    )

        failure = raised.exception.__cause__
        self.assertIs(type(failure), ProviderLaneUnavailable)
        self.assertIn("2 consecutive unavailable operations", str(failure))
        self.assertIn("HTTP 502", str(failure))
        self.assertEqual(
            sum(
                request.operation is ProviderOperation.JOB_INPUT_READ
                for request in api.requests
            ),
            2,
        )

    def test_periodic_hello_retries_unavailability_without_gating_lanes(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = PeriodicHelloApi(stop, fatal_second_hello=False)
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                run_provider_process(
                    runtime=runtime,
                    api=api,
                    journal=journal,
                    interpreter=self._unused_interpreter,
                    hf_session=hf_session,
                    chf_session=chf_session,
                    hello=hello_request(),
                    policy=process_policy(hello_interval_seconds=0.005),
                    stop=stop,
                    on_ready=lambda: None,
                )

        self.assertEqual(api.hello_count, 2)
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

    def test_periodic_fatal_hello_stops_and_joins_both_lanes(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        api = PeriodicHelloApi(stop, fatal_second_hello=True)
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)

        with journal_directory() as root:
            with AttemptJournalStore(root, maximum_records=2) as journal:
                with self.assertRaisesRegex(
                    ProviderProtocolFailed,
                    "publish provider capabilities.*invalid_shape",
                ):
                    run_provider_process(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=self._unused_interpreter,
                        hf_session=hf_session,
                        chf_session=chf_session,
                        hello=hello_request(),
                        policy=process_policy(hello_interval_seconds=0.005),
                        stop=stop,
                        on_ready=lambda: None,
                    )

        self.assertEqual(api.hello_count, 2)
        self.assertTrue(stop.is_set())
        self.assertFalse(
            any(thread.name.startswith("nmrpeak-") for thread in threads())
        )
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)

    def test_forced_shutdown_cancels_sessions_and_reports_live_lanes(self) -> None:
        runtime = generation_runtime()
        stop = Event()
        release = Event()
        api = BlockingFeedApi(stop, release)
        hf_session, hf_channel = runner_session(HF_FACTS, HF_RUNNER_CODEC)
        chf_session, chf_channel = runner_session(CHF_FACTS, CHF_RUNNER_CODEC)
        policy = ProviderProcessPolicy(
            feed_interval_seconds=0.001,
            hello_interval_seconds=0.1,
            shutdown_drain_seconds=0.005,
            forced_join_seconds=0.005,
            inventory_maximum_pages=1,
            maximum_consecutive_unavailable=2,
            observation=ObservationPolicy(0.001, 0.1),
        )
        try:
            with journal_directory() as root:
                with AttemptJournalStore(root, maximum_records=2) as journal:
                    with self.assertRaises(ProviderShutdownFailed):
                        run_provider_process(
                            runtime=runtime,
                            api=api,
                            journal=journal,
                            interpreter=self._unused_interpreter,
                            hf_session=hf_session,
                            chf_session=chf_session,
                            hello=hello_request(),
                            policy=policy,
                            stop=stop,
                            on_ready=lambda: None,
                        )
        finally:
            release.set()
            deadline = time.monotonic() + 1
            while (
                any(thread.name.startswith("nmrpeak-") for thread in threads())
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)

        self.assertFalse(
            any(thread.name.startswith("nmrpeak-") for thread in threads())
        )
        self.assertTrue(hf_channel.closed)
        self.assertTrue(chf_channel.closed)


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


def runner_session(facts, codec) -> tuple[RunnerSession, FakeRunnerChannel]:
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
    return (
        RunnerSession.admit(
            channel,
            facts,
            RunnerDeadlines(0.1, 0.1, 0.1, 0.1, 0.1),
            codec,
        ),
        channel,
    )


def completion_pending(generation: RunGenerationIdentity) -> TerminalPending:
    input_fingerprint = "sha256:" + "6" * 64
    active = ActiveAttempt(
        job_ref="job:retained",
        provider_attempt_key=derive_provider_attempt_key(
            provider_ref=generation.provider_ref,
            run_generation_fingerprint=run_generation_fingerprint(generation),
            job_ref="job:retained",
            input_fingerprint=input_fingerprint,
        ),
        input_fingerprint=input_fingerprint,
        frozen_generation_id=FROZEN_GENERATION_ID,
        execution_attempt_ref="execution_attempt:sha256:" + "7" * 64,
        local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
    )
    return retain_terminal_command(
        active,
        prepare_execution_attempt_complete(
            execution_attempt_ref=active.execution_attempt_ref,
            result_schema_id=RESULT_SCHEMA_ID,
            canonical_result=b'{"candidates":[{"generated_smiles":"CCO"}]}',
        ),
    )


def persist_terminal(journal: AttemptJournalStore, terminal: TerminalPending) -> None:
    pending = StartPending(
        job_ref=terminal.job_ref,
        provider_attempt_key=terminal.provider_attempt_key,
        input_fingerprint=terminal.input_fingerprint,
        frozen_generation_id=terminal.frozen_generation_id,
    )
    active = ActiveAttempt(
        job_ref=terminal.job_ref,
        provider_attempt_key=terminal.provider_attempt_key,
        input_fingerprint=terminal.input_fingerprint,
        frozen_generation_id=terminal.frozen_generation_id,
        execution_attempt_ref=terminal.execution_attempt_ref,
        local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
    )
    journal.admit(pending)
    journal.replace(pending, active)
    journal.replace(active, terminal)


def inventory_response(records: tuple[TerminalPending, ...]) -> ProviderHttpResponse:
    return response(
        {
            "schema_id": "nmr.provider.execution_attempts.list.response.v1",
            "attempts": [
                {
                    "analysis_kind_ref": CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                    "execution_attempt_ref": record.execution_attempt_ref,
                    "job_ref": record.job_ref,
                    "provider_attempt_key": record.provider_attempt_key,
                    "state": "in_progress",
                    "started_at": "2026-08-24T12:00:00Z",
                }
                for record in records
            ],
            "next_cursor": None,
        }
    )


def completion_receipt(request: object) -> ProviderHttpResponse:
    command = json.loads(request.body)
    result = b64decode(command["canonical_result_base64"], validate=True)
    return response(
        {
            "schema_id": "nmr.provider.execution_attempt_complete_response.v1",
            "execution_attempt_ref": command["execution_attempt_ref"],
            "analysis_result_ref": "analysis_result:sha256:" + "8" * 64,
            "result_schema_id": command["result_schema_id"],
            "result_fingerprint": "sha256:" + sha256(result).hexdigest(),
            "result_byte_length": len(result),
            "committed_at": "2026-08-24T12:02:00Z",
            "replayed": True,
        }
    )


def job_item(analysis_kind_ref: str) -> dict[str, object]:
    return {
        "job_ref": "job:selected",
        "analysis_kind_ref": analysis_kind_ref,
        "input_fingerprint": "sha256:" + "6" * 64,
        "input_schema_id": "nmr.job.specification.text.v1",
        "input_byte_length": 2,
        "created_at": "2026-08-24T12:00:00Z",
    }


def jobs_response(
    analysis_kind_ref: str,
    *jobs: dict[str, object],
) -> ProviderHttpResponse:
    return response(
        {
            "schema_id": "nmr.provider.jobs.list.response.v1",
            "analysis_kind_ref": analysis_kind_ref,
            "has_provider_execution_attempt": False,
            "jobs": list(jobs),
            "next_cursor": None,
        }
    )


def hello_response() -> ProviderHttpResponse:
    return response(
        {
            "schema_id": "nmr.provider.hello_response.v1",
            "provider_ref": "provider:nmrpeak",
            "accepted_at": "2026-08-24T12:00:00Z",
        }
    )


def hello_request():
    return prepare_provider_hello(
        display_name="NMRPeak",
        description="NMRPeak structure generation from structured NMR input.",
        analysis_offerings=(
            HelloOffering(
                HF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                "Requires structured molecular formula and 1H NMR input.",
            ),
            HelloOffering(
                CHF_LIFECYCLE_LANE.offering.analysis_kind_ref,
                "Requires structured molecular formula, 1H NMR, and 13C NMR input.",
            ),
        ),
    )


def response(document: dict[str, object]) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        200,
        "dev-local",
        "application/json",
        None,
        json.dumps(document, separators=(",", ":")).encode(),
    )


def query_value(query: str, name: str) -> str:
    for field in query.split("&"):
        key, value = field.split("=", 1)
        if key == name:
            return value
    raise AssertionError(f"missing query field {name}")


def process_policy(
    *,
    hello_interval_seconds: float = 0.1,
) -> ProviderProcessPolicy:
    return ProviderProcessPolicy(
        feed_interval_seconds=0.001,
        hello_interval_seconds=hello_interval_seconds,
        shutdown_drain_seconds=0.2,
        forced_join_seconds=0.2,
        inventory_maximum_pages=1,
        maximum_consecutive_unavailable=2,
        observation=ObservationPolicy(0.001, 0.1),
    )


class journal_directory:
    def __init__(self) -> None:
        self.temporary = TemporaryDirectory()

    def __enter__(self) -> Path:
        root = Path(self.temporary.name) / "journal"
        root.mkdir(mode=0o700)
        return root

    def __exit__(self, *exc_info: object) -> None:
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
