"""Run both real NMR adapters through SDK controllers and durable shared IPC."""

import asyncio
import json
from base64 import b64decode
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Barrier, Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryUnavailable
from provider_core.api import ApiFailure
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.api_config import api_settings_with_material
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_delivery_ipc import SharedDeliveryInbox, SharedDeliverySocket
from provider_core.delivery_store import DeliveryFull, DeliveryOutcome, DeliveryQueue, QueueLimits
from provider_core.analysis import AnalysisOutcomeUnresolved, ObservedWorkerCrash, PreparedAnalysis
from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import fail_body, start_body
from provider_core.controller import ControllerFailed
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionRefused, ExecutionStopRequested, ObservationGapExpired
from provider_core.hello import AnalysisOffering
from provider_core.producer_checkpoint import SuppressedCheckpoint
from provider_core.ownership import AttemptClaims, LiveAttemptLimits

from nmrpeak_provider.analysis_session import NmrpeakAnalysisSession
from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.interpreter import InterpreterTool, InterpreterUnavailable, InterpreterUnavailableReason
from nmrpeak_provider.interpreter_policy import InterpreterPolicy
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec
from provider_core.interpreter import Interpreter
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.generation_runtime import GenerationRuntime
from nmrpeak_provider.product_result import canonical_result_bytes
from nmrpeak_provider.provider_config import ObservationPolicy, ProviderProcessPolicy
from nmrpeak_provider.provider_process import ProviderAnnouncement, ProviderProcess
from nmrpeak_provider.runner_protocol import GenerateFrame, ValidateFrame
from nmrpeak_provider.runner_session import open_runner_session, RunnerDeadlines
from tests.fakes.model_http import LoopbackHTTP, Reply, reply
from tests.integration.test_native_runner_session import native_runner
from tests.fakes.shared_api import ApiTransport, SharedApi
from tests.fakes.native_runner import lane_contract
from provider_core.network import NetworkIOObservation
from provider_core.transport import ExchangeFailed
from tests.fakes.runner import FakeRunnerFault
from tests.unit.test_analysis_session import scientific_lane
from tests.fakes.interpreter_values import HF_VALUE, VALUE
from tests.unit.test_analysis_session import interpreter
from tests.unit.test_provider_startup_inputs import credential_bytes


class SharedProviderProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_sink_failure_does_not_block_next_selected_job(self):
        service = await self._idle_process()
        lane = service.lanes[0]
        diagnostic = RuntimeError("private diagnostic failure")
        first = SimpleNamespace(diagnostic_failure=diagnostic)
        second = SimpleNamespace(diagnostic_failure=None)
        runs = 0

        async def run(_pending):
            nonlocal runs
            runs += 1
            if runs == 2:
                service._stop.set()
            return None

        controller = SimpleNamespace(run=run)
        with patch("nmrpeak_provider.provider_process.NmrpeakAnalysisSession", side_effect=[first, second]), \
             patch.object(service, "_select", AsyncMock(return_value=(object(), object()))), \
             patch.object(service, "_controller", return_value=controller), \
             patch("nmrpeak_provider.provider_process._LOG.warning", side_effect=RuntimeError("logging unavailable")):
            await service._lane_loop(lane)
        self.assertEqual(runs, 2)
        self.assertIsInstance(service.diagnostic_failure, RuntimeError)

    @asynccontextmanager
    async def _interpreter_executor_fixture(self, *, script, runner=None):
        # Reuse the maintained service/controller composition rather than model
        # executor ownership in a second fixture.
        service = await self._idle_process(with_job=True)
        lane = service.lanes[0]
        if runner is not None:
            self.assertEqual(runner.result_facts, lane.generation.result_facts)
            lane.runner = runner
        service.policy = replace(service.policy, forced_join_seconds=0.05)
        backend = service.api._transport.api
        backend.inputs["job:hf"] = (lane.generation.generation.analysis_kind_ref,
                                    b"Interpret these proton peaks.")
        server = await LoopbackHTTP(script).start()
        configured = InputInterpreter((OpenAIChatEndpointSpec(
            configuration_id="fixture", url=server.base_url + "/chat/completions",
            api_key="fixture-key", model="fixture-model", trust=server.trust,
        ),), InterpreterPolicy(interpretation_timeout_seconds=20))
        service.interpreter = configured

        endpoint = service.delivery.endpoint
        state = endpoint.socket_path.parent / "interpreter-state"
        state.mkdir(mode=0o700)
        queue = DeliveryQueue(state, limits=QueueLimits(4, 1048576, 4, 1048576))
        accepted = asyncio.Event()
        inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2,
                              on_accepted=lambda _endpoint: accepted.set(), on_failure=Mock(),
                              binding_status=lambda _endpoint: {"state": "inbox_only"})
        await inbox.start()
        pending, instructions = await service._select(lane)
        session = NmrpeakAnalysisSession(lane.generation, lane.runner, configured)
        controller = service._controller(lane, session, pending, instructions)
        lane.controller = controller
        try:
            yield service, controller, pending, session, server, queue, accepted
        finally:
            # Each witness releases its held operation before leaving this scope.
            await service._drain()
            await inbox.close()
            queue.close()
            await server.close()

    async def test_executor_retires_inflight_interpreter_http(self):
        release, endpoint_retired = asyncio.Event(), asyncio.Event()
        original_prepare_tasks = []
        prepare = NmrpeakAnalysisSession.prepare
        retire = Interpreter.retire

        async def watched_prepare(session, *args, **kwargs):
            original_prepare_tasks.append(asyncio.current_task())
            return await prepare(session, *args, **kwargs)

        async def watched_retire(owner):
            await retire(owner)
            endpoint_retired.set()

        async with self._interpreter_executor_fixture(
                script=[Reply(wait_for=release)]) as (_, controller, pending, session, server, queue, _):
            with patch.object(NmrpeakAnalysisSession, "prepare", watched_prepare), \
                 patch.object(Interpreter, "retire", watched_retire):
                original = asyncio.create_task(controller.run(pending))
                try:
                    await asyncio.wait_for(server.request_seen.wait(), 2)
                    controller.request_stop()
                    await asyncio.wait_for(endpoint_retired.wait(), 2)
                    with self.assertRaises(ControllerFailed):
                        await asyncio.wait_for(asyncio.shield(original), 2)
                    executor = controller.operation
                    self.assertIs(type(executor), AttemptExecutor)
                    self.assertTrue(executor.retirement_confirmed)
                    self.assertTrue(session._interpretation._shared.retirement_confirmed)
                    self.assertEqual(len(original_prepare_tasks), 1)
                    prepare_task = original_prepare_tasks[0]
                    self.assertEqual(len(server.requests), 1)

                    release.set()
                    done, _ = await asyncio.wait((prepare_task,), timeout=2)
                    self.assertIn(prepare_task, done)
                    self.assertTrue(executor.work_settled)
                    self.assertTrue(executor.shutdown_ready)
                    self.assertEqual(len(server.requests), 1)
                    self.assertFalse(queue.inventory(controller.scope.api_origin,
                                                     controller.scope.provider_ref))
                finally:
                    release.set()
                    controller.request_stop()
                    await asyncio.gather(original, return_exceptions=True)

    async def test_observed_validation_crash_handoff_precedes_held_interpreter_cleanup(self):
        cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
        captured_crashes = []
        retire = Interpreter.retire
        call_science = NmrpeakAnalysisSession._call_science

        async def held_retire(owner):
            cleanup_entered.set()
            await cleanup_release.wait()
            await retire(owner)

        async def watched_science(session, *args, **kwargs):
            try:
                return await call_science(session, *args, **kwargs)
            except ObservedWorkerCrash as crash:
                captured_crashes.append(crash)
                raise

        completion = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "fixture-validation", "type": "function", "function": {
                    "name": "submit_interpretation",
                    "arguments": json.dumps({"value": HF_VALUE}),
                },
            }],
        }}]}
        with native_runner("hf", generation_mode="validation_crash") as (socket, _, facts, codec, _):
            runner = await open_runner_session(str(socket), facts, RunnerDeadlines(5, 5, 2, 2, 20), codec)
            try:
                async with self._interpreter_executor_fixture(
                        script=[reply(completion)], runner=runner) as (
                            service, controller, pending, _, server, queue, accepted):
                    submit = controller.delivery.submit
                    submissions = 0
                    handoff_acked = asyncio.Event()
                    other_lane = service.lanes[1]
                    service.api._transport.api.inputs['job:other'] = (
                        other_lane.generation.generation.analysis_kind_ref, canonical_json_bytes(VALUE))

                    async def pressured_submit(outcome):
                        nonlocal submissions
                        submissions += 1
                        if submissions == 1:
                            raise DeliveryFull('Fixture report capacity is temporarily full')
                        acknowledgement = await submit(outcome)
                        handoff_acked.set()
                        return acknowledgement

                    with patch.object(Interpreter, "retire", held_retire), \
                         patch.object(NmrpeakAnalysisSession, "_call_science", watched_science), \
                         patch.object(controller.delivery, "submit", pressured_submit):
                        original = asyncio.create_task(controller.run(pending))
                        try:
                            await asyncio.wait_for(cleanup_entered.wait(), 3)
                            await asyncio.wait_for(accepted.wait(), 3)
                            await asyncio.wait_for(handoff_acked.wait(), 3)
                            self.assertGreaterEqual(submissions, 2)
                            self.assertTrue(service._admission_paused.is_set())
                            service._clear_delivery_pressure()
                            self.assertFalse(service._admission_paused.is_set())
                            other = await service._select(other_lane)
                            self.assertEqual(other[0].input.job_ref, 'job:other')
                            self.assertIn(controller, service._claims.live_owners())
                            executor = controller.operation
                            self.assertIs(type(executor), AttemptExecutor)
                            self.assertEqual(len(captured_crashes), 1)
                            self.assertIs(type(captured_crashes[0]), ObservedWorkerCrash)
                            self.assertIs(executor.observed_crash, captured_crashes[0])
                            self.assertIs(executor.observed_crash.runner, runner.native)
                            self.assertIs(executor.observed_crash.native_exit, runner.native.original_exit)
                            self.assertEqual(runner.native.original_exit.returncode, 17)
                            self.assertFalse(executor.retirement_confirmed)
                            self.assertFalse(executor.work_settled)
                            self.assertFalse(executor.shutdown_ready)
                            self.assertEqual(len(server.requests), 1)
                            reports = queue.inventory(controller.scope.api_origin, controller.scope.provider_ref)
                            self.assertEqual(len(reports), 1)
                            item = queue.next_pending(controller.scope.api_origin, controller.scope.provider_ref)
                            self.assertEqual(json.loads(item.body)["failure_code"], "worker_crashed")
                            # The bounded observation may fail while its exact
                            # cleanup child remains held; delivery already won.
                            with self.assertRaises(ControllerFailed):
                                await asyncio.wait_for(asyncio.shield(original), 2)
                            cleanup_release.set()
                            await controller.settle()
                            self.assertTrue(executor.work_settled)
                            self.assertTrue(executor.shutdown_ready)
                            self.assertTrue(runner.retirement_confirmed)
                            self.assertEqual(len(queue.inventory(controller.scope.api_origin,
                                                                 controller.scope.provider_ref)), 1)
                        finally:
                            cleanup_release.set()
                            await asyncio.gather(original, return_exceptions=True)
            finally:
                await runner.retire()

    async def test_api_read_recovers_on_original_attempt_before_gap(self):
        await self._api_outage(expires=False)

    async def test_api_read_beyond_gap_before_generation_preserves_attempt(self):
        await self._api_outage(expires=True)

    async def test_api_read_beyond_gap_during_generation_retires_original_runner(self):
        await self._api_outage(expires=True, inflight=True)

    async def _api_outage(self, *, expires, inflight=False):
        service = await self._idle_process(with_job=True)
        lane = service.lanes[0]
        channel = lane.runner._channel
        if inflight:
            channel._accept_generate = lambda _frame: channel.generate_received.set()
        backend = service.api._transport.api
        pending, instructions = await service._select(lane)
        service.policy = replace(service.policy, observation=ObservationPolicy(
            poll_interval_seconds=0.01, gap_seconds=0.1 if expires else 1,
            retry_max_seconds=0.02))
        session = NmrpeakAnalysisSession(lane.generation, lane.runner, service.interpreter)
        endpoint = service.delivery.endpoint
        state = endpoint.socket_path.parent / "state"
        state.mkdir(mode=0o700)
        queue = DeliveryQueue(state, limits=QueueLimits(4, 1048576, 4, 1048576))
        inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2, on_accepted=Mock(),
                              on_failure=Mock(), binding_status=lambda _endpoint: {"state": "inbox_only"})
        await inbox.start()
        original_exchange = ApiTransport.exchange
        read_refs, interruptions = [], []
        retries, recovered = Mock(), Mock()

        async def interrupted_read(transport, request, *, maximum_bytes, owner=None):
            targeted = request.method == "GET" and "/execution-attempts/" in request.url.path
            if targeted:
                read_refs.append(request.url.path.split("/")[-1])
                if expires and (not inflight or channel.generate_received.is_set()):
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError as interruption:
                        interruptions.append(interruption)
                        transport.last_failure = ExchangeFailed("Fixture read cancelled", primary=interruption)
                        raise
                if not expires and len(read_refs) == 1:
                    failure = TimeoutError("Fixture read timed out")
                    transport.last_failure = ExchangeFailed(
                        "Fixture read failed", primary=failure, network_failure=failure,
                        network_io=NetworkIOObservation(stage="read", error=failure))
                    raise transport.last_failure
            return await original_exchange(transport, request, maximum_bytes=maximum_bytes, owner=owner)

        try:
            with patch.object(ApiTransport, "exchange", interrupted_read), \
                 patch.object(service, "_observation_retry", retries), \
                 patch.object(service, "_observation_recovered", recovered):
                lane.controller = service._controller(lane, session, pending, instructions)
                if expires:
                    with self.assertRaises(ControllerFailed) as caught:
                        await asyncio.wait_for(lane.controller.run(pending), 3)
                    failure = caught.exception.cause.cause
                    self.assertIsInstance(failure, ObservationGapExpired)
                    self.assertTrue(failure.read_started)
                    self.assertEqual(len(interruptions), 1)
                    self.assertIn(lane.controller, service._claims.live_owners())
                    if inflight:
                        self.assertTrue(lane.runner.retirement_confirmed)
                    else:
                        self.assertTrue(lane.runner.quiescent)
                    self.assertTrue(lane.controller.shutdown_ready)
                    self.assertFalse(queue.inventory(backend.origin, backend.provider_ref))
                else:
                    await asyncio.wait_for(lane.controller.run(pending), 3)
                    retries.assert_called_once()
                    recovered.assert_called_once()
                    self.assertEqual(len(queue.inventory(backend.origin, backend.provider_ref)), 1)
            reference = backend.started["job:hf"]
            self.assertTrue(read_refs)
            self.assertEqual(set(read_refs), {reference})
            starts = [body for _, path, body in backend.calls if path.endswith("/start")]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0], json.loads(pending.body))
            self.assertEqual(sum(isinstance(frame, GenerateFrame) for frame in channel.received_frames),
                             0 if expires and not inflight else 1)
            self.assertFalse(any(path.endswith(("/complete", "/fail")) for _, path, _ in backend.calls))
        finally:
            await service._drain()
            await inbox.close()
            queue.close()

    async def test_retry_after_gates_discovery_for_both_lanes(self):
        service = await self._idle_process()
        failure = ApiFailure("discover", "rate limited", response=SimpleNamespace(
            status=429, headers=((b"retry-after", b"30"),)))
        empty = SimpleNamespace(jobs=(), next_cursor=None)
        with patch("nmrpeak_provider.provider_process.monotonic", side_effect=[100, 100, 101, 130]), \
             patch.object(service.api, "jobs", AsyncMock(side_effect=[failure, empty])) as jobs, \
             patch.object(service.api, "retire", AsyncMock()), \
             patch.object(service.api, "can_retry_job_discovery", return_value=True), \
             patch.object(service, "_notice"):
            self.assertIsNone(await service._select(service.lanes[0]))
            self.assertIsNone(await service._select(service.lanes[1]))
            self.assertEqual(jobs.await_count, 1)
            self.assertIsNone(await service._select(service.lanes[1]))
            self.assertEqual(jobs.await_count, 2)

    async def test_hello_respects_server_retry_after(self):
        service = await self._idle_process()
        failure = ApiFailure("hello", "rate limited", response=SimpleNamespace(
            status=429, headers=((b"retry-after", b"30"),)))
        with patch.object(service.hello_api, "publish_hello", AsyncMock(side_effect=[failure, None])), \
             patch.object(service.hello_api, "retire", AsyncMock()), \
             patch.object(service.hello_api, "can_refresh_hello", return_value=True), \
             patch.object(service, "_notice"), patch.object(service, "_pause", AsyncMock()) as pause:
            await service._announce()
            pause.assert_awaited_once_with(30)

    async def test_startup_does_not_read_outbox_inventory(self):
        service = await self._idle_process()
        ready = Mock(side_effect=service.request_stop)
        with patch.object(service.delivery, "pending", AsyncMock(side_effect=AssertionError("push-only intake"))):
            await asyncio.wait_for(service.run(on_ready=ready), 5)
        ready.assert_called_once()
        self.assertTrue(service.shutdown_ready)

    async def test_job_discovery_does_not_read_outbox_inventory(self):
        service = await self._idle_process()
        with patch.object(service.delivery, "pending", AsyncMock(side_effect=AssertionError("push-only intake"))):
            self.assertIsNone(await service._select(service.lanes[0]))
        await service.api.retire(owner=service)

    async def test_duplicate_selection_settles_without_starting_another_attempt(self):
        service = await self._idle_process(with_job=True)
        lane = service.lanes[0]
        selection = await service._select(lane)
        pending, _instructions = selection
        owner = Mock()
        claim = service._claims.claim_live(pending, lifecycle_owner=owner,
                                          maximum_terminal_bytes=131072)

        async def stop_after_retry(_seconds):
            service.request_stop()

        try:
            with patch.object(service, "_select", AsyncMock(side_effect=[selection, None])) as select, \
                 patch.object(service, "_pause", side_effect=stop_after_retry):
                await asyncio.wait_for(service._lane_loop(lane), 5)
            self.assertEqual(select.await_count, 1)
            self.assertTrue(lane.controller.shutdown_ready)
            self.assertIsNone(lane.controller.operation)
            self.assertTrue(service._claims.owns_job(pending.scope, pending.input.job_ref))
        finally:
            claim.release(owner=owner)
            await service.api.retire(owner=service)

    async def test_full_live_claims_settle_selection_without_starting_an_attempt(self):
        service = await self._idle_process(with_job=True)
        lane = service.lanes[0]
        selection = await service._select(lane)
        pending, _instructions = selection
        service._claims = AttemptClaims.live(limits=LiveAttemptLimits(1, 262144, 131072))
        occupied = replace(pending, input=replace(pending.input, job_ref="job:occupied"),
                           body=start_body("job:occupied", "nmrpeak-provider.v1:" + "a" * 64))
        owner = Mock()
        claim = service._claims.claim_live(occupied, lifecycle_owner=owner,
                                          maximum_terminal_bytes=131072)

        async def stop_after_retry(_seconds):
            service.request_stop()

        try:
            with patch.object(service, "_select", AsyncMock(side_effect=[selection, None])) as select, \
                 patch.object(service, "_pause", side_effect=stop_after_retry):
                await asyncio.wait_for(service._lane_loop(lane), 5)
            self.assertEqual(select.await_count, 1)
            self.assertTrue(lane.controller.shutdown_ready)
            self.assertIsNone(lane.controller.operation)
            self.assertFalse(service._claims.owns_job(pending.scope, pending.input.job_ref))
        finally:
            claim.release(owner=owner)
            await service.api.retire(owner=service)

    async def test_started_validation_can_finish_after_model_window(self):
        from tests.unit.test_analysis_session import admission, phase_reporter
        from tests.unit.test_shared_interpreter_adapter import Factory, reply as model_reply

        hf, runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf", validate_seconds=2))
        model = Factory("fixture", model_reply("submit_interpretation", {"value": HF_VALUE}))
        configured = InputInterpreter((SimpleNamespace(configuration_id="fixture", api_key="fixture-only"),),
                                      InterpreterPolicy(interpretation_timeout_seconds=0.1))
        session = NmrpeakAnalysisSession(hf, runner, configured)
        acknowledged, instructions = admission(hf, b"Interpret these peaks.")
        entered, release = Event(), Event()
        original_validate = channel._accept_validate

        def delayed_validation(frame):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("Fixture validation was not released")
            original_validate(frame)

        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=(model,)), \
             patch.object(channel, "_accept_validate", delayed_validation):
            task = asyncio.create_task(session.prepare(acknowledged, instructions, report_condition=phase_reporter()))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.sleep(0.2)
                self.assertFalse(task.done())
                self.assertFalse(any(type(frame) is GenerateFrame for frame in channel.received_frames))
            finally:
                release.set()
            prepared = await asyncio.wait_for(task, 2)
        self.assertIs(type(prepared), PreparedAnalysis)
        self.assertEqual(sum(type(frame) is ValidateFrame for frame in channel.received_frames), 1)
        self.assertFalse(any(type(frame) is GenerateFrame for frame in channel.received_frames))
        await session.retire()

    async def test_stop_after_controller_check_prevents_first_generation_submission(self):
        hf, hf_runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, _, _ = await self.enterAsyncContext(scientific_lane("chf"))
        api = SharedApi({"job:hf": (hf.generation.analysis_kind_ref, canonical_json_bytes(HF_VALUE))})
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "state").mkdir(mode=0o700)
        (root / "ipc").mkdir(mode=0o700)
        endpoint = DeliveryEndpoint(api.origin, api.provider_ref, root / "ipc/report.sock")
        queue = DeliveryQueue(root / "state", limits=QueueLimits(4, 1048576, 4, 1048576))
        inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2, on_accepted=Mock(),
                              on_failure=Mock(), binding_status=lambda _endpoint: {"state": "inbox_only"})
        await inbox.start()
        service = ProviderProcess(
            runtime=GenerationRuntime("sha256:" + "1" * 64, hf, chf),
            hf_session=hf_runner, chf_session=chf_runner, interpreter=interpreter(),
            new_api=api.new_api,
            new_delivery=lambda: DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=0.2),
            policy=ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30)),
            announcement=ProviderAnnouncement("NMRPeak", "Stop fixture", tuple(
                AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering") for lane in (hf, chf))),
        )
        original_execute = NmrpeakAnalysisSession.execute
        entered = asyncio.Event()

        async def stop_before_submission(session, prepared, *, report_condition):
            entered.set()
            service._scientific_stopping.set()
            return await original_execute(session, prepared, report_condition=report_condition)

        try:
            with patch.object(NmrpeakAnalysisSession, "execute", stop_before_submission):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await asyncio.wait_for(service.run(on_ready=Mock()), 10)
            self.assertTrue(entered.is_set())
            controller = service.lanes[0].controller
            self.assertIsNotNone(caught.exception.subgroup(lambda error: error is controller.last_failure))
            self.assertIsInstance(controller.operation.last_failure.cause, ExecutionStopRequested)
            self.assertTrue(any(type(frame) is ValidateFrame for frame in channel.received_frames))
            self.assertFalse(any(type(frame) is GenerateFrame for frame in channel.received_frames))
            self.assertFalse(queue.inventory(api.origin, api.provider_ref))
            self.assertFalse(any(path.endswith(("/complete", "/fail")) for _, path, _ in api.calls))
        finally:
            await inbox.close()
            queue.close()

    async def test_stop_after_result_records_suppression_and_finishes_shutdown(self):
        await self._stop_after_result(refuse_suppression=False)

    async def test_refused_suppression_retains_the_claim_and_stops_retrying(self):
        await self._stop_after_result(refuse_suppression=True)

    async def test_suppression_failure_retains_the_claim_and_stops_retrying(self):
        await self._stop_after_result(refuse_suppression=True, unexpected=True)

    async def _stop_after_result(self, *, refuse_suppression, unexpected=False):
        hf, hf_runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, _, _ = await self.enterAsyncContext(scientific_lane("chf"))
        api = SharedApi({"job:hf": (hf.generation.analysis_kind_ref, canonical_json_bytes(HF_VALUE))})
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "state").mkdir(mode=0o700)
        (root / "ipc").mkdir(mode=0o700)
        endpoint = DeliveryEndpoint(api.origin, api.provider_ref, root / "ipc/report.sock")
        queue = DeliveryQueue(root / "state", limits=QueueLimits(4, 1048576, 4, 1048576))
        inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2, on_accepted=Mock(),
                              on_failure=Mock(), binding_status=lambda _endpoint: {"state": "inbox_only"})
        await inbox.start()
        service = ProviderProcess(
            runtime=GenerationRuntime("sha256:" + "1" * 64, hf, chf),
            hf_session=hf_runner, chf_session=chf_runner, interpreter=interpreter(),
            new_api=api.new_api,
            new_delivery=lambda: DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=0.2),
            policy=ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30)),
            announcement=ProviderAnnouncement("NMRPeak", "Stop fixture", tuple(
                AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering") for lane in (hf, chf))),
        )
        original_execute = NmrpeakAnalysisSession.execute
        cleanup_failures = []

        async def stop_after_result(session, prepared, *, report_condition):
            result = await original_execute(session, prepared, report_condition=report_condition)
            service._scientific_stopping.set()
            return result

        def bounded_notice(message, cause):
            if message == "Original Attempt cleanup or report remains owned":
                cleanup_failures.append(cause)
                if len(cleanup_failures) == 2:
                    raise AssertionError("Shutdown repeated an unresolved settlement")

        try:
            refusal = (RuntimeError("Fixture disposition failure") if unexpected
                       else ExecutionRefused("Fixture refuses suppression"))
            refuse = AsyncMock(side_effect=refusal)
            disposition = (patch.object(AttemptExecutor, "retain_suppressed_disposition", refuse)
                           if refuse_suppression else nullcontext())
            with patch.object(NmrpeakAnalysisSession, "execute", stop_after_result), \
                 patch.object(service, "_notice", bounded_notice), disposition:
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await asyncio.wait_for(service.run(on_ready=Mock()), 10)
            self.assertIsNone(caught.exception.subgroup(lambda error: isinstance(error, AssertionError)))
            controller = service.lanes[0].controller
            self.assertTrue(channel.generate_received.is_set())
            self.assertIsNotNone(caught.exception.subgroup(lambda error: error is controller.last_failure))
            if refuse_suppression:
                refuse.assert_awaited_once()
                self.assertIsNotNone(caught.exception.subgroup(lambda error: error is refusal))
                self.assertFalse(service.shutdown_ready)
                self.assertIn(controller, service._claims.live_owners())
                self.assertTrue(service.api.retirement_confirmed)
                self.assertTrue(service.hello_api.retirement_confirmed)
                self.assertTrue(service.delivery.retirement_confirmed)
            else:
                self.assertIsInstance(controller.operation._checkpoint, SuppressedCheckpoint)
                self.assertTrue(service.shutdown_ready)
                self.assertFalse(service._claims.live_owners())
            self.assertFalse(queue.inventory(api.origin, api.provider_ref))
            self.assertFalse(any(path.endswith(("/complete", "/fail")) for _, path, _ in api.calls))
        finally:
            await inbox.close()
            queue.close()

    async def test_unconfirmed_generation_keeps_claim_and_never_hands_off_failure(self):
        hf, hf_runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, _, _ = await self.enterAsyncContext(scientific_lane("chf"))
        channel._fault = FakeRunnerFault.WRONG_RESULT_CORRELATION
        api = SharedApi({"job:hf": (hf.generation.analysis_kind_ref, canonical_json_bytes(HF_VALUE))})
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "state").mkdir(mode=0o700)
        (root / "ipc").mkdir(mode=0o700)
        endpoint = DeliveryEndpoint(api.origin, api.provider_ref, root / "ipc/report.sock")
        queue = DeliveryQueue(root / "state", limits=QueueLimits(4, 1048576, 4, 1048576))
        inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2, on_accepted=Mock(),
                              on_failure=Mock(), binding_status=lambda _endpoint: {"state": "inbox_only"})
        await inbox.start()
        service = ProviderProcess(
            runtime=GenerationRuntime("sha256:" + "1" * 64, hf, chf),
            hf_session=hf_runner, chf_session=chf_runner, interpreter=interpreter(),
            new_api=api.new_api,
            new_delivery=lambda: DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=0.2),
            policy=ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30)),
            announcement=ProviderAnnouncement("NMRPeak", "Unconfirmed fixture", tuple(
                AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering") for lane in (hf, chf))),
        )
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop_errors = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            with self.assertRaises(BaseExceptionGroup) as caught:
                await asyncio.wait_for(service.run(on_ready=Mock()), 10)
            controller = service.lanes[0].controller
            self.assertIsNotNone(controller)
            self.assertIs(type(controller.last_failure), ControllerFailed)
            self.assertIn("response type or correlation is wrong", str(controller.last_failure.cause.cause))
            self.assertIn(controller, service._claims.live_owners())
            self.assertTrue(hf_runner.retirement_confirmed)
            self.assertTrue(service.shutdown_ready)
            self.assertFalse(queue.inventory(api.origin, api.provider_ref))
            self.assertFalse(any(path.endswith(("/complete", "/fail")) for _, path, _ in api.calls))
            self.assertIsNotNone(caught.exception.subgroup(lambda error: error is controller.last_failure))
            await asyncio.sleep(0)
            self.assertEqual(loop_errors, [])
        finally:
            loop.set_exception_handler(previous_handler)
            await inbox.close()
            queue.close()

    async def test_stop_before_run_performs_no_inventory_or_hello_exchange(self):
        service = await self._idle_process()
        ready = Mock()
        with patch.object(service.delivery, "pending", new=AsyncMock()) as pending, \
                patch.object(service, "_announce", new=AsyncMock()) as announce:
            service.request_stop()
            await service.run(on_ready=ready)
        pending.assert_not_awaited()
        announce.assert_not_awaited()
        ready.assert_not_called()
        self.assertTrue(service.shutdown_ready)

    async def test_stop_during_announcement_does_not_start_lanes(self):
        service = await self._idle_process()
        ready = Mock()

        async def announce():
            service.request_stop()

        with patch.object(service, "_announce", side_effect=announce) as called:
            await service.run(on_ready=ready)
        called.assert_awaited_once()
        ready.assert_not_called()
        self.assertTrue(service.shutdown_ready)

    async def _idle_process(self, *, with_job=False):
        hf, hf_runner, _, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, _, _ = await self.enterAsyncContext(scientific_lane("chf"))
        api = SharedApi({"job:hf": (hf.generation.analysis_kind_ref, canonical_json_bytes(HF_VALUE))}
                        if with_job else {})
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        endpoint = DeliveryEndpoint(api.origin, api.provider_ref, root / "report.sock")
        return ProviderProcess(
            runtime=GenerationRuntime("sha256:" + "1" * 64, hf, chf),
            hf_session=hf_runner, chf_session=chf_runner, interpreter=interpreter(),
            new_api=api.new_api,
            new_delivery=lambda: DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=0.2),
            policy=ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30)),
            announcement=ProviderAnnouncement("NMRPeak", "Idle fixture", tuple(
                AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering")
                for lane in (hf, chf))),
        )

    async def test_fatal_client_cleanup_does_not_skip_independent_owners(self):
        service = await self._idle_process()
        failure = BaseExceptionGroup("Original API retirement failed", [SystemExit(7)])
        hello, delivery = service.hello_api.retire, service.delivery.retire
        events = []

        async def broken(*, owner):
            events.append("api")
            raise failure

        async def retire_hello(*, owner):
            events.append("hello")
            await hello(owner=owner)

        async def retire_delivery():
            events.append("delivery")
            await delivery()

        with patch.object(service.api, "retire", broken), \
             patch.object(service.hello_api, "retire", retire_hello), \
             patch.object(service.delivery, "retire", retire_delivery):
            with self.assertRaises(BaseExceptionGroup) as caught:
                await service._drain()
        self.assertEqual(events, ["api", "hello", "delivery"])
        self.assertIn(failure, caught.exception.exceptions)
        self.assertTrue(service.shutdown_ready)

    async def test_cancellation_during_settlement_backoff_retires_independent_clients(self):
        service = await self._idle_process()
        controller = Mock(request_stop=Mock(), join=AsyncMock(), operation=None,
                          shutdown_ready=False, settle=AsyncMock(side_effect=RuntimeError("still owned")))
        with patch.object(service, "_controllers", return_value=(controller,)), \
             patch.object(service, "_notice"), \
             patch("nmrpeak_provider.provider_process.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(BaseExceptionGroup) as caught:
                await service._drain()
        self.assertIsNotNone(caught.exception.subgroup(lambda error: isinstance(error, asyncio.CancelledError)))
        self.assertTrue(service.api.retirement_confirmed)
        self.assertTrue(service.hello_api.retirement_confirmed)
        self.assertTrue(service.delivery.retirement_confirmed)
        self.assertFalse(service.shutdown_ready)

    async def test_independent_cleanup_failure_retains_the_primary_failure(self):
        service = await self._idle_process()
        primary = RuntimeError("Original provider announcement failure")
        cleanup = BaseExceptionGroup("Independent API cleanup failure", [SystemExit(7)])
        with patch.object(service, "_announce", AsyncMock(side_effect=primary)), \
             patch.object(service.api, "retire", AsyncMock(side_effect=cleanup)):
            with self.assertRaises(BaseExceptionGroup) as caught:
                await service.run(on_ready=Mock())
        self.assertIs(caught.exception.exceptions[0], primary)
        self.assertTrue(service.shutdown_ready)

    async def test_stop_during_both_lanes_hands_off_their_original_exact_results(self):
        await self._two_lane_stop(fill_queue=False)

    async def test_delivery_backpressure_retains_both_original_reports_until_space_is_available(self):
        await self._two_lane_stop(fill_queue=True)

    async def test_unavailable_delivery_retains_both_original_reports_until_reachable(self):
        await self._two_lane_stop(fill_queue=False, unavailable=True)

    async def test_lost_acceptance_reply_replays_the_original_shared_handoff(self):
        await self._two_lane_stop(fill_queue=False, lost_ack=True)

    async def _two_lane_stop(self, *, fill_queue, unavailable=False, lost_ack=False):
        hf, hf_runner, hf_channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, chf_channel, _ = await self.enterAsyncContext(scientific_lane("chf"))
        runtime = GenerationRuntime("sha256:" + "1" * 64, hf, chf)
        api = SharedApi({"job:hf": (hf.generation.analysis_kind_ref, canonical_json_bytes(HF_VALUE)),
                         "job:chf": (chf.generation.analysis_kind_ref, canonical_json_bytes(VALUE))})
        rendezvous = Barrier(2)
        generating, release = Event(), Event()
        for channel in (hf_channel, chf_channel):
            def generate(frame, original=channel._accept_generate):
                rendezvous.wait(timeout=2)
                generating.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("Scientific fixture release was not granted")
                original(frame)
            channel._accept_generate = generate
        accepted = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state").mkdir(mode=0o700)
            (root / "ipc").mkdir(mode=0o700)
            endpoint = DeliveryEndpoint(api.origin, api.provider_ref, root / "ipc/http.sock")
            queue = DeliveryQueue(root / "state", limits=QueueLimits(4, 1048576, 4, 1048576))
            fields = {"origin": api.origin, "profile": "run", "topology": "web",
                      "provider_ref": api.provider_ref, "credential_file": "/fixture/credential",
                      "exchange_seconds": 2, "retirement_seconds": 0.2}
            credential = credential_bytes(api.identity.private_key)
            _, _, publication = api_settings_with_material(fields, lambda *_args, **_kwargs: credential)

            def on_accepted(_endpoint):
                accepted.append(True)

            inbox = SharedDeliveryInbox(queue, SharedDeliverySocket(endpoint.socket_path, None), request_seconds=2,
                                        on_accepted=on_accepted, on_failure=Mock())
            original_respond = inbox._respond
            replies_lost = []
            async def respond(connection, writer, status, document):
                if lost_ack and status == 200 and not replies_lost:
                    replies_lost.append(True)
                    writer.transport.abort()
                    return
                await original_respond(connection, writer, status, document)
            inbox._respond = respond
            await inbox.start()
            try:
                policy = ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30))
                ready = Mock()
                service = ProviderProcess(
                    runtime=runtime, hf_session=hf_runner, chf_session=chf_runner,
                    interpreter=interpreter(), new_api=api.new_api,
                    new_delivery=lambda: SharedDeliveryClient(endpoint, publication, exchange_seconds=2,
                                                               retirement_seconds=0.2),
                    policy=policy, announcement=ProviderAnnouncement("NMRPeak", "Scientific fixture", tuple(
                        AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering")
                        for lane in (hf, chf))),
                )
                pressure = asyncio.Event()
                fillers = []
                retry = service._delivery_retry

                def retain_pressure(lane, cause):
                    self.assertIsInstance(cause, DeliveryUnavailable if (unavailable or lost_ack) else DeliveryFull)
                    retry(lane, cause)
                    if len(service._delivery_waiters) == 2:
                        pressure.set()

                with patch.object(service, '_delivery_retry', side_effect=retain_pressure):
                    original = asyncio.create_task(service.run(on_ready=ready))
                    try:
                        self.assertTrue(await asyncio.to_thread(generating.wait, 2))
                        if fill_queue:
                            for index in range(4):
                                reference = 'execution_attempt:sha256:' + str(index)*64
                                item = DeliveryOutcome(api.origin, api.provider_ref, f'job:filler{index}',
                                    reference, TerminalOperation.FAIL, fail_body(reference, 'fixture_failure', 'Fixture retained report.'))
                                queue.enqueue(item)
                                fillers.append(item)
                        if unavailable:
                            await inbox.close()
                        if fill_queue or unavailable:
                            release.set()
                            await asyncio.wait_for(pressure.wait(), 2)
                            self.assertEqual(len(api.started), 2)
                            self.assertEqual(len(service._claims.live_owners()), 2)
                            self.assertTrue(service._admission_paused.is_set())
                            self.assertTrue(hf_runner.quiescent and chf_runner.quiescent)
                        service.request_stop()
                        self.assertFalse(original.done())
                    finally:
                        service.request_stop()
                        release.set()
                        if fill_queue:
                            for item in fillers:
                                queue.remove_published(item)
                        if unavailable:
                            await inbox.start()
                        await asyncio.wait_for(original, 5)
                ready.assert_called_once()
                self.assertEqual(bool(replies_lost), lost_ack)
                self.assertEqual(len(api.started), 2)
                self.assertEqual(len(queue.inventory(api.origin, api.provider_ref)), 2)
                for lane in (hf, chf):
                    handoff = queue.next_pending_handoff(api.origin, api.provider_ref)
                    self.assertEqual(handoff.publication.credential_bytes, credential)
                    item = handoff.outcome
                    import json
                    document = json.loads(item.body)
                    selected = hf if item.job_ref == "job:hf" else chf
                    self.assertEqual(b64decode(document["canonical_result_base64"]),
                                     canonical_result_bytes(["CCO", "OCC"], selected.result_facts))
                    queue.remove_published(item)
                self.assertFalse(any(path.endswith(("/complete", "/fail")) for _, path, _ in api.calls))
                self.assertTrue(service.shutdown_ready)
            finally:
                await inbox.close()
                queue.close()
                await hf_runner.retire()
                await chf_runner.retire()
