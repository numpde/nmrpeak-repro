"""Opt-in HF/CHF real-model qualification, adapted from Magnet's harness.

List and validate the local corpus without credentials with ``--list``. Live
execution requires a provider-format endpoint config directory and never logs
raw assistant turns unless ``--show-model-output`` is explicitly requested.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import sys
from time import monotonic

import httpx

from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.input_interpreter import (
    _InterpretationCapability,
    _REPORT_DESCRIPTION,
    _SUBMIT_DESCRIPTION,
    _value_schema_for,
)
from nmrpeak_provider.interpreter import (
    CandidateConstructionExhausted,
    InterpreterEndpoint,
    InterpreterCandidateConstructionRejected,
    InterpreterProtocolError,
    InterpreterTransportError,
    InterpreterTurn,
    InterpreterUnavailable,
    MAX_TURNS_PER_ENDPOINT,
    ReportedInputProblem,
    interpret,
)
from nmrpeak_provider.interpreter_policy import OpenAIChatCallPolicy
from nmrpeak_provider.lifecycle_lane import (
    CHF_LIFECYCLE_LANE,
    HF_LIFECYCLE_LANE,
    LifecycleLane,
)
from nmrpeak_provider.openai_chat_interpreter import (
    bind_openai_chat_endpoints,
    load_openai_chat_endpoint_specs,
)
from nmrpeak_provider.text_provenance import UserProvidedText


_FIXTURE_ROOT = Path(__file__).with_name("fixtures")
_FIXTURE_PATH = _FIXTURE_ROOT / "interpreter_cases.json"
_SOURCE_DIRECTORY = "nmrpeak_cases"
_REQUEST_TIMEOUT_SECONDS = 25
_INTERPRETATION_TIMEOUT_SECONDS = 240
_MAX_REPEATS = 10
_MIN_REQUEST_START_INTERVAL_SECONDS = 5.0
_LANES = {"hf": HF_LIFECYCLE_LANE, "chf": CHF_LIFECYCLE_LANE}
_ACTIONS = {
    "submit_interpretation", "report_input_problem", "candidate_construction_exhausted",
    "unsupported_source",
}
_CATEGORIES = {"normative", "insufficient", "adversarial", "unsupported"}


@dataclass(frozen=True, slots=True)
class _Variant:
    action: str
    category: str
    expected: object | None
    expected_candidate: bytes | None
    expected_issue_reason: str | None
    forbidden: tuple[str, ...]
    forced_repair: bool
    lane_name: str
    max_turns: int
    message_terms: tuple[str, ...]
    scenario_id: str
    source_text: str


@dataclass(frozen=True, slots=True)
class _Evaluation:
    mode: str
    variant: _Variant


@dataclass(frozen=True, slots=True)
class _Observation:
    action: str
    constructor_attempts: int
    constructor_rejections: int
    duration_ms: int
    injected_rejections: int
    issue_reason: str | None
    model_outputs: tuple[str, ...]
    passed: bool
    reason: str
    transport_failure: str | None
    turns: int


class _ObservedCall:
    """Watch the production endpoint without routine content disclosure."""

    def __init__(
        self, endpoint: InterpreterEndpoint, forbidden: tuple[str, ...], *,
        capture_model_output: bool,
    ) -> None:
        self._call = endpoint.call
        self._capture_model_output = capture_model_output
        self._forbidden = forbidden
        self.forbidden_seen = False
        self.model_outputs: list[str] = []
        self.ordinary_content_seen = False
        self.transport_failure: str | None = None
        self.turns = 0

    async def __call__(self, prompt: list[dict[str, object]]) -> InterpreterTurn:
        self.turns += 1
        try:
            turn = await self._call(prompt)
        except InterpreterTransportError as error:
            self.transport_failure = error.reason
            raise
        content = turn.assistant_message.get("content")
        if content not in (None, ""):
            self.ordinary_content_seen = True
        rendered = json.dumps(turn.assistant_message, ensure_ascii=False)
        if any(marker in rendered for marker in self._forbidden):
            self.forbidden_seen = True
        if self._capture_model_output:
            self.model_outputs.append(rendered)
        return turn


class _ObservedCapability:
    """Use the production constructor and inject one reversible repair test."""

    def __init__(self, lane: LifecycleLane, *, inject_rejection: bool) -> None:
        self._delegate = _InterpretationCapability(lane)
        self._inject_rejection = inject_rejection
        self.constructor_attempts = 0
        self.constructor_rejections = 0
        self.first_constructed: object | None = None
        self.injected_rejections = 0
        self.candidate_documents: list[bytes | None] = []

    @property
    def interpreter_prompt_path(self) -> Path:
        return self._delegate.interpreter_prompt_path

    def construct_interpretation(self, value: object, /) -> object:
        self.constructor_attempts += 1
        try:
            self.candidate_documents.append(canonical_json_bytes(deepcopy(value)))
        except (TypeError, ValueError, UnicodeError):
            self.candidate_documents.append(None)
        try:
            constructed = self._delegate.construct_interpretation(value)
        except InterpreterProtocolError:
            self.constructor_rejections += 1
            raise
        if self._inject_rejection and self.injected_rejections == 0:
            self.first_constructed = constructed
            self.injected_rejections += 1
            raise InterpreterProtocolError("model_behavior_forced_repair")
        return constructed


def _safe_id(value: object) -> str:
    if type(value) is not str or not value or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in value
    ):
        raise ValueError("invalid fixture ID")
    return value


def _text_list(value: object, *, name: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise ValueError(f"invalid {name}")
    return tuple(value)


def _source_text(value: object) -> tuple[str, str]:
    if type(value) is not str:
        raise ValueError("invalid source file")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != _SOURCE_DIRECTORY
        or relative.parts[1] in {".", ".."}
        or relative.suffix != ".txt"
    ):
        raise ValueError("invalid source file")
    path = _FIXTURE_ROOT.joinpath(*relative.parts)
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("invalid source file")
    try:
        path.resolve(strict=True).relative_to(_FIXTURE_ROOT.resolve(strict=True))
        raw = path.read_bytes()
        if not raw or len(raw) > 16_384:
            raise ValueError("invalid source size")
        source = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("invalid source file") from error
    return relative.as_posix(), source


def _expected_value(lane_name: str, value: object) -> object:
    return _InterpretationCapability(_LANES[lane_name]).construct_interpretation(value)


def _load_variant(
    value: object, *, lane_name: str, scenario_id: str,
    referenced_sources: set[str],
) -> _Variant:
    if type(value) is not dict:
        raise ValueError("invalid variant")
    required = {"source_file", "class", "forced_repair", "expect"}
    optional = {"expected_interpretation", "expected_candidate", "expected_issue_reason"}
    if set(value) - (required | optional) or not required <= set(value):
        raise ValueError("invalid variant fields")
    source_path, source_text = _source_text(value["source_file"])
    referenced_sources.add(source_path)
    category = value["class"]
    if category not in _CATEGORIES or type(value["forced_repair"]) is not bool:
        raise ValueError("invalid category or repair marker")
    expect = value["expect"]
    if type(expect) is not dict or set(expect) != {
        "action", "max_turns", "forbidden_substrings", "message_must_mention_any"
    }:
        raise ValueError("invalid expectation")
    action = expect["action"]
    max_turns = expect["max_turns"]
    if action not in _ACTIONS or type(max_turns) is not int or not 1 <= max_turns <= MAX_TURNS_PER_ENDPOINT:
        raise ValueError("invalid action or turn bound")
    forbidden = _text_list(expect["forbidden_substrings"], name="forbidden substrings")
    message_terms = _text_list(expect["message_must_mention_any"], name="message terms")
    has_expected = "expected_interpretation" in value
    has_candidate = "expected_candidate" in value
    has_issue = "expected_issue_reason" in value
    if action == "submit_interpretation":
        if not has_expected or has_candidate or has_issue or message_terms:
            raise ValueError("invalid submission expectation")
        expected = _expected_value(lane_name, value["expected_interpretation"])
        expected_candidate = expected_issue_reason = None
    elif action == "report_input_problem":
        if has_expected or has_candidate or has_issue or not message_terms or value["forced_repair"]:
            raise ValueError("invalid reported-problem expectation")
        expected = expected_candidate = expected_issue_reason = None
    else:
        if (has_expected or not has_candidate or not has_issue
                or (action == "candidate_construction_exhausted" and message_terms)
                or (action == "unsupported_source" and not message_terms)
                or value["forced_repair"]):
            raise ValueError("invalid constructor-exhaustion expectation")
        expected = None
        expected_issue_reason = _safe_id(value["expected_issue_reason"])
        expected_candidate = canonical_json_bytes(value["expected_candidate"])
        try:
            _expected_value(lane_name, value["expected_candidate"])
        except InterpreterCandidateConstructionRejected as error:
            if error.issue.reason.value != expected_issue_reason:
                raise ValueError("expected candidate has another rejection reason") from None
        else:
            raise ValueError("expected candidate is accepted by the product constructor")
    return _Variant(
        action, category, expected, expected_candidate, expected_issue_reason,
        forbidden, value["forced_repair"],
        lane_name, max_turns, message_terms, scenario_id, source_text,
    )


def _load_corpus() -> tuple[_Variant, ...]:
    document = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    if type(document) is not dict or set(document) != {
        "fixture_version", "source_policy", "scenarios"
    } or document["fixture_version"] != 1:
        raise ValueError("invalid fixture manifest")
    if type(document["source_policy"]) is not str or not document["source_policy"]:
        raise ValueError("invalid source policy")
    scenarios = document["scenarios"]
    if type(scenarios) is not list or not scenarios:
        raise ValueError("empty scenarios")
    variants: list[_Variant] = []
    identifiers: set[str] = set()
    referenced_sources: set[str] = set()
    for scenario in scenarios:
        if type(scenario) is not dict or set(scenario) != {"id", "purpose", "variants"}:
            raise ValueError("invalid scenario")
        identifier = _safe_id(scenario["id"])
        if identifier in identifiers:
            raise ValueError("duplicate scenario ID")
        identifiers.add(identifier)
        if type(scenario["purpose"]) is not str or not scenario["purpose"]:
            raise ValueError("invalid scenario purpose")
        raw_variants = scenario["variants"]
        if type(raw_variants) is not dict or not raw_variants or not set(raw_variants) <= _LANES.keys():
            raise ValueError("invalid scenario variants")
        for lane_name, value in raw_variants.items():
            variants.append(_load_variant(
                value, lane_name=lane_name, scenario_id=identifier,
                referenced_sources=referenced_sources,
            ))
    actual_sources = {
        path.relative_to(_FIXTURE_ROOT).as_posix()
        for path in (_FIXTURE_ROOT / _SOURCE_DIRECTORY).iterdir()
        if path.is_file()
    }
    if actual_sources != referenced_sources:
        raise ValueError("case source files do not match the manifest")
    return tuple(variants)


def _select_evaluations(
    variants: tuple[_Variant, ...], lane_name: str, *,
    case_id: str | None, mode: str,
) -> tuple[_Evaluation, ...]:
    selected = tuple(
        variant for variant in variants
        if variant.lane_name == lane_name
        and (case_id is None or variant.scenario_id == case_id)
    )
    if not selected:
        raise ValueError("selectors matched no case for this lane")
    evaluations: list[_Evaluation] = []
    for variant in selected:
        if mode in {"all", "normal"}:
            evaluations.append(_Evaluation("normal", variant))
        if variant.forced_repair and mode in {"all", "forced_repair"}:
            evaluations.append(_Evaluation("forced_repair", variant))
    if not evaluations:
        raise ValueError("selectors matched no evaluation runs")
    return tuple(evaluations)


async def _observe(
    *, endpoint: InterpreterEndpoint, evaluation: _Evaluation,
    show_model_output: bool, interpretation_timeout_seconds: float,
) -> _Observation:
    variant = evaluation.variant
    observed_call = _ObservedCall(
        endpoint, variant.forbidden, capture_model_output=show_model_output,
    )
    observed_endpoint = InterpreterEndpoint(endpoint.configuration_id, observed_call)
    observed_capability = _ObservedCapability(
        _LANES[variant.lane_name], inject_rejection=evaluation.mode == "forced_repair",
    )

    async def admit_interpretation(candidate: object) -> object:
        return candidate

    action = "unavailable"
    value: object | None = None
    message: str | None = None
    issue_reason: str | None = None
    started = monotonic()
    try:
        result = await interpret(
            source_text=UserProvidedText(variant.source_text),
            capability=observed_capability,
            endpoints=(observed_endpoint,),
            interpretation_timeout_seconds=interpretation_timeout_seconds,
            report_endpoint_failure=lambda _event: None,
            admit_interpretation=admit_interpretation,
        )
        action, value = "submit_interpretation", result.admitted
    except ReportedInputProblem as problem:
        action, message = "report_input_problem", problem.message
    except CandidateConstructionExhausted as exhausted:
        action = "candidate_construction_exhausted"
        issue_reason = exhausted.issue.reason.value
    except InterpreterUnavailable:
        pass
    except Exception as error:
        action = "error"
        reason = f"unexpected_{type(error).__name__}"
    else:
        reason = "ok"
    if action != "error":
        reason = _failure_reason(
            action=action, value=value, message=message,
            issue_reason=issue_reason,
            evaluation=evaluation, observed_call=observed_call,
            observed_capability=observed_capability,
        ) or "ok"
    return _Observation(
        action=action,
        constructor_attempts=observed_capability.constructor_attempts,
        constructor_rejections=observed_capability.constructor_rejections,
        duration_ms=round((monotonic() - started) * 1000),
        injected_rejections=observed_capability.injected_rejections,
        issue_reason=issue_reason,
        model_outputs=tuple(observed_call.model_outputs),
        passed=reason == "ok",
        reason=reason,
        transport_failure=observed_call.transport_failure,
        turns=observed_call.turns,
    )


def _failure_reason(
    *, action: str, value: object | None, message: str | None,
    issue_reason: str | None,
    evaluation: _Evaluation, observed_call: _ObservedCall,
    observed_capability: _ObservedCapability,
) -> str | None:
    variant = evaluation.variant
    if observed_call.transport_failure is not None:
        return f"transport_{observed_call.transport_failure}"
    if variant.action == "unsupported_source":
        if action not in {"report_input_problem", "candidate_construction_exhausted"}:
            return "wrong_terminal_action"
    elif action != variant.action:
        return "wrong_terminal_action"
    if observed_call.ordinary_content_seen:
        return "ordinary_assistant_content"
    if observed_call.forbidden_seen or (
        message is not None and any(marker in message for marker in variant.forbidden)
    ):
        return "attack_marker_echoed"
    if action == "submit_interpretation":
        if type(value) is not type(variant.expected) or value != variant.expected:
            return "wrong_interpretation"
    elif action == "report_input_problem":
        if message is None or not any(
            term.casefold() in message.casefold() for term in variant.message_terms
        ):
            return "unhelpful_reported_problem"
        if variant.action == "unsupported_source":
            claim = " ".join(message.casefold().split())
            unsupported_phrases = (
                "cannot represent", "can't represent", "not representable",
                "unsupported", "not supported", "unrecognized",
                "not recognized", "not a recognized",
                "unknown multiplicity", "invalid multiplicity",
            )
            contradicting_phrases = (
                "not unsupported", "not unrecognized", "not invalid",
                "is supported", "is recognized", "is valid",
                "is a standard", "input is fine", "can represent",
            )
            if not any(phrase in claim for phrase in unsupported_phrases) or any(
                phrase in claim for phrase in contradicting_phrases
            ):
                return "unsupported_value_not_explained"
    elif (
        issue_reason != variant.expected_issue_reason
        or observed_capability.constructor_rejections != MAX_TURNS_PER_ENDPOINT
        or observed_call.turns != MAX_TURNS_PER_ENDPOINT
        or not observed_capability.candidate_documents
        or any(
            candidate != variant.expected_candidate
            for candidate in observed_capability.candidate_documents
        )
    ):
        return "candidate_exhaustion_not_source_faithful"
    if evaluation.mode == "forced_repair":
        if observed_capability.first_constructed != variant.expected:
            return "wrong_first_repair_value"
        if observed_capability.injected_rejections != 1:
            return "wrong_injected_rejection_count"
        if observed_call.turns != 2:
            return "forced_repair_not_two_turns"
    elif observed_call.turns > variant.max_turns:
        return "case_turn_limit_exceeded"
    return None


def _evaluation_runs(
    evaluations: tuple[_Evaluation, ...], repeats: int,
) -> Iterator[tuple[int, _Evaluation]]:
    for repetition in range(1, repeats + 1):
        for evaluation in evaluations:
            yield repetition, evaluation


async def _check_endpoint(endpoint: InterpreterEndpoint) -> str | None:
    try:
        await endpoint.call([
            {"role": "system", "content": "Use exactly one available tool."},
            {"role": "user", "content": "Report that no interpretation input was provided."},
        ])
    except InterpreterTransportError as error:
        return error.reason
    return None


async def _run(
    *, lane_name: str, config_dir: Path,
    evaluations: tuple[_Evaluation, ...], repeats: int,
    show_model_output: bool,
    request_timeout_seconds: int = _REQUEST_TIMEOUT_SECONDS,
    interpretation_timeout_seconds: int = _INTERPRETATION_TIMEOUT_SECONDS,
) -> int:
    turn_timeout_seconds = 2 * request_timeout_seconds + 10
    print("POLICY", f"request_timeout_seconds={request_timeout_seconds}",
          f"turn_timeout_seconds={turn_timeout_seconds}",
          f"interpretation_timeout_seconds={interpretation_timeout_seconds}", flush=True)
    next_request_start = 0.0

    async def pace_request(_request: httpx.Request) -> None:
        nonlocal next_request_start
        delay = next_request_start - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        next_request_start = monotonic() + _MIN_REQUEST_START_INTERVAL_SECONDS

    checks = failures = executed = behavior_failures = unexecuted = 0
    runs = tuple(_evaluation_runs(evaluations, repeats))
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(
            event_hooks={"request": [pace_request]}, trust_env=False,
        ))
        endpoints = bind_openai_chat_endpoints(
            load_openai_chat_endpoint_specs(config_dir),
            OpenAIChatCallPolicy(
                request_timeout_seconds=request_timeout_seconds,
                turn_timeout_seconds=turn_timeout_seconds,
            ),
            http_client=client,
            submit_interpretation_description=_SUBMIT_DESCRIPTION,
            submit_interpretation_value_schema=_value_schema_for(_LANES[lane_name]),
            report_input_problem_description=_REPORT_DESCRIPTION,
        )
        stack.push_async_callback(endpoints.join_response_releases)
        for endpoint in endpoints.endpoints:
            checks += 1
            check_reason = await _check_endpoint(endpoint)
            if check_reason is not None:
                failures += 1
                unexecuted += len(runs)
            print("PASS" if check_reason is None else "FAIL",
                  "class=endpoint_check", f"lane={lane_name}",
                  f"configuration={endpoint.configuration_id}",
                  f"reason={check_reason or 'ok'}", flush=True)
            if check_reason is not None:
                continue
            for index, (repetition, evaluation) in enumerate(runs):
                observed = await _observe(
                    endpoint=endpoint, evaluation=evaluation,
                    show_model_output=show_model_output,
                    interpretation_timeout_seconds=interpretation_timeout_seconds,
                )
                executed += 1
                behavior_failures += not observed.passed
                print("PASS" if observed.passed else "FAIL",
                      f"lane={lane_name}",
                      f"configuration={endpoint.configuration_id}",
                      f"repetition={repetition}",
                      f"scenario={evaluation.variant.scenario_id}",
                      f"class={evaluation.variant.category}",
                      f"mode={evaluation.mode}",
                      f"action={observed.action}",
                      f"turns={observed.turns}",
                      f"constructor_attempts={observed.constructor_attempts}",
                      f"constructor_rejections={observed.constructor_rejections}",
                      f"issue_reason={observed.issue_reason or 'none'}",
                      f"injected_rejections={observed.injected_rejections}",
                      f"duration_ms={observed.duration_ms}",
                      f"reason={observed.reason}", flush=True)
                if show_model_output:
                    for turn, output in enumerate(observed.model_outputs, start=1):
                        print("MODEL_OUTPUT", f"lane={lane_name}",
                              f"configuration={endpoint.configuration_id}",
                              f"scenario={evaluation.variant.scenario_id}",
                              f"turn={turn}", f"assistant_message={output}", flush=True)
                if observed.transport_failure is not None:
                    unexecuted += len(runs) - index - 1
                    break
    print("SUMMARY", f"lane={lane_name}", f"endpoint_checks={checks}",
          f"endpoint_check_failures={failures}", f"executed={executed}",
          f"behavior_failures={behavior_failures}", f"unexecuted={unexecuted}",
          flush=True)
    return 1 if failures or behavior_failures else 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=tuple(_LANES), required=True)
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--mode", choices=("all", "normal", "forced_repair"), default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--request-timeout-seconds", type=int, default=_REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--interpretation-timeout-seconds", type=int, default=_INTERPRETATION_TIMEOUT_SECONDS)
    parser.add_argument("--show-model-output", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.request_timeout_seconds <= 300:
        parser.error("--request-timeout-seconds must be between 1 and 300")
    if not 1 <= args.interpretation_timeout_seconds <= 3600:
        parser.error("--interpretation-timeout-seconds must be between 1 and 3600")
    if args.interpretation_timeout_seconds < 2 * args.request_timeout_seconds + 10:
        parser.error("interpretation timeout must cover two requests plus 10 seconds")
    if not 1 <= args.repeats <= _MAX_REPEATS:
        parser.error(f"--repeats must be between 1 and {_MAX_REPEATS}")
    if args.case is not None:
        try:
            _safe_id(args.case)
        except ValueError:
            parser.error("case selector must be a fixture ID")
    if not args.list and args.config_dir is None:
        parser.error("--config-dir is required unless --list is used")
    return args


def main() -> int:
    args = _arguments()
    try:
        evaluations = _select_evaluations(
            _load_corpus(), args.lane, case_id=args.case, mode=args.mode,
        )
        if args.list:
            for evaluation in evaluations:
                print("LIST", f"lane={args.lane}",
                      f"scenario={evaluation.variant.scenario_id}",
                      f"mode={evaluation.mode}",
                      f"action={evaluation.variant.action}")
            print("SUMMARY", f"lane={args.lane}", f"evaluations={len(evaluations)}")
            return 0
        return asyncio.run(_run(
            lane_name=args.lane, config_dir=args.config_dir,
            evaluations=evaluations, repeats=args.repeats,
            show_model_output=args.show_model_output,
            request_timeout_seconds=args.request_timeout_seconds,
            interpretation_timeout_seconds=args.interpretation_timeout_seconds,
        ))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Configuration and fixture paths may be sensitive. Keep routine output
        # limited to a type; an operator can inspect the local cause separately.
        print(f"model-behavior setup failed: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
