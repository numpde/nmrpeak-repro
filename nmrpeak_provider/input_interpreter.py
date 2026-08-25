"""Translate bounded freeform NMR prose into validated model requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import unicodedata

import httpx

from .canonical_json import canonical_json_bytes
from .interpreter import (
    InterpretationCandidateRejected,
    InterpreterProtocolError,
    InterpreterUnavailable,
    interpret,
)
from .lifecycle_lane import LifecycleLane
from .openai_chat_interpreter import (
    OpenAIChatEndpointSpec,
    bind_openai_chat_endpoints,
)
from .interpreter_policy import InterpreterPolicy
from .product import CHF_OFFERING, HF_OFFERING
from .product_input import (
    InputRejected,
    InputRejectionReason,
    parse_job_input,
)
from .provider_events import InterpreterEndpointFailed
from .runner_session import (
    RunnerInputRejected,
    RunnerSession,
    ValidatedRunnerRequest,
)
from .text_provenance import UserProvidedText


INTERPRETER_CONFIG_DIRECTORY = Path(
    "/run/secrets/nmrpeak-provider/openai-chat-completions.d"
)
MAX_FREEFORM_INPUT_BYTES = 16_384
_PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_LOG = logging.getLogger(__name__)
_PROMPT_PATHS = {
    HF_OFFERING: _PROMPT_DIRECTORY / "hf_interpreter.md",
    CHF_OFFERING: _PROMPT_DIRECTORY / "chf_interpreter.md",
}
@dataclass(frozen=True, slots=True)
class InputInterpreter:
    """Own immutable endpoint facts used by both lifecycle lanes."""

    endpoint_specs: tuple[OpenAIChatEndpointSpec, ...]
    policy: InterpreterPolicy

    def validate_freeform_input(
        self,
        *,
        source: bytes,
        lane: LifecycleLane,
        session: RunnerSession,
        execution_attempt_ref: str,
        provider_attempt_key: str,
    ) -> ValidatedRunnerRequest:
        """Interpret prose and return the runner capability that admitted it."""

        source_text = _admit_source_text(source)
        return asyncio.run(
            self._validate_freeform_input(
                source_text=source_text,
                lane=lane,
                session=session,
                execution_attempt_ref=execution_attempt_ref,
                provider_attempt_key=provider_attempt_key,
            )
        )

    async def _validate_freeform_input(
        self,
        *,
        source_text: UserProvidedText,
        lane: LifecycleLane,
        session: RunnerSession,
        execution_attempt_ref: str,
        provider_attempt_key: str,
    ) -> ValidatedRunnerRequest:
        capability = _InterpretationCapability(lane)

        async def admit_interpretation(
            model_input: object,
        ) -> ValidatedRunnerRequest:
            runner_input = lane.bind_runner_input(model_input)
            outcome = session.validate(
                execution_attempt_ref=execution_attempt_ref,
                provider_attempt_key=provider_attempt_key,
                model_input=runner_input,
            )
            if type(outcome) is RunnerInputRejected:
                raise InterpretationCandidateRejected(outcome.message)
            return outcome

        def report_endpoint_failure(event: InterpreterEndpointFailed) -> None:
            _report_endpoint_failure(
                event,
                execution_attempt_ref=execution_attempt_ref,
            )

        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            endpoints = bind_openai_chat_endpoints(
                self.endpoint_specs,
                self.policy.call_policy,
                http_client=client,
            )
            try:
                result = await interpret(
                    source_text=source_text,
                    capability=capability,
                    endpoints=endpoints.endpoints,
                    interpretation_timeout_seconds=(
                        self.policy.interpretation_timeout_seconds
                    ),
                    report_endpoint_failure=report_endpoint_failure,
                    admit_interpretation=admit_interpretation,
                )
            except InterpreterUnavailable as unavailable:
                attempted = ",".join(
                    unavailable.attempted_configuration_ids
                ) or "none"
                _LOG.warning(
                    "Cannot interpret Attempt %s: %s; attempted endpoints: %s. "
                    "No runner request was validated.",
                    execution_attempt_ref,
                    unavailable.reason.value,
                    attempted,
                )
                raise
            finally:
                await endpoints.join_response_releases()
        _LOG.info(
            "Interpreter accepted endpoint %s after route %s",
            result.configuration_id,
            ",".join(result.attempted_configuration_ids),
        )
        return result.admitted


@dataclass(frozen=True, slots=True)
class _InterpretationCapability:
    lane: LifecycleLane

    @property
    def interpreter_prompt_path(self) -> Path:
        return _PROMPT_PATHS[self.lane.offering]

    def construct_interpretation(self, value: object, /) -> object:
        try:
            encoded = canonical_json_bytes(value)
        except (TypeError, ValueError, UnicodeError) as error:
            raise InterpreterProtocolError(
                "submitted_value_not_constructible"
            ) from error
        return parse_job_input(encoded, self.lane.offering)


def _admit_source_text(source: bytes) -> UserProvidedText:
    if type(source) is not bytes:
        raise TypeError("Freeform input must be exact bytes")
    if not source:
        raise InputRejected(InputRejectionReason.INVALID_STRUCTURE)
    if len(source) > MAX_FREEFORM_INPUT_BYTES:
        raise InputRejected(InputRejectionReason.DOCUMENT_TOO_LARGE)
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InputRejected(InputRejectionReason.INVALID_JSON) from None
    if any(
        character == "\x00"
        or (
            unicodedata.category(character) in {"Cc", "Cf"}
            and character not in {"\n", "\t"}
        )
        for character in text
    ):
        raise InputRejected(InputRejectionReason.INVALID_STRUCTURE)
    return UserProvidedText(text)


def _report_endpoint_failure(
    event: InterpreterEndpointFailed,
    *,
    execution_attempt_ref: str,
) -> None:
    _LOG.warning(
        "Interpreter endpoint %s failed while preparing Attempt %s: %s/%s%s",
        event.configuration_id,
        execution_attempt_ref,
        event.failure_kind,
        event.failure_reason,
        f"/{event.failure_state}" if event.failure_state is not None else "",
    )


__all__ = [
    "INTERPRETER_CONFIG_DIRECTORY",
    "InputInterpreter",
]
