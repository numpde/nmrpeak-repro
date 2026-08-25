"""Run generic tool interpretation across an ordered set of model endpoints.

This module owns prompt assembly, bounded protocol repair, ordered fallback,
and dispatch of the two generic tools. An injected capability supplies the
analysis-specific prompt and typed constructor; callers own outcome policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from pathlib import Path
import re
from typing import Generic, Protocol, TypeVar

import nmrpeak_provider.provider_events as _events
from nmrpeak_provider.failure_message import is_failure_message
from nmrpeak_provider.interpreter_policy import MAX_INTERPRETER_ENDPOINTS
from nmrpeak_provider.text_provenance import (
    ModelGeneratedText,
    UserProvidedText,
)


Candidate = TypeVar("Candidate")
Admitted = TypeVar("Admitted")
MAX_TURNS_PER_ENDPOINT = 3
MAX_INTERPRETER_CONFIGURATION_ID_BYTES = 128
_MAX_PROMPT_BYTES = 64 * 1024
_CONFIGURATION_ID = re.compile(
    rf"[a-z0-9][a-z0-9._-]{{0,{MAX_INTERPRETER_CONFIGURATION_ID_BYTES - 1}}}",
    re.ASCII,
)
_PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_SYSTEM_PROMPT_PATH = _PROMPT_DIRECTORY / "interpreter.md"
_CORRECTION_PROMPT_PATH = _PROMPT_DIRECTORY / "protocol_correction.md"

PromptMessage = dict[str, object]
InterpreterPrompt = list[PromptMessage]


class InterpreterProtocolError(ValueError):
    """An assistant turn did not satisfy the generic tool contract."""

    def __init__(self, reason: str) -> None:
        self.reason = _require_failure_reason(reason)
        super().__init__(self.reason)


class InterpreterTransportError(RuntimeError):
    """One endpoint failed with a content-free operational classification."""

    def __init__(self, reason: str = "unclassified") -> None:
        self.reason = _require_failure_reason(reason)
        super().__init__(self.reason)


class InterpreterUnavailableReason(StrEnum):
    """The factual cause known at the interpreter boundary."""

    PROMPT_UNAVAILABLE = "prompt_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    ENDPOINTS_EXHAUSTED = "endpoints_exhausted"


class InterpreterUnavailable(RuntimeError):
    """Prompts were unavailable, the aggregate deadline expired,
    or no endpoint produced a trustworthy answer."""

    def __init__(
        self,
        reason: InterpreterUnavailableReason,
        attempted_configuration_ids: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.attempted_configuration_ids = attempted_configuration_ids
        attempted = ",".join(attempted_configuration_ids) or "none"
        super().__init__(
            f"{reason.value}; attempted interpreter endpoints: {attempted}"
        )


class ReportedInputProblem(ValueError):
    """The assistant completed interpretation by explaining a caller problem."""

    def __init__(
        self,
        message: ModelGeneratedText,
    ) -> None:
        self.message = message
        # The public message may quote caller input. Keep it available only for
        # deliberate projection, never in the incidental exception string.
        super().__init__("reported_input_problem")


class InterpretationCandidateRejected(ValueError):
    """An analysis-specific admission boundary rejected one candidate."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class InterpretationRejected(ValueError):
    """Every configured endpoint produced a runner-rejected candidate."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class InterpreterTool(StrEnum):
    """The complete generic tool-name vocabulary exposed to every endpoint."""

    SUBMIT_INTERPRETATION = "submit_interpretation"
    REPORT_INPUT_PROBLEM = "report_input_problem"


@dataclass(frozen=True, slots=True)
class InterpreterToolInvocation:
    """One transport-parsed tool invocation; generic dispatch validates it."""

    name: object
    arguments: object


@dataclass(frozen=True, slots=True)
class InterpreterTurn:
    """One assistant message and its best-effort generic tool projection.

    The normalized assistant message is retained only so a rejected invocation
    can be followed by the OpenAI-required tool results and a correction. A
    non-``None`` ``tool_call_ids`` tuple marks a message that can be continued
    faithfully; malformed tool-call carriers set it to ``None``.
    """

    assistant_message: PromptMessage
    invocation: InterpreterToolInvocation | None
    tool_call_ids: tuple[str, ...] | None

    def __post_init__(self) -> None:
        if type(self.assistant_message) is not dict:
            raise TypeError("assistant_message must be an object")
        if (
            self.invocation is not None
            and type(self.invocation) is not InterpreterToolInvocation
        ):
            raise TypeError("invocation must be an InterpreterToolInvocation or None")
        if self.tool_call_ids is not None and (
            type(self.tool_call_ids) is not tuple
            or any(
                type(call_id) is not str or not call_id
                for call_id in self.tool_call_ids
            )
        ):
            raise TypeError("tool_call_ids must be a tuple of non-empty text")


InterpreterCall = Callable[[InterpreterPrompt], Awaitable[InterpreterTurn]]
InterpretationAdmission = Callable[[Candidate], Awaitable[Admitted]]


@dataclass(frozen=True, slots=True)
class InterpreterEndpoint:
    """One configured interpreter call in fallback order."""

    configuration_id: str
    call: InterpreterCall

    def __post_init__(self) -> None:
        require_interpreter_configuration_id(self.configuration_id)
        if not callable(self.call):
            raise TypeError("call must be callable")


def require_interpreter_configuration_id(value: object, /) -> None:
    """Admit an identifier before it reaches fallback events or logs."""

    if type(value) is not str or _CONFIGURATION_ID.fullmatch(value) is None:
        raise TypeError("configuration_id must be a bounded safe identifier")


@dataclass(frozen=True, slots=True)
class InterpretationResult(Generic[Admitted]):
    """An admitted interpretation plus safe endpoint-selection provenance."""

    admitted: Admitted
    configuration_id: str
    attempted_configuration_ids: tuple[str, ...]


class InterpretationCapability(Protocol[Candidate]):
    """Supply one analysis-specific prompt and construct its typed value.

    A constructor's ``InterpreterProtocolError`` is protocol evidence that may
    trigger a repair turn; it is not automatically a caller-input failure.
    """

    @property
    def interpreter_prompt_path(self) -> Path: ...

    def construct_interpretation(self, value: object, /) -> Candidate: ...


async def interpret(
    *,
    source_text: UserProvidedText,
    capability: InterpretationCapability[Candidate],
    endpoints: tuple[InterpreterEndpoint, ...],
    interpretation_timeout_seconds: float,
    report_endpoint_failure: Callable[[_events.InterpreterEndpointFailed], None],
    admit_interpretation: InterpretationAdmission[Candidate, Admitted],
) -> InterpretationResult[Admitted]:
    """Return one admitted interpretation using bounded repair and fallback.

    Prompt files are reread for every operation. Endpoints are tried in order
    under one aggregate deadline, each with bounded protocol-repair turns.
    Assistant-reported input problems remain distinct from local or endpoint
    unavailability. Injecting the endpoint-failure destination keeps policy
    selection outside generic interpretation and lets the caller reuse one
    logging-policy snapshot across related events.
    """

    _require_endpoints(endpoints)
    try:
        base_prompt = [
            {"role": "system", "content": _read_prompt(_SYSTEM_PROMPT_PATH)},
            {
                "role": "user",
                "content": _read_prompt(capability.interpreter_prompt_path),
            },
            {"role": "user", "content": source_text},
        ]
        correction = _read_prompt(_CORRECTION_PROMPT_PATH)
    except (OSError, UnicodeError, ValueError) as error:
        # Prompt files are runtime dependencies. A broken hot reload is an
        # unavailable interpreter, not evidence that caller input was bad.
        raise InterpreterUnavailable(
            InterpreterUnavailableReason.PROMPT_UNAVAILABLE
        ) from error

    attempted: list[str] = []
    endpoint_failures: list[BaseException] = []
    all_endpoints_rejected_candidate = True
    last_rejection: str | None = None
    deadline = asyncio.timeout(interpretation_timeout_seconds)
    try:
        # This is the caller-visible operation bound. Endpoint adapters retain
        # their tighter per-turn bounds, while repair and fallback must share
        # one finite budget rather than multiplying it.
        async with deadline:
            for endpoint in endpoints:
                attempted.append(endpoint.configuration_id)
                prompt = list(base_prompt)

                for turn in count(1):
                    try:
                        # Endpoint adapters do not own generic prompt history.
                        # Isolate the bounded JSON-like message tree so a failed
                        # adapter cannot contaminate repair or fallback.
                        assistant = await endpoint.call(deepcopy(prompt))
                    except InterpreterTransportError as error:
                        all_endpoints_rejected_candidate = False
                        endpoint_failures.append(error)
                        _report_endpoint_failure(
                            report_endpoint_failure,
                            endpoint.configuration_id,
                            failure_kind="transport",
                            failure_reason=error.reason,
                        )
                        break
                    if type(assistant) is not InterpreterTurn:
                        all_endpoints_rejected_candidate = False
                        error = InterpreterProtocolError("invalid_turn_type")
                        endpoint_failures.append(error)
                        _report_endpoint_failure(
                            report_endpoint_failure,
                            endpoint.configuration_id,
                            failure_kind="protocol",
                            failure_reason=error.reason,
                        )
                        break

                    has_repair_context = assistant.tool_call_ids is not None
                    repair_exhausted = (
                        turn >= MAX_TURNS_PER_ENDPOINT and has_repair_context
                    )
                    try:
                        candidate = _dispatch_turn(assistant, capability=capability)
                        admitted = await admit_interpretation(candidate)
                    except InterpretationCandidateRejected as rejection:
                        last_rejection = rejection.message
                        _report_endpoint_failure(
                            report_endpoint_failure,
                            endpoint.configuration_id,
                            failure_kind="admission",
                            failure_reason=rejection.message,
                        )
                        break
                    except InterpreterProtocolError as error:
                        if repair_exhausted or not has_repair_context:
                            all_endpoints_rejected_candidate = False
                            endpoint_failures.append(error)
                            _report_endpoint_failure(
                                report_endpoint_failure,
                                endpoint.configuration_id,
                                failure_kind="protocol",
                                failure_reason=error.reason,
                                failure_state=(
                                    "repair_exhausted"
                                    if repair_exhausted
                                    else "repair_unavailable"
                                ),
                            )
                            break
                        # Keep the rejected assistant response in the same
                        # bounded conversation. OpenAI tool calls must each
                        # receive a matching result before the correction.
                        _append_repair(
                            prompt,
                            assistant=assistant,
                            tool_result=correction,
                            correction=correction,
                        )
                        continue

                    return InterpretationResult(
                        admitted=admitted,
                        configuration_id=endpoint.configuration_id,
                        attempted_configuration_ids=tuple(attempted),
                    )
    except TimeoutError as error:
        if not deadline.expired():
            # Endpoint adapters must translate their own operational timeouts
            # to InterpreterTransportError. Do not conceal a broken adapter
            # contract behind the aggregate-deadline failure channel.
            raise
        # Deadline expiry is operational unavailability. It must never be
        # relabelled as a caller input problem, and the attempted route remains
        # available for deliberate operator reporting.
        if attempted:
            _report_endpoint_failure(
                report_endpoint_failure,
                attempted[-1],
                failure_kind="timeout",
                failure_reason="aggregate_timeout",
            )
        raise InterpreterUnavailable(
            InterpreterUnavailableReason.DEADLINE_EXCEEDED,
            attempted_configuration_ids=tuple(attempted),
        ) from error

    if all_endpoints_rejected_candidate:
        if last_rejection is None:
            raise AssertionError(
                "Interpreter rejection outcome has no runner diagnostic"
            )
        raise InterpretationRejected(last_rejection)
    unavailable = InterpreterUnavailable(
        InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED,
        attempted_configuration_ids=tuple(attempted),
    )
    if endpoint_failures:
        raise unavailable from ExceptionGroup(
            "Interpreter endpoint failures",
            endpoint_failures,
        )
    raise unavailable


def _report_endpoint_failure(
    report: Callable[[_events.InterpreterEndpointFailed], None],
    configuration_id: str,
    *,
    failure_kind: str,
    failure_reason: str,
    failure_state: str | None = None,
) -> None:
    """Project validated endpoint identity and closed facts for its destination."""

    report(
        _events.InterpreterEndpointFailed(
            configuration_id=configuration_id,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
            failure_state=failure_state,
        )
    )


def _append_repair(
    prompt: InterpreterPrompt,
    *,
    assistant: InterpreterTurn,
    tool_result: str,
    correction: str,
) -> None:
    """Continue one repairable assistant turn in its original conversation."""

    prompt.append(assistant.assistant_message)
    prompt.extend(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": tool_result,
        }
        for call_id in assistant.tool_call_ids
    )
    prompt.append({"role": "user", "content": correction})


def _require_failure_reason(reason: object, /) -> str:
    if (
        type(reason) is not str
        or not reason
        or len(reason) > 64
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in reason
        )
    ):
        raise TypeError("reason must be a bounded safe identifier")
    return reason


def _require_endpoints(endpoints: tuple[InterpreterEndpoint, ...]) -> None:
    if type(endpoints) is not tuple or not endpoints:
        raise TypeError("endpoints must be a non-empty tuple")
    if len(endpoints) > MAX_INTERPRETER_ENDPOINTS:
        raise ValueError(
            f"at most {MAX_INTERPRETER_ENDPOINTS} interpreter endpoints are supported"
        )
    configuration_ids: set[str] = set()
    for endpoint in endpoints:
        if type(endpoint) is not InterpreterEndpoint:
            raise TypeError("endpoints must contain InterpreterEndpoint values")
        if endpoint.configuration_id in configuration_ids:
            raise ValueError("interpreter configuration IDs must be unique")
        configuration_ids.add(endpoint.configuration_id)


def _read_prompt(path: Path) -> str:
    with path.open("rb") as prompt_file:
        raw = prompt_file.read(_MAX_PROMPT_BYTES + 1)
    if not raw or len(raw) > _MAX_PROMPT_BYTES:
        raise ValueError("prompt must be non-empty and bounded")
    prompt = raw.decode("utf-8", errors="strict")
    if not prompt.strip():
        raise ValueError("prompt must contain non-whitespace text")
    return prompt


def _dispatch_turn(
    turn: InterpreterTurn,
    *,
    capability: InterpretationCapability[Candidate],
) -> Candidate:
    invocation = turn.invocation
    if type(invocation) is not InterpreterToolInvocation:
        raise InterpreterProtocolError("missing_tool_invocation")
    if type(invocation.name) is not str or type(invocation.arguments) is not dict:
        raise InterpreterProtocolError("invalid_tool_invocation_fields")
    return _dispatch(
        invocation.name,
        invocation.arguments,
        capability=capability,
    )


def _dispatch(
    name: str,
    arguments: dict[str, object],
    *,
    capability: InterpretationCapability[Candidate],
) -> Candidate:
    match name:
        case InterpreterTool.SUBMIT_INTERPRETATION if set(arguments) == {"value"}:
            return capability.construct_interpretation(arguments["value"])
        case InterpreterTool.REPORT_INPUT_PROBLEM if set(arguments) == {"message"}:
            message = arguments["message"]
            if type(message) is not str:
                raise InterpreterProtocolError("unexpected_tool_invocation")
            _validate_reported_problem(message)
            raise ReportedInputProblem(ModelGeneratedText(message))
        case _:
            raise InterpreterProtocolError("unexpected_tool_invocation")


def _validate_reported_problem(message: str) -> None:
    """Apply the public failure contract before accepting a model report."""

    if not is_failure_message(message):
        raise InterpreterProtocolError("invalid_reported_input_problem") from None
