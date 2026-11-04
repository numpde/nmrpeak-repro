"""Qualify NMR science through the installed shared per-Attempt session port."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import RecoveryScope, StartPending, acknowledge_start
from provider_core.attempt_request import start_body
from provider_core.attempt_response import AttemptAdmission
from provider_core.job_input import FeedInputFacts, VerifiedJobInput

from nmrpeak_provider.analysis_session import NmrpeakAnalysisSession
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.generation_runtime import GenerationLane
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.interpreter import (
    InterpreterUnavailable, InterpreterUnavailableReason,
)
from nmrpeak_provider.product_input import InputIssue, InputRejectionReason
from nmrpeak_provider.product_result import canonical_result_bytes
from nmrpeak_provider.run_generation import run_generation_fingerprint
from nmrpeak_provider.runner_session import (
    NativeObservationRequired, RunnerSessionRetired, ScientificCallInterrupted,
)
from nmrpeak_provider.runner_protocol import RunnerRejectionReason, runner_rejection_diagnostic
from nmrpeak_provider.text_provenance import ModelGeneratedText
from tests.fakes.native_runner import lane_contract
from tests.fakes.runner import FakeRunnerChannel, admit_fake_session
from tests.unit.test_generation_runtime import generation
from tests.fakes.interpreter_values import HF_VALUE, VALUE
from tests.unit.test_runner_session import ATTEMPT_KEY, ATTEMPT_REF, DEADLINES


@asynccontextmanager
async def scientific_lane(name, *, rejected_validations=0, validate_seconds=0.1):
    facts, codec, ready, model_input = lane_contract(name)
    lane = HF_LIFECYCLE_LANE if name == "hf" else CHF_LIFECYCLE_LANE
    configured = GenerationLane(lane, generation(lane.offering.analysis_kind_ref, name), facts, codec)
    channel = FakeRunnerChannel(codec, ready, candidates=("CCO", "OCC"),
                                rejected_validations=rejected_validations)
    async with admit_fake_session(channel, facts, replace(DEADLINES, connect_seconds=2, ready_seconds=2,
                                                        validate_seconds=validate_seconds,
                                                        retire_seconds=2), codec) as runner:
        yield configured, runner, channel, model_input


def admission(configured, raw):
    identity = FeedInputFacts("job:selected", "nmr.job.specification.text.v1",
                              "sha256:" + sha256(raw).hexdigest(), len(raw))
    scope = RecoveryScope("https://api.example.test", configured.generation.provider_ref,
                          configured.result_facts.identity.runner_ref,
                          run_generation_fingerprint(configured.generation))
    pending = StartPending(scope, configured.lane.offering.analysis_kind_ref, identity,
                           start_body(identity.job_ref, ATTEMPT_KEY))
    receipt = AttemptAdmission(ATTEMPT_REF, identity.job_ref, pending.analysis_kind_ref,
                               scope.provider_ref, "in_progress", datetime.now(UTC), False)
    return acknowledge_start(pending, receipt), VerifiedJobInput(identity, raw)


def interpreter():
    configured = SimpleNamespace(diagnostic_secrets=(), validate_freeform_input_async=AsyncMock(), retire=AsyncMock(), join=AsyncMock())
    configured.new_session = lambda: configured
    return configured


def phase_reporter(*args, **kwargs):
    reporter = AsyncMock(*args, **kwargs)
    reporter.require_execution = Mock()
    return reporter


class AnalysisSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_prior_waiter_cancellation_does_not_hide_later_owned_cancellation(self):
        configured, runner, _, _ = await self.enterAsyncContext(scientific_lane("hf"))
        translator = interpreter()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def retire():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise asyncio.CancelledError()

        translator.retire.side_effect = retire
        owner = NmrpeakAnalysisSession(configured, runner, translator)

        async def interrupted_waiter():
            try:
                await owner.retire()
            except asyncio.CancelledError:
                pass
            release.set()
            await asyncio.wait({owner._retirement})
            with self.assertRaises(asyncio.CancelledError):
                await owner.retire()
            await owner.retire()

        waiter = asyncio.create_task(interrupted_waiter())
        await asyncio.wait_for(entered.wait(), 1)
        waiter.cancel()
        await asyncio.wait_for(waiter, 2)
        self.assertEqual(calls, 2)

    async def test_cancelled_owned_retirement_can_be_reobserved(self):
        configured, runner, _, _ = await self.enterAsyncContext(scientific_lane("hf"))
        translator = interpreter()
        translator.retire.side_effect = [asyncio.CancelledError(), None]
        owner = NmrpeakAnalysisSession(configured, runner, translator)
        with self.assertRaises(asyncio.CancelledError):
            await owner.retire()
        await owner.retire()
        self.assertEqual(translator.retire.await_count, 2)

    async def test_revoked_permission_prevents_generation_submission(self):
        configured, runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        owner = NmrpeakAnalysisSession(configured, runner, interpreter())
        acknowledged, instructions = admission(configured, canonical_json_bytes(HF_VALUE))
        prepared = await owner.prepare(acknowledged, instructions, report_condition=phase_reporter())
        reporter = phase_reporter()
        failure = RuntimeError("Attempt permission withdrawn")
        reporter.require_execution.side_effect = failure
        before = list(channel.received_frames)
        try:
            with self.assertRaises(RuntimeError) as caught:
                await owner.execute(prepared, report_condition=reporter)
            self.assertIs(caught.exception, failure)
            self.assertEqual(channel.received_frames, before)
        finally:
            await owner.retire()

    async def test_revoked_permission_prevents_validation_submission(self):
        configured, runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
        owner = NmrpeakAnalysisSession(configured, runner, interpreter())
        acknowledged, instructions = admission(configured, canonical_json_bytes(HF_VALUE))
        reporter = phase_reporter()
        failure = RuntimeError("Attempt permission withdrawn")
        reporter.require_execution.side_effect = failure
        try:
            with self.assertRaises(RuntimeError) as caught:
                await owner.prepare(acknowledged, instructions, report_condition=reporter)
            self.assertIs(caught.exception, failure)
            self.assertEqual(channel.received_frames, [])
        finally:
            await owner.retire()

    async def test_interpreter_callback_checks_permission_after_await(self):
        configured, runner, channel, model_input = await self.enterAsyncContext(scientific_lane("hf"))
        translator = interpreter()
        reporter = phase_reporter()
        failure = RuntimeError("Attempt permission withdrawn during interpretation")

        async def translate(**arguments):
            await asyncio.sleep(0)
            reporter.require_execution.side_effect = failure
            return await arguments["validate_input"](model_input)

        translator.validate_freeform_input_async.side_effect = translate
        owner = NmrpeakAnalysisSession(configured, runner, translator)
        acknowledged, instructions = admission(configured, b"Interpret these peaks.")
        try:
            with self.assertRaises(RuntimeError) as caught:
                await owner.prepare(acknowledged, instructions, report_condition=reporter)
            self.assertIs(caught.exception, failure)
            self.assertEqual(channel.received_frames, [])
        finally:
            await owner.retire()

    async def test_cancelled_native_observation_retains_the_original_scientific_failure(self):
        from unittest.mock import patch
        _, runner, _, model_input = await self.enterAsyncContext(scientific_lane("hf"))
        failure = NativeObservationRequired("Scientific peer closed before native observation")
        observing = asyncio.Event()

        async def observe(_cause):
            observing.set()
            await asyncio.Event().wait()

        with patch.object(runner, "_validate", side_effect=failure), patch.object(
            runner, "observe_failure", side_effect=observe,
        ):
            original = runner.validate(execution_attempt_ref=ATTEMPT_REF,
                                       provider_attempt_key=ATTEMPT_KEY, model_input=model_input)
            joining = asyncio.create_task(runner.join_science(original))
            await asyncio.wait_for(observing.wait(), 1)
            joining.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await joining
        with self.assertRaises(RunnerSessionRetired) as caught:
            await runner.retire()
        self.assertIs(caught.exception.__cause__.exceptions[0], failure)
        self.assertTrue(runner.retirement_confirmed)

    async def test_local_shutdown_wake_is_not_an_independent_scientific_failure(self):
        from unittest.mock import patch
        from tests.fakes.runner import FakeRunnerFault
        _, runner, channel, model_input = await self.enterAsyncContext(scientific_lane("hf"))
        validated = await runner.join_science(runner.validate(
            execution_attempt_ref=ATTEMPT_REF, provider_attempt_key=ATTEMPT_KEY,
            model_input=model_input,
        ))
        channel._fault = FakeRunnerFault.BLOCK_GENERATION
        original = runner.generate(validated)
        self.assertTrue(await asyncio.to_thread(channel.generate_received.wait, 1))

        def close_after_reader_wakes(stream):
            stream.shutdown(2)
            try:
                original.result(timeout=1)
            except RunnerSessionRetired:
                pass
            stream.close()
            return None

        with patch("nmrpeak_provider.runner_session._close_channel", side_effect=close_after_reader_wakes):
            await runner.retire()
        self.assertIsInstance(original.exception(), ScientificCallInterrupted)
        self.assertTrue(runner.retirement_confirmed)

    async def test_cancelled_caller_does_not_erase_a_later_independent_scientific_failure(self):
        from threading import Event
        from unittest.mock import patch
        configured, runner, _, _ = await self.enterAsyncContext(scientific_lane("chf"))
        owner = NmrpeakAnalysisSession(configured, runner, interpreter())
        acknowledged, instructions = admission(configured, canonical_json_bytes(VALUE))
        entered, release = Event(), Event()
        failure = RuntimeError("Independent validation adapter failure")

        def failed(**_arguments):
            entered.set()
            release.wait(2)
            raise failure

        with patch.object(runner, "_validate", side_effect=failed):
            original = asyncio.create_task(owner.prepare(acknowledged, instructions, report_condition=phase_reporter()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            original.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await original
            release.set()
            self.assertIs(await asyncio.to_thread(runner._scientific_call.exception, 1), failure)
            cleanup = asyncio.create_task(owner.retire())
            with self.assertRaises(RunnerSessionRetired) as caught:
                await cleanup
        self.assertIs(caught.exception.__cause__.exceptions[0], failure)
        self.assertTrue(runner.retirement_confirmed)

    async def test_scientific_overlap_is_rejected_instead_of_queued(self):
        from threading import Event
        from unittest.mock import patch
        _, runner, channel, model_input = await self.enterAsyncContext(scientific_lane("hf"))
        entered, release = Event(), Event()
        validate = runner._validate

        def blocked(**arguments):
            entered.set()
            release.wait(2)
            return validate(**arguments)

        with patch.object(runner, "_validate", side_effect=blocked):
            original = runner.validate(execution_attempt_ref=ATTEMPT_REF,
                                       provider_attempt_key=ATTEMPT_KEY, model_input=model_input)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                with self.assertRaisesRegex(RuntimeError, "overlap"):
                    runner.validate(execution_attempt_ref=ATTEMPT_REF,
                                    provider_attempt_key=ATTEMPT_KEY, model_input=model_input)
            finally:
                release.set()
                await runner.join_science(original)
        self.assertEqual(len(channel.received_frames), 1)

    async def test_delayed_dispatch_cannot_renew_the_scientific_phase_deadline(self):
        from threading import Event
        from unittest.mock import patch
        _, runner, channel, model_input = await self.enterAsyncContext(scientific_lane("hf"))
        entered, release = Event(), Event()
        validate = runner._validate

        def delayed(**arguments):
            entered.set()
            release.wait(2)
            return validate(**arguments)

        with patch.object(runner, "_validate", side_effect=delayed):
            original = runner.validate(execution_attempt_ref=ATTEMPT_REF,
                                       provider_attempt_key=ATTEMPT_KEY, model_input=model_input)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.sleep(0.15)
            finally:
                release.set()
            with self.assertRaises(RunnerSessionRetired):
                await runner.join_science(original)
        self.assertEqual(channel.received_frames, [])

    async def test_typed_rejection_releases_the_warm_owner_after_preparation(self):
        for prose in (False, True):
            with self.subTest(prose=prose):
                configured, runner, _, model_input = await self.enterAsyncContext(scientific_lane("chf", rejected_validations=1))
                translator = interpreter()

                async def translate(**arguments):
                    rejection = await arguments["validate_input"](model_input)
                    raise InterpreterUnavailable(InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED, ("fixture",))

                translator.validate_freeform_input_async.side_effect = translate
                raw = b"Interpret these peaks." if prose else canonical_json_bytes(VALUE)
                owner = NmrpeakAnalysisSession(configured, runner, translator)
                acknowledged, instructions = admission(configured, raw)
                outcome = await owner.prepare(acknowledged, instructions, report_condition=phase_reporter())
                self.assertIsInstance(outcome, AnalysisFailure)
                await owner.retire()
                self.assertFalse(runner.retired)
                await runner.retire()

    async def test_exhausted_interpreter_finishes_once_without_restarting_the_ladder(self):
        for reason in (InterpreterUnavailableReason.DEADLINE_EXCEEDED,
                       InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED):
            with self.subTest(reason=reason):
                configured, runner, channel, _ = await self.enterAsyncContext(scientific_lane("hf"))
                translator = interpreter()
                translator.diagnostic_secrets = ("private-credential",)
                unavailable = InterpreterUnavailable(reason)
                unavailable.add_note("safe context, private-credential")
                translator.validate_freeform_input_async.side_effect = unavailable
                owner = NmrpeakAnalysisSession(configured, runner, translator)
                acknowledged, instructions = admission(configured, b"Interpret these peaks.")
                conditions = phase_reporter()
                outcome = await owner.prepare(acknowledged, instructions, report_condition=conditions)
                self.assertIsInstance(outcome, AnalysisFailure)
                self.assertEqual(outcome.code, "interpretation_failed")
                self.assertIn("could not be completed", outcome.message)
                self.assertNotIn("private-credential", outcome.message)
                evidence = json.loads(outcome.evidence.content)
                self.assertIn("safe context", evidence["cause"])
                self.assertNotIn("private-credential", evidence["cause"])
                self.assertEqual(translator.validate_freeform_input_async.await_count, 1)
                self.assertEqual(conditions.await_count, 0)
                self.assertEqual(channel.received_frames, [])
                await owner.retire()

    async def test_both_lanes_return_exact_results_and_release_healthy_warm_owner(self):
        for name, document in (("hf", HF_VALUE), ("chf", VALUE)):
            with self.subTest(lane=name):
                configured, runner, _, _ = await self.enterAsyncContext(scientific_lane(name))
                translator = interpreter()
                for _ in range(2):
                    owner = NmrpeakAnalysisSession(configured, runner, translator)
                    acknowledged, instructions = admission(configured, canonical_json_bytes(document))
                    handle = await owner.prepare(acknowledged, instructions, report_condition=phase_reporter())
                    self.assertIsInstance(handle, PreparedAnalysis)
                    outcome = await owner.execute(handle, report_condition=phase_reporter())
                    self.assertIsInstance(outcome, AnalysisSuccess)
                    self.assertEqual(outcome.content, canonical_result_bytes(["CCO", "OCC"], runner.result_facts))
                    await owner.retire()
                    self.assertFalse(runner.retired)
                translator.validate_freeform_input_async.assert_not_awaited()
                await runner.retire()

    async def test_malformed_structured_source_cannot_fall_back_to_interpretation(self):
        configured, runner, channel, _ = await self.enterAsyncContext(scientific_lane("chf"))
        translator = interpreter()
        owner = NmrpeakAnalysisSession(configured, runner, translator)
        acknowledged, instructions = admission(configured, b'{"model_input":')
        outcome = await owner.prepare(acknowledged, instructions, report_condition=phase_reporter())
        self.assertIsInstance(outcome, AnalysisFailure)
        translator.validate_freeform_input_async.assert_not_awaited()
        self.assertEqual(channel.received_frames, [])
        await owner.retire()
        self.assertFalse(runner.retired)
        await runner.retire()

    async def test_prose_uses_the_async_interpreter_validation_port(self):
        configured, runner, _, model_input = await self.enterAsyncContext(scientific_lane("chf"))
        translator = interpreter()

        async def translate(**arguments):
            return await arguments["validate_input"](model_input)

        translator.validate_freeform_input_async.side_effect = translate
        owner = NmrpeakAnalysisSession(configured, runner, translator)
        acknowledged, instructions = admission(configured, b"Formula C2H6O. Interpret these NMR peaks.")
        handle = await owner.prepare(acknowledged, instructions, report_condition=phase_reporter())
        self.assertIsInstance(handle, PreparedAnalysis)
        translator.validate_freeform_input_async.assert_awaited_once()
        await owner.retire()
        self.assertTrue(runner.retirement_confirmed)

    async def test_prepared_handle_is_consumed_once_and_foreign_handle_is_refused(self):
        configured, runner, channel, _ = await self.enterAsyncContext(scientific_lane("chf"))
        owner = NmrpeakAnalysisSession(configured, runner, interpreter())
        acknowledged, instructions = admission(configured, canonical_json_bytes(VALUE))
        handle = await owner.prepare(acknowledged, instructions, report_condition=phase_reporter())
        foreign = PreparedAnalysis(handle.identity)
        with self.assertRaises(ValueError):
            await owner.execute(foreign, report_condition=phase_reporter())
        self.assertEqual(len(channel.received_frames), 1)
        await owner.execute(handle, report_condition=phase_reporter())
        with self.assertRaises(ValueError):
            await owner.execute(handle, report_condition=phase_reporter())
        self.assertEqual(len(channel.received_frames), 2)
        await owner.retire()
        await runner.retire()

    async def test_cancellation_joins_the_original_blocking_validation(self):
        configured, runner, _, _ = await self.enterAsyncContext(scientific_lane("chf"))
        owner = NmrpeakAnalysisSession(configured, runner, interpreter())
        acknowledged, instructions = admission(configured, canonical_json_bytes(VALUE))
        from threading import Event
        from unittest.mock import patch
        entered, release = Event(), Event()
        validate = runner._validate

        def blocked(**arguments):
            entered.set()
            release.wait(2)
            return validate(**arguments)

        with patch.object(runner, "_validate", side_effect=blocked):
            original = asyncio.create_task(owner.prepare(acknowledged, instructions, report_condition=phase_reporter()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            original.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await original
            cleanup = asyncio.create_task(owner.retire())
            await asyncio.sleep(0)
            self.assertFalse(cleanup.done())
            release.set()
            await cleanup
        self.assertTrue(runner.retirement_confirmed)


class InterpretationRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_science_does_not_discharge_unconfirmed_http_retirement(self):
        configured, runner, _channel, _input = await self.enterAsyncContext(scientific_lane('hf'))
        translator = interpreter()
        entered = asyncio.Event()
        failure = OSError('Original model connection did not retire')

        async def interpreting(**_arguments):
            entered.set()
            await asyncio.Event().wait()

        translator.validate_freeform_input_async.side_effect = interpreting
        translator.retire.side_effect = failure
        owner = NmrpeakAnalysisSession(configured, runner, translator)
        acknowledged, instructions = admission(configured, b'Proton peaks described in prose.')
        task = asyncio.create_task(owner.prepare(acknowledged, instructions, report_condition=phase_reporter()))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(runner.quiescent)
            with self.assertRaises(OSError) as caught:
                await owner.retire()
            self.assertIs(caught.exception, failure)
            translator.retire.assert_awaited_once_with()
            self.assertFalse(runner.retired)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_completed_science_cannot_be_replaced_until_its_original_outcome_is_observed(self):
        _configured, runner, _channel, model_input = await self.enterAsyncContext(scientific_lane('hf', rejected_validations=1))
        original = runner.validate(execution_attempt_ref=ATTEMPT_REF, provider_attempt_key=ATTEMPT_KEY,
                                   model_input=model_input)
        await asyncio.to_thread(original.result, 1)
        self.assertFalse(runner.quiescent)
        with self.assertRaisesRegex(RuntimeError, 'join the original operation'):
            runner.validate(execution_attempt_ref=ATTEMPT_REF, provider_attempt_key=ATTEMPT_KEY,
                            model_input=model_input)
        await runner.join_science(original)
        self.assertTrue(runner.quiescent)

    async def test_http_failure_does_not_skip_independent_scientific_retirement(self):
        configured, _runner, _channel, _input = await self.enterAsyncContext(scientific_lane('hf'))
        http, native = OSError('HTTP retirement'), OSError('Scientific retirement')
        translator = interpreter()
        translator.retire.side_effect = http
        runner = SimpleNamespace(result_facts=configured.result_facts, quiescent=False,
                                 retire=AsyncMock(side_effect=native))
        owner = NmrpeakAnalysisSession(configured, runner, translator)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await owner.retire()
        self.assertEqual(caught.exception.exceptions, (http, native))
        runner.retire.assert_awaited_once_with()
