"""Offline checks for the opt-in real-model qualification harness."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest

from nmrpeak_provider.interpreter import (
    InterpreterEndpoint,
    InterpreterTool,
    InterpreterToolInvocation,
    InterpreterTransportError,
    InterpreterTurn,
    interpret,
)
from nmrpeak_provider.input_interpreter import _InterpretationCapability
from nmrpeak_provider.lifecycle_lane import HF_LIFECYCLE_LANE
from nmrpeak_provider.product_input import ChfModelInput, HfModelInput
from nmrpeak_provider.text_provenance import UserProvidedText
from tests.model_behavior.run_interpreter import (
    _load_corpus,
    _observe,
    _select_evaluations,
)


def _turn(name: InterpreterTool, arguments: object) -> InterpreterTurn:
    return InterpreterTurn(
        assistant_message={"role": "assistant", "content": None},
        invocation=InterpreterToolInvocation(name.value, arguments),
        tool_call_ids=("call-1",),
    )


def _fixture_variant(scenario_id: str, lane: str) -> dict[str, object]:
    manifest = json.loads(
        (Path(__file__).with_name("fixtures") / "interpreter_cases.json")
        .read_text(encoding="utf-8")
    )
    scenario = next(item for item in manifest["scenarios"] if item["id"] == scenario_id)
    return scenario["variants"][lane]


class ModelBehaviorHarnessTests(unittest.TestCase):
    def test_corpus_selects_both_lanes_and_validates_expected_values(self) -> None:
        corpus = _load_corpus()
        self.assertEqual(len(_select_evaluations(corpus, "hf", case_id=None, mode="all")), 4)
        self.assertEqual(len(_select_evaluations(corpus, "chf", case_id=None, mode="all")), 4)

    def test_forced_repair_uses_production_constructor_without_raw_output(self) -> None:
        corpus = _load_corpus()
        for lane in ("hf", "chf"):
            with self.subTest(lane=lane):
                evaluation = _select_evaluations(
                    corpus, lane, case_id="ethanol_point", mode="forced_repair"
                )[0]
                expected = evaluation.variant.expected
                if type(expected) not in {HfModelInput, ChfModelInput}:
                    self.fail("fixture did not construct a product model input")
                # The fixture manifest is the source of the protocol value;
                # this local test checks the repair machinery, not a model.
                value = _fixture_variant("ethanol_point", lane)["expected_interpretation"]

                async def call(_prompt: object) -> InterpreterTurn:
                    return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

                observation = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake", call),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertTrue(observation.passed, observation.reason)
                self.assertEqual(observation.turns, 2)
                self.assertEqual(observation.injected_rejections, 1)
                self.assertEqual(observation.model_outputs, ())

    def test_missing_carbon_report_stays_a_report(self) -> None:
        evaluation = _select_evaluations(
            _load_corpus(), "chf", case_id="missing_carbon", mode="normal"
        )[0]

        async def call(_prompt: object) -> InterpreterTurn:
            return _turn(
                InterpreterTool.REPORT_INPUT_PROBLEM,
                {"message": "The required 13C carbon peak list is missing."},
            )

        observation = asyncio.run(_observe(
            endpoint=InterpreterEndpoint("fake", call),
            evaluation=evaluation,
            show_model_output=False,
            interpretation_timeout_seconds=3,
        ))
        self.assertTrue(observation.passed, observation.reason)
        self.assertEqual(observation.action, "report_input_problem")

    def test_unsupported_source_value_rejects_in_production_constructor_each_turn(self) -> None:
        for lane in ("hf", "chf"):
            with self.subTest(lane=lane):
                evaluation = _select_evaluations(
                    _load_corpus(), lane, case_id="unsupported_multiplicity", mode="normal"
                )[0]
                value = _fixture_variant("unsupported_multiplicity", lane)["expected_candidate"]

                async def call(_prompt: object) -> InterpreterTurn:
                    return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

                async def once():
                    return await _observe(
                        endpoint=InterpreterEndpoint("fake", call), evaluation=evaluation,
                        show_model_output=False, interpretation_timeout_seconds=3,
                    )

                first, second = asyncio.run(_repeat_twice(once))
                for observation in (first, second):
                    self.assertTrue(observation.passed, observation.reason)
                    self.assertEqual(observation.action, "candidate_construction_exhausted")
                    self.assertEqual(observation.constructor_rejections, 3)
                    self.assertEqual(observation.issue_reason, "unsupported_multiplicity")
                    self.assertEqual(observation.turns, 3)
                    self.assertEqual(observation.model_outputs, ())

    def test_unsupported_source_report_is_valid_but_substitution_is_not(self) -> None:
        for lane in ("hf", "chf"):
            with self.subTest(lane=lane):
                evaluation = _select_evaluations(
                    _load_corpus(), lane, case_id="unsupported_multiplicity", mode="normal"
                )[0]

                async def reported(_prompt: object) -> InterpreterTurn:
                    return _turn(
                        InterpreterTool.REPORT_INPUT_PROBLEM,
                        {"message": "The source reports multiplicity xy, which this model cannot represent."},
                    )

                report = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-report", reported),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(report.passed)
                self.assertEqual(report.reason, "unsupported_report_requires_review")
                self.assertEqual(report.action, "report_input_problem")
                self.assertIsNone(report.reported_message)

                captured_report = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-captured-report", reported),
                    evaluation=evaluation,
                    show_model_output=True,
                    interpretation_timeout_seconds=3,
                ))
                self.assertEqual(
                    captured_report.reported_message,
                    "The source reports multiplicity xy, which this model cannot represent.",
                )
                self.assertEqual(captured_report.reason, "unsupported_report_requires_review")

                async def alternate_report(_prompt: object) -> InterpreterTurn:
                    return _turn(
                        InterpreterTool.REPORT_INPUT_PROBLEM,
                        {"message": "The source gives multiplicity xy, which is not a recognized label."},
                    )

                alternate = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-alternate-report", alternate_report),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(alternate.passed)
                self.assertEqual(alternate.reason, "unsupported_report_requires_review")

                async def live_wording(_prompt: object) -> InterpreterTurn:
                    return _turn(
                        InterpreterTool.REPORT_INPUT_PROBLEM,
                        {"message": "The source reports multiplicity xy, but that label cannot be represented here."},
                    )

                observed_wording = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-live-wording", live_wording),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(observed_wording.passed)
                self.assertEqual(observed_wording.reason, "unsupported_report_requires_review")

                async def corrective_wording(_prompt: object) -> InterpreterTurn:
                    return _turn(
                        InterpreterTool.REPORT_INPUT_PROBLEM,
                        {"message": "The source gives multiplicity xy; please provide a multiplicity label this product can represent in a new Job."},
                    )

                correction = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-corrective-wording", corrective_wording),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(correction.passed)
                self.assertEqual(correction.reason, "unsupported_report_requires_review")

                async def contradictory_correction(_prompt: object) -> InterpreterTurn:
                    return _turn(
                        InterpreterTool.REPORT_INPUT_PROBLEM,
                        {"message": "The source reports multiplicity xy, which this product supports. Please provide a multiplicity label this product can represent."},
                    )

                contradiction = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-contradiction", contradictory_correction),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(contradiction.passed)
                self.assertEqual(contradiction.reason, "unsupported_value_not_explained")

                async def falsely_accepted(_prompt: object) -> InterpreterTurn:
                    return _turn(
                        InterpreterTool.REPORT_INPUT_PROBLEM,
                        {"message": "xy is the standard triplet label, so the input is fine."},
                    )

                false_report = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-false-report", falsely_accepted),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(false_report.passed)
                self.assertEqual(false_report.reason, "unsupported_value_not_explained")

                async def negated(_prompt: object) -> InterpreterTurn:
                    return _turn(
                        InterpreterTool.REPORT_INPUT_PROBLEM,
                        {"message": "xy is not unsupported; it is a standard triplet and the input is fine."},
                    )

                negated_report = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-negated-report", negated),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(negated_report.passed)
                self.assertEqual(negated_report.reason, "unsupported_value_not_explained")

                substituted = _fixture_variant("ethanol_point", lane)["expected_interpretation"]

                async def changed(_prompt: object) -> InterpreterTurn:
                    return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": substituted})

                replacement = asyncio.run(_observe(
                    endpoint=InterpreterEndpoint("fake-changed", changed),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(replacement.passed)
                self.assertEqual(replacement.reason, "wrong_terminal_action")

    def test_operational_first_endpoint_falls_back_to_source_faithful_second(self) -> None:
        evaluation = _select_evaluations(
            _load_corpus(), "hf", case_id="ethanol_point", mode="normal"
        )[0]
        value = _fixture_variant("ethanol_point", "hf")["expected_interpretation"]

        async def broken(_prompt: object) -> InterpreterTurn:
            raise InterpreterTransportError("connect_failed")

        async def healthy(_prompt: object) -> InterpreterTurn:
            return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

        async def admit(candidate: object) -> object:
            return candidate

        result = asyncio.run(interpret(
            source_text=UserProvidedText(evaluation.variant.source_text),
            capability=_InterpretationCapability(HF_LIFECYCLE_LANE),
            endpoints=(
                InterpreterEndpoint("broken", broken),
                InterpreterEndpoint("healthy", healthy),
            ),
            interpretation_timeout_seconds=3,
            report_endpoint_failure=lambda _event: None,
            admit_interpretation=admit,
        ))
        self.assertEqual(result.attempted_configuration_ids, ("broken", "healthy"))
        self.assertEqual(result.configuration_id, "healthy")
        self.assertEqual(result.admitted, evaluation.variant.expected)


async def _repeat_twice(call):
    return await call(), await call()


if __name__ == "__main__":
    unittest.main()
