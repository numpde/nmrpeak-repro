"""Run generic tool interpretation across an ordered set of model endpoints.

This module owns prompt assembly, bounded protocol repair, ordered fallback,
and dispatch of the two generic tools. An injected capability supplies the
analysis-specific prompt and typed constructor; callers own outcome policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from typing import Generic, Protocol, TypeVar

from provider_core.conversation import (
    ConversationFailed, TextCorrection, ToolConversation, ToolFinished, ToolRejected,
)
from provider_core.analysis import ObservedWorkerCrash

from nmrpeak_provider.failure_message import is_failure_message
from nmrpeak_provider.diagnostic_text import diagnostic_text
from nmrpeak_provider.interpreter_policy import MAX_INTERPRETER_ENDPOINTS
from nmrpeak_provider.runner_protocol import (
    RunnerRejectionReason,
    runner_rejection_diagnostic,
)
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


@dataclass(frozen=True, slots=True, kw_only=True)
class InterpreterEndpointFailure:
    """Validated endpoint facts awaiting caller-owned operator correlation."""

    configuration_id: str
    failure_kind: str
    failure_reason: str
    failure_state: str | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_code: str | None = None
    request_id: str | None = None
    explanation: str | None = None


class InterpreterProtocolError(ValueError):
    """An assistant turn did not satisfy the generic tool contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(self.reason)


class InterpreterCandidateConstructionRejected(InterpreterProtocolError):
    """A valid submit tool value failed its product-owned constructor."""

    def __init__(self, reason: str, issue: object) -> None:
        super().__init__(reason)
        self.issue = issue


class CandidateConstructionExhausted(RuntimeError):
    """Every endpoint exhausted repair on a deterministic constructor issue."""

    def __init__(self, issue: object, attempted_configuration_ids: tuple[str, ...]) -> None:
        self.issue = issue
        self.attempted_configuration_ids = _require_route(attempted_configuration_ids)
        super().__init__("candidate_construction_exhausted")


class InterpreterTransportError(RuntimeError):
    """One endpoint failed with a classification and redacted explanation."""

    def __init__(
        self,
        reason: str = "unclassified",
        *,
        http_status: int | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        explanation: str | None = None,
    ) -> None:
        self.reason = _require_failure_reason(reason)
        if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
            raise TypeError("http_status must be an HTTP status")
        for value in (error_type, error_code, request_id, explanation):
            if value is not None and (type(value) is not str or len(value.encode("utf-8")) > 4096):
                raise TypeError("Endpoint diagnostic fields require bounded text")
        self.http_status = http_status
        self.error_type = error_type
        self.error_code = error_code
        self.request_id = request_id
        self.explanation = explanation
        super().__init__(self.reason + (": " + explanation if explanation else ""))


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
    """Every endpoint reported a model-authored assessment of the source."""

    def __init__(
        self,
        message: ModelGeneratedText,
        attempted_configuration_ids: tuple[str, ...] = (),
    ) -> None:
        self.message = message
        self.attempted_configuration_ids = _require_route(attempted_configuration_ids)
        # Model text may quote caller input. Keep it out of exception strings.
        super().__init__("reported_input_problem")


class InterpretationCandidateRejected(ValueError):
    """An analysis-specific admission boundary rejected one candidate."""

    def __init__(self, message: str, token_count: int) -> None:
        if message != runner_rejection_diagnostic(
            RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED, token_count
        ):
            raise TypeError("Candidate rejection requires exact runner token evidence")
        self.message = message
        self.token_count = token_count
        super().__init__(message)


class InterpretationRejected(ValueError):
    """Every configured endpoint produced a runner-rejected candidate."""

    def __init__(
        self, message: str, token_count: int,
        attempted_configuration_ids: tuple[str, ...] = (),
    ) -> None:
        if message != runner_rejection_diagnostic(
            RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED, token_count
        ):
            raise TypeError("Interpretation rejection requires exact runner token evidence")
        self.message = message
        self.token_count = token_count
        self.attempted_configuration_ids = _require_route(attempted_configuration_ids)
        super().__init__(message)


class InterpreterTool(StrEnum):
    """The complete generic tool-name vocabulary exposed to every endpoint."""

    SUBMIT_INTERPRETATION = "submit_interpretation"
    REPORT_INPUT_PROBLEM = "report_input_problem"


class ConversationEndpoint(Protocol):
    @property
    def identity(self) -> bytes: ...

    @property
    def retirement_confirmed(self) -> bool: ...

    async def complete(self, messages, tools) -> dict[str, object]: ...

    async def retire(self) -> None: ...


InterpretationAdmission = Callable[[Candidate], Awaitable[Admitted]]


@dataclass(frozen=True, slots=True)
class InterpreterEndpoint:
    """One owned carrier and scientific tool contract in fallback order."""

    configuration_id: str
    carrier: ConversationEndpoint
    tools: tuple[dict[str, object], dict[str, object]]
    secrets: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        require_interpreter_configuration_id(self.configuration_id)


class _ScientificRejection(ToolRejected):
    """Retain the typed provider rejection through SDK correction history."""

    def __init__(self, original: InterpreterProtocolError | InterpretationCandidateRejected):
        self.original = original
        super().__init__(str(original))


@dataclass(frozen=True, slots=True)
class _EndpointReport:
    message: ModelGeneratedText


def require_interpreter_configuration_id(value: object, /) -> None:
    """Admit an identifier before it reaches fallback events or logs."""

    if type(value) is not str or _CONFIGURATION_ID.fullmatch(value) is None:
        raise TypeError("configuration_id must be a bounded safe identifier")


def _require_route(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_INTERPRETER_ENDPOINTS:
        raise TypeError("interpreter route must be a bounded tuple")
    for configuration_id in value:
        require_interpreter_configuration_id(configuration_id)
    if len(set(value)) != len(value):
        raise ValueError("interpreter route IDs must be unique")
    return value


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
    report_endpoint_failure: Callable[[InterpreterEndpointFailure], None],
    admit_interpretation: InterpretationAdmission[Candidate, Admitted],
    retain_conversation: Callable[[ToolConversation], None],
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
    endpoint_kinds: list[str] = []
    last_rejection: str | None = None
    last_rejection_token_count: int | None = None
    last_constructor_issue: object | None = None
    last_report: ModelGeneratedText | None = None
    deadline = asyncio.timeout(interpretation_timeout_seconds)
    try:
        # This is the caller-visible operation bound. Endpoint adapters retain
        # their tighter per-turn bounds, while repair and fallback must share
        # one finite budget rather than multiplying it.
        async with deadline:
            for endpoint in endpoints:
                attempted.append(endpoint.configuration_id)
                async def dispatch(name, arguments):
                    try:
                        candidate = _dispatch(name, arguments, capability=capability)
                        admitted = await admit_interpretation(candidate)
                    except ReportedInputProblem as report:
                        return ToolFinished(_EndpointReport(ModelGeneratedText(diagnostic_text(
                            report.message, secrets=endpoint.secrets, limit=8192,
                        ))))
                    except (InterpreterProtocolError, InterpretationCandidateRejected) as rejection:
                        raise _ScientificRejection(rejection) from rejection
                    return ToolFinished(admitted)

                conversation = ToolConversation(
                    (endpoint.carrier,),
                    max_turns=MAX_TURNS_PER_ENDPOINT,
                    secrets=endpoint.secrets,
                    text_correction=TextCorrection(user_content=correction),
                )
                primary = None
                retained = False
                try:
                    try:
                        result = await conversation.run(base_prompt, endpoint.tools, dispatch)
                    except ObservedWorkerCrash:
                        retain_conversation(conversation)
                        retained = True
                        raise
                    except BaseException as error:
                        primary = error
                        raise
                    finally:
                        if not retained:
                            try:
                                await conversation.retire()
                            except BaseException as cleanup:
                                if primary is not None and cleanup is not primary:
                                    raise BaseExceptionGroup(
                                        "Interpreter conversation and retirement failed", [primary, cleanup]
                                    ) from None
                                raise
                except InterpreterTransportError as error:
                    endpoint_kinds.append("operational")
                    endpoint_failures.append(error)
                    _report_endpoint_failure(
                        report_endpoint_failure, endpoint.configuration_id,
                        failure_kind="transport", failure_reason=error.reason,
                        http_status=error.http_status, error_type=error.error_type,
                        error_code=error.error_code, request_id=error.request_id,
                        explanation=error.explanation,
                    )
                    continue
                except ConversationFailed as failure:
                    # Only the final repairable scientific rejection establishes
                    # unanimous constructor/admission exhaustion. Earlier rejected
                    # candidates never override a later carrier or protocol fault.
                    rejection = None
                    exhausted = bool(failure.cause is None and not failure.endpoint_failures
                                     and failure.correction_failures
                                     and failure.correction_failures[-1][0] == MAX_TURNS_PER_ENDPOINT)
                    if exhausted:
                        rejected = failure.correction_failures[-1][1]
                        if isinstance(rejected, _ScientificRejection):
                            rejection = rejected.original
                    endpoint_failures.append(
                        rejection if rejection is not None else
                        failure.cause if failure.cause is not None else failure
                    )
                    if isinstance(rejection, InterpretationCandidateRejected):
                        last_rejection = rejection.message
                        last_rejection_token_count = rejection.token_count
                        endpoint_kinds.append("admission")
                        _report_endpoint_failure(
                            report_endpoint_failure, endpoint.configuration_id,
                            failure_kind="admission", failure_reason="runner_input_rejected",
                            explanation=rejection.message, failure_state="repair_exhausted",
                        )
                    elif type(rejection) is InterpreterCandidateConstructionRejected:
                        last_constructor_issue = rejection.issue
                        endpoint_kinds.append("constructor")
                        _report_endpoint_failure(
                            report_endpoint_failure, endpoint.configuration_id,
                            failure_kind="construction", failure_reason="candidate_constructor_rejected",
                            explanation=rejection.reason, failure_state="repair_exhausted",
                        )
                    else:
                        endpoint_kinds.append("operational")
                        _report_endpoint_failure(
                            report_endpoint_failure, endpoint.configuration_id,
                            failure_kind="protocol",
                            failure_reason=(rejection.reason if rejection is not None
                                            else "invalid_tool_carrier" if failure.endpoint_failures
                                            else "conversation_failed" if not exhausted
                                            else "unexpected_tool_invocation"),
                            explanation=(str(failure.cause) if failure.endpoint_failures
                                         and failure.cause is not None else
                                         str(failure) if not failure.correction_failures else None),
                            failure_state="repair_exhausted" if exhausted else "repair_unavailable",
                        )
                    continue

                if type(result.value) is _EndpointReport:
                    last_report = result.value.message
                    endpoint_kinds.append("report")
                    _report_endpoint_failure(
                        report_endpoint_failure, endpoint.configuration_id,
                        failure_kind="report", failure_reason="reported_input_problem",
                        explanation=last_report,
                    )
                    continue
                return InterpretationResult(
                    admitted=result.value,
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

    if len(endpoint_kinds) != len(endpoints):
        raise AssertionError("Interpreter route ended without an endpoint outcome")
    if all(kind == "admission" for kind in endpoint_kinds):
        if last_rejection is None or last_rejection_token_count is None:
            raise AssertionError("Admission exhaustion has no runner diagnostic")
        raise InterpretationRejected(
            last_rejection, last_rejection_token_count, tuple(attempted)
        )
    if all(kind == "constructor" for kind in endpoint_kinds):
        if last_constructor_issue is None:
            raise AssertionError("Constructor exhaustion has no typed issue")
        raise CandidateConstructionExhausted(
            last_constructor_issue,
            tuple(attempted),
        )
    if all(kind == "report" for kind in endpoint_kinds):
        if last_report is None:
            raise AssertionError("Report exhaustion has no model report")
        raise ReportedInputProblem(last_report, tuple(attempted))
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
    report: Callable[[InterpreterEndpointFailure], None],
    configuration_id: str,
    *,
    failure_kind: str,
    failure_reason: str,
    failure_state: str | None = None,
    http_status: int | None = None,
    error_type: str | None = None,
    error_code: str | None = None,
    request_id: str | None = None,
    explanation: str | None = None,
) -> None:
    """Pass endpoint identity and producer evidence to its owning destination."""

    report(
        InterpreterEndpointFailure(
            configuration_id=configuration_id,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
            failure_state=failure_state,
            http_status=http_status,
            error_type=error_type,
            error_code=error_code,
            request_id=request_id,
            explanation=explanation,
        )
    )


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

    if not message.strip() or len(message.encode("utf-8", errors="replace")) > 8192:
        raise InterpreterProtocolError("invalid_reported_input_problem")
