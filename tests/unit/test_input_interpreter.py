"""Verify Peak's product and runner boundaries through the shared interpreter."""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nmrpeak_provider.input_interpreter import InputInterpreter, _value_schema_for
from nmrpeak_provider.interpreter import InterpreterUnavailable, InterpreterUnavailableReason
from nmrpeak_provider.interpreter_policy import InterpreterPolicy
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.runner_session import RunnerInputRejected, ValidatedRunnerRequest
from nmrpeak_provider.runner_protocol import RunnerRejectionReason, runner_rejection_diagnostic
from tests.fakes.interpreter_values import HF_VALUE, VALUE
from tests.unit.test_shared_interpreter_adapter import Factory, reply


class ScriptedFactory(Factory):
    def __init__(self, name, *answers):
        super().__init__(name, None)
        self.answers = iter(answers)

    async def complete(self, messages, tools, **kwargs):
        self.answer = next(self.answers)
        return await super().complete(messages, tools, **kwargs)


class InputInterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def run_interpreter(self, lane, factories, *, source=b"Molecular formula C2H6O and reported NMR peaks.",
                              reject_once=False, validate=None, record_diagnostic=None):
        specs = tuple(SimpleNamespace(configuration_id=f"model-{i}", api_key=f"credential-{i}")
                      for i in range(len(factories)))
        admitted = ValidatedRunnerRequest(object(), object())
        rejection = RunnerInputRejected(runner_rejection_diagnostic(
            RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED, 512), 512)
        if validate is None:
            validate = AsyncMock(side_effect=[rejection, admitted] if reject_once else None,
                                 return_value=admitted)
        with patch("nmrpeak_provider.input_interpreter.endpoint_factories", return_value=factories):
            session = InputInterpreter(specs, InterpreterPolicy(interpretation_timeout_seconds=30)).new_session()
            try:
                result = await session.validate_freeform_input_async(
                    source=source, lane=lane, validate_input=validate, execution_attempt_ref="attempt",
                    record_diagnostic=record_diagnostic)
            finally:
                await session.retire()
                await session.join()
        return result, admitted, validate

    async def test_hf_and_chf_candidates_pass_product_and_runner_admission(self):
        for lane, value in ((HF_LIFECYCLE_LANE, HF_VALUE), (CHF_LIFECYCLE_LANE, VALUE)):
            with self.subTest(lane=lane.offering):
                factory = ScriptedFactory("model", reply("submit_interpretation", {"value": value}))
                result, admitted, validate = await self.run_interpreter(lane, (factory,))
                self.assertIs(result, admitted)
                validate.assert_awaited_once()
                self.assertEqual(validate.await_args.args[0].wire_document()["molecular_formula"], "C2H6O")

    async def test_invalid_candidate_can_be_repaired_without_skipping_product_parser(self):
        invalid = {**HF_VALUE, "model_input": {**HF_VALUE["model_input"], "formula": ""}}
        factory = ScriptedFactory("model", reply("submit_interpretation", {"value": invalid}),
                                  reply("submit_interpretation", {"value": HF_VALUE}))
        events = []
        result, admitted, validate = await self.run_interpreter(
            HF_LIFECYCLE_LANE, (factory,), record_diagnostic=events.append)
        self.assertIs(result, admitted)
        self.assertEqual(len(factory.requests), 2)
        validate.assert_awaited_once()
        self.assertTrue(any("failure_kind='correction'" in event for event in events))

    async def test_runner_rejection_is_corrected_before_admission(self):
        factory = ScriptedFactory("model", reply("submit_interpretation", {"value": HF_VALUE}),
                                  reply("submit_interpretation", {"value": HF_VALUE}))
        result, admitted, validate = await self.run_interpreter(HF_LIFECYCLE_LANE, (factory,), reject_once=True)
        self.assertIs(result, admitted)
        self.assertEqual(validate.await_count, 2)

    async def test_unsupported_multiplicity_does_not_reach_runner(self):
        invalid = deepcopy(HF_VALUE)
        invalid["model_input"]["spectra"]["1H"]["peaks"][0]["multiplicity"] = "br s"
        factory = ScriptedFactory("model", *(reply("submit_interpretation", {"value": invalid}) for _ in range(3)))
        validate = AsyncMock()
        with self.assertRaises(InterpreterUnavailable) as caught:
            await self.run_interpreter(HF_LIFECYCLE_LANE, (factory,), validate=validate)
        self.assertIs(caught.exception.reason, InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED)
        validate.assert_not_awaited()
        self.assertEqual(len(factory.requests), 3)

    async def test_report_hands_off_to_next_model(self):
        first = ScriptedFactory("first", reply("report_interpretation_problem", {"message": "Private claim."}))
        second = ScriptedFactory("second", reply("submit_interpretation", {"value": HF_VALUE}))
        result, admitted, validate = await self.run_interpreter(HF_LIFECYCLE_LANE, (first, second))
        self.assertIs(result, admitted)
        self.assertNotIn("Private claim.", str(second.requests))
        validate.assert_awaited_once()

    async def test_all_reports_are_unavailable_not_verified_input_failure(self):
        first = ScriptedFactory("first", reply("report_interpretation_problem", {"message": "Cannot interpret."}))
        with self.assertRaises(InterpreterUnavailable) as caught:
            await self.run_interpreter(HF_LIFECYCLE_LANE, (first,))
        self.assertIs(caught.exception.reason, InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED)

    async def test_credential_in_source_never_reaches_model(self):
        factory = ScriptedFactory("first", reply("submit_interpretation", {"value": HF_VALUE}))
        with self.assertRaises(InterpreterUnavailable) as caught:
            await self.run_interpreter(HF_LIFECYCLE_LANE, (factory,), source=b"credential-0")
        self.assertIs(caught.exception.reason, InterpreterUnavailableReason.INPUT_REJECTED)
        self.assertEqual(factory.requests, [])

    def test_multiplicity_schema_does_not_force_source_substitution(self):
        schema = _value_schema_for(HF_LIFECYCLE_LANE)
        peak = schema["properties"]["model_input"]["properties"]["spectra"]["properties"]["1H"]
        multiplicity = peak["properties"]["peaks"]["items"]["properties"]["multiplicity"]
        self.assertEqual(multiplicity, {"type": "string"})
