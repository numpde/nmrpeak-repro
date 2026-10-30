"""Run both real NMR adapters through SDK controllers and durable shared IPC."""

import asyncio
from base64 import b64decode
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import AsyncMock, Mock, patch

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryFull, DeliveryOutcome, DeliveryQueue, QueueLimits
from provider_core.analysis import AnalysisOutcomeUnresolved
from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import fail_body, start_body
from provider_core.controller import ControllerFailed
from provider_core.execution import AttemptExecutor, ExecutionRefused, ExecutionStopRequested
from provider_core.hello import AnalysisOffering
from provider_core.producer_checkpoint import SuppressedCheckpoint
from provider_core.ownership import AttemptClaims, LiveAttemptLimits

from nmrpeak_provider.analysis_session import NmrpeakAnalysisSession
from nmrpeak_provider.input_interpreter import InputInterpreter
from nmrpeak_provider.interpreter import InterpreterEndpoint, InterpreterTool, InterpreterUnavailable, InterpreterUnavailableReason
from nmrpeak_provider.interpreter_policy import InterpreterPolicy, OpenAIChatCallPolicy
from nmrpeak_provider.openai_chat_interpreter import OpenAIChatEndpointSpec
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.generation_runtime import GenerationRuntime
from nmrpeak_provider.product_result import canonical_result_bytes
from nmrpeak_provider.provider_config import ObservationPolicy, ProviderProcessPolicy
from nmrpeak_provider.provider_process import ProviderAnnouncement, ProviderProcess
from nmrpeak_provider.runner_protocol import GenerateFrame, ValidateFrame
from tests.fakes.shared_api import SharedApi
from tests.fakes.runner import FakeRunnerFault
from tests.unit.test_analysis_session import scientific_lane
from tests.unit.test_input_interpreter import BoundEndpoints, HF_VALUE, VALUE, turn
from tests.unit.test_analysis_session import interpreter


class SharedProviderProcessTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_interpreter_deadline_during_validation_keeps_attempt_without_terminal_report(self):
        hf, hf_runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        chf, chf_runner, _, _ = await self.enterAsyncContext(scientific_lane("chf"))
        api = SharedApi({"job:hf": (hf.generation.analysis_kind_ref, b"Interpret these peaks.")})
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "state").mkdir(mode=0o700)
        (root / "ipc").mkdir(mode=0o700)
        endpoint = DeliveryEndpoint(api.origin, api.provider_ref, root / "ipc/report.sock")
        queue = DeliveryQueue(root / "state", limits=QueueLimits(4, 1048576, 4, 1048576))
        inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2, on_accepted=Mock(),
                              on_failure=Mock(), binding_status=lambda _endpoint: {"state": "inbox_only"})
        await inbox.start()
        calls = []
        async def interpret_call(_messages):
            calls.append(None)
            if len(calls) == 1:
                return turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": HF_VALUE})
            return turn(InterpreterTool.REPORT_INPUT_PROBLEM, {"message": "The source is incomplete."})

        configured = InputInterpreter((OpenAIChatEndpointSpec(
            configuration_id="fixture", base_url="https://interpreter.invalid/v1",
            api_key="fixture-only", model="fixture", reasoning_effort=None),),
            InterpreterPolicy(call_policy=OpenAIChatCallPolicy(request_timeout_seconds=1, turn_timeout_seconds=3),
                              interpretation_timeout_seconds=0.1))
        service = ProviderProcess(
            runtime=GenerationRuntime("sha256:" + "1" * 64, hf, chf),
            hf_session=hf_runner, chf_session=chf_runner, interpreter=configured,
            new_api=api.new_api,
            new_delivery=lambda: DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=0.2),
            policy=ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30)),
            announcement=ProviderAnnouncement("NMRPeak", "Deadline fixture", tuple(
                AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering") for lane in (hf, chf))),
        )
        entered, release = Event(), Event()
        task = None
        original_validate = channel._accept_validate
        def delayed_validation(frame):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("Fixture validation was not released")
            original_validate(frame)

        try:
            with patch("nmrpeak_provider.input_interpreter.bind_openai_chat_endpoints",
                       return_value=BoundEndpoints((InterpreterEndpoint("fixture", interpret_call),))), \
                 patch.object(channel, "_accept_validate", delayed_validation):
                task = asyncio.create_task(service.run(on_ready=Mock()))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    await asyncio.sleep(0.25)
                finally:
                    release.set()
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await asyncio.wait_for(task, 10)
            self.assertFalse(queue.inventory(api.origin, api.provider_ref))
            controller = service.lanes[0].controller
            self.assertIsNotNone(caught.exception.subgroup(lambda error: error is controller.last_failure))
            unresolved = controller.last_failure.cause.cause
            self.assertIsInstance(unresolved, AnalysisOutcomeUnresolved)
            self.assertIsInstance(unresolved.__cause__, InterpreterUnavailable)
            self.assertIs(unresolved.__cause__.reason, InterpreterUnavailableReason.DEADLINE_EXCEEDED)
            self.assertIn(controller, service._claims.live_owners())
            self.assertTrue(hf_runner.retirement_confirmed)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sum(type(frame) is ValidateFrame for frame in channel.received_frames), 1)
            self.assertFalse(any(type(frame) is GenerateFrame for frame in channel.received_frames))
            self.assertFalse(any(path.endswith(("/complete", "/fail")) for _, path, _ in api.calls))
            self.assertFalse(any(body.get("condition_code") == "interpreter_deadline_exceeded"
                                 for _, _, body in api.calls))
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await inbox.close()
            queue.close()

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

    async def _two_lane_stop(self, *, fill_queue):
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

            def on_accepted(_endpoint):
                accepted.append(True)

            inbox = DeliveryInbox(queue, (endpoint,), request_seconds=2,
                                  on_accepted=on_accepted, on_failure=Mock(),
                                  binding_status=lambda _endpoint: {"state": "inbox_only"})
            await inbox.start()
            try:
                policy = ProviderProcessPolicy(0.01, 60, 2, 0.2, 20, ObservationPolicy(0.01, 1, 30))
                ready = Mock()
                service = ProviderProcess(
                    runtime=runtime, hf_session=hf_runner, chf_session=chf_runner,
                    interpreter=interpreter(), new_api=api.new_api,
                    new_delivery=lambda: DeliveryClient(endpoint, exchange_seconds=2, retirement_seconds=0.2),
                    policy=policy, announcement=ProviderAnnouncement("NMRPeak", "Scientific fixture", tuple(
                        AnalysisOffering(lane.generation.analysis_kind_ref, "Fixture offering")
                        for lane in (hf, chf))),
                )
                pressure = asyncio.Event()
                fillers = []
                retry = service._delivery_retry

                def retain_pressure(lane, cause):
                    self.assertIsInstance(cause, DeliveryFull)
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
                        await asyncio.wait_for(original, 5)
                ready.assert_called_once()
                self.assertEqual(len(api.started), 2)
                self.assertEqual(len(queue.inventory(api.origin, api.provider_ref)), 2)
                for lane in (hf, chf):
                    item = queue.next_pending(api.origin, api.provider_ref)
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
