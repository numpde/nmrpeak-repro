"""Offline checks for the opt-in real-model qualification harness."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from provider_core.chat_errors import ChatFailed
from tests.fake_interpreter import fake_endpoint, turn as _turn

from nmrpeak_provider.interpreter import (
    InterpreterTool,
)
from nmrpeak_provider.product_input import ChfModelInput, HfModelInput
from tests.model_behavior.run_interpreter import (
    _LANES,
    _load_corpus,
    _observe,
    _select_evaluations,
)


def _fixture_variant(scenario_id: str, lane: str) -> dict[str, object]:
    manifest = json.loads(
        (Path(__file__).with_name("fixtures") / "interpreter_cases.json")
        .read_text(encoding="utf-8")
    )
    scenario = next(item for item in manifest["scenarios"] if item["id"] == scenario_id)
    return scenario["variants"][lane]


class ModelBehaviorHarnessTests(unittest.TestCase):
    def test_information_query_does_not_count_as_an_extra_proposal(self):
        evaluation = _select_evaluations(_load_corpus(), "hf", case_id="ethanol_point", mode="normal")[0]
        value = _fixture_variant("ethanol_point", "hf")["expected_interpretation"]
        replies = iter((_turn("describe_input_constraints", {"tool": "submit_interpretation"}),
                        _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})))

        async def call(_prompt):
            return next(replies)

        observed = asyncio.run(_observe(endpoint=fake_endpoint("fake", call, lane=_LANES["hf"]),
                                        evaluation=evaluation, show_model_output=False,
                                        interpretation_timeout_seconds=3))
        self.assertTrue(observed.passed, observed.reason)
        self.assertEqual(observed.turns, 2)

    def test_transient_transport_failure_then_success_is_not_scored_as_failure(self):
        evaluation = _select_evaluations(_load_corpus(), "hf", case_id="ethanol_point", mode="normal")[0]
        value = _fixture_variant("ethanol_point", "hf")["expected_interpretation"]
        calls = 0

        async def call(_prompt):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ChatFailed("http_status", "busy", response=SimpleNamespace(status=503, headers=()))
            return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

        observed = asyncio.run(_observe(endpoint=fake_endpoint("fake", call, lane=_LANES["hf"]),
                                        evaluation=evaluation, show_model_output=False,
                                        interpretation_timeout_seconds=3))
        self.assertTrue(observed.passed, observed.reason)
        self.assertEqual(calls, 2)
        self.assertEqual(observed.turns, 1)

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

                async def call(_prompt: object) -> dict[str, object]:
                    return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

                observation = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake", call, lane=_LANES[evaluation.variant.lane_name]),
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

        async def call(_prompt: object) -> dict[str, object]:
            return _turn(
                InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                {"message": "The required 13C carbon peak list is missing."},
            )

        observation = asyncio.run(_observe(
            endpoint=fake_endpoint("fake", call, lane=_LANES[evaluation.variant.lane_name]),
            evaluation=evaluation,
            show_model_output=False,
            interpretation_timeout_seconds=3,
        ))
        self.assertTrue(observation.passed, observation.reason)
        self.assertEqual(observation.action, "report_interpretation_problem")

    def test_unsupported_source_value_rejects_in_production_constructor_each_turn(self) -> None:
        for lane in ("hf", "chf"):
            with self.subTest(lane=lane):
                evaluation = _select_evaluations(
                    _load_corpus(), lane, case_id="unsupported_multiplicity", mode="normal"
                )[0]
                value = _fixture_variant("unsupported_multiplicity", lane)["expected_candidate"]

                async def call(_prompt: object) -> dict[str, object]:
                    return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": value})

                async def once():
                    return await _observe(
                        endpoint=fake_endpoint("fake", call, lane=_LANES[evaluation.variant.lane_name]), evaluation=evaluation,
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

                async def reported(_prompt: object) -> dict[str, object]:
                    return _turn(
                        InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                        {"message": "The source reports multiplicity xy, which this model cannot represent."},
                    )

                report = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-report", reported, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(report.passed)
                self.assertEqual(report.reason, "unsupported_report_requires_review")
                self.assertEqual(report.action, "report_interpretation_problem")
                self.assertIsNone(report.reported_message)

                captured_report = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-captured-report", reported, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=True,
                    interpretation_timeout_seconds=3,
                ))
                self.assertEqual(
                    captured_report.reported_message,
                    "The source reports multiplicity xy, which this model cannot represent.",
                )
                self.assertEqual(captured_report.reason, "unsupported_report_requires_review")

                async def alternate_report(_prompt: object) -> dict[str, object]:
                    return _turn(
                        InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                        {"message": "The source gives multiplicity xy, which is not a recognized label."},
                    )

                alternate = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-alternate-report", alternate_report, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(alternate.passed)
                self.assertEqual(alternate.reason, "unsupported_report_requires_review")

                async def live_wording(_prompt: object) -> dict[str, object]:
                    return _turn(
                        InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                        {"message": "The source reports multiplicity xy, but that label cannot be represented here."},
                    )

                observed_wording = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-live-wording", live_wording, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(observed_wording.passed)
                self.assertEqual(observed_wording.reason, "unsupported_report_requires_review")

                async def corrective_wording(_prompt: object) -> dict[str, object]:
                    return _turn(
                        InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                        {"message": "The source gives multiplicity xy; please provide a multiplicity label this product can represent in a new Job."},
                    )

                correction = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-corrective-wording", corrective_wording, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(correction.passed)
                self.assertEqual(correction.reason, "unsupported_report_requires_review")

                async def contradictory_correction(_prompt: object) -> dict[str, object]:
                    return _turn(
                        InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                        {"message": "The source reports multiplicity xy, which this product supports. Please provide a multiplicity label this product can represent."},
                    )

                contradiction = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-contradiction", contradictory_correction, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(contradiction.passed)
                self.assertEqual(contradiction.reason, "unsupported_value_not_explained")

                async def falsely_accepted(_prompt: object) -> dict[str, object]:
                    return _turn(
                        InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                        {"message": "xy is the standard triplet label, so the input is fine."},
                    )

                false_report = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-false-report", falsely_accepted, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(false_report.passed)
                self.assertEqual(false_report.reason, "unsupported_value_not_explained")

                async def negated(_prompt: object) -> dict[str, object]:
                    return _turn(
                        InterpreterTool.REPORT_INTERPRETATION_PROBLEM,
                        {"message": "xy is not unsupported; it is a standard triplet and the input is fine."},
                    )

                negated_report = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-negated-report", negated, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(negated_report.passed)
                self.assertEqual(negated_report.reason, "unsupported_value_not_explained")

                substituted = _fixture_variant("ethanol_point", lane)["expected_interpretation"]

                async def changed(_prompt: object) -> dict[str, object]:
                    return _turn(InterpreterTool.SUBMIT_INTERPRETATION, {"value": substituted})

                replacement = asyncio.run(_observe(
                    endpoint=fake_endpoint("fake-changed", changed, lane=_LANES[evaluation.variant.lane_name]),
                    evaluation=evaluation,
                    show_model_output=False,
                    interpretation_timeout_seconds=3,
                ))
                self.assertFalse(replacement.passed)
                self.assertEqual(replacement.reason, "wrong_terminal_action")



async def _repeat_twice(call):
    return await call(), await call()


if __name__ == "__main__":
    unittest.main()
