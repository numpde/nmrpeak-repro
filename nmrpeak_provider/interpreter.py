"""Own Peak interpretation dispatch and provider-specific rejection facts."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
import re
from typing import Protocol, TypeVar

from provider_core.conversation import ToolRejected

from nmrpeak_provider.interpreter_policy import MAX_INTERPRETER_ENDPOINTS
from nmrpeak_provider.runner_protocol import (
    RunnerRejectionReason,
    runner_rejection_diagnostic,
)
from nmrpeak_provider.text_provenance import (
    ModelGeneratedText,
)


Candidate = TypeVar("Candidate")
MAX_INTERPRETER_CONFIGURATION_ID_BYTES = 128
_MAX_PROMPT_BYTES = 64 * 1024
_CONFIGURATION_ID = re.compile(
    rf"[a-z0-9][a-z0-9._-]{{0,{MAX_INTERPRETER_CONFIGURATION_ID_BYTES - 1}}}",
    re.ASCII,
)
_PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_SYSTEM_PROMPT_PATH = _PROMPT_DIRECTORY / "interpreter.md"


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


class InterpreterUnavailableReason(StrEnum):
    """The factual cause known at the interpreter boundary."""

    PROMPT_UNAVAILABLE = "prompt_unavailable"
    INPUT_REJECTED = "input_rejected"
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
    """One model reported an unverified obstacle to interpretation."""

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


class InterpreterTool(StrEnum):
    """The complete generic tool-name vocabulary exposed to every endpoint."""

    SUBMIT_INTERPRETATION = "submit_interpretation"
    REPORT_INTERPRETATION_PROBLEM = "report_interpretation_problem"


class _ScientificRejection(ToolRejected):
    """Retain the typed provider rejection through SDK correction history."""

    def __init__(self, original: InterpreterProtocolError | InterpretationCandidateRejected):
        self.original = original
        super().__init__(str(original))


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


class InterpretationCapability(Protocol[Candidate]):
    """Supply one analysis-specific prompt and construct its typed value.

    A constructor's ``InterpreterProtocolError`` is protocol evidence that may
    trigger a repair turn; it is not automatically a caller-input failure.
    """

    @property
    def interpreter_prompt_path(self) -> Path: ...

    def construct_interpretation(self, value: object, /) -> Candidate: ...


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
        case InterpreterTool.REPORT_INTERPRETATION_PROBLEM if set(arguments) == {"message"}:
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
