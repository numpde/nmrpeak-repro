"""Translate bounded freeform NMR prose into validated model requests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
from pathlib import Path
import unicodedata

from .diagnostic_text import diagnostic_text, exception_diagnostic
from .canonical_json import canonical_json_bytes
from .interpreter import (
    InterpreterCandidateConstructionRejected,
    InterpreterEndpointFailure,
    InterpretationCandidateRejected,
    InterpreterProtocolError,
    InterpreterUnavailable,
    interpret,
)
from .input_issue_message import render_candidate_correction
from .lifecycle_lane import LifecycleLane
from .openai_chat_interpreter import (
    OpenAIChatEndpointSpec,
    bind_openai_chat_endpoints,
)
from .interpreter_policy import InterpreterPolicy
from .product import CHF_OFFERING, HF_OFFERING
from .product_input import (
    INPUT_SCHEMA_ID,
    InputRejected,
    InputRejectionReason,
    parse_job_input,
)
from .provider_events import (
    InterpreterEndpointFailed,
    InterpreterRoute,
    render_provider_event,
)
from .runner_session import (
    RunnerInputRejected,
    RunnerSession,
    ValidatedRunnerRequest,
)
from .text_provenance import UserProvidedText


INTERPRETER_CONFIG_DIRECTORY = Path(
    "/run/config/nmrpeak-provider/interpreter"
)
MAX_FREEFORM_INPUT_BYTES = 16_384
_PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
_LOG = logging.getLogger(__name__)
_PROMPT_PATHS = {
    HF_OFFERING: _PROMPT_DIRECTORY / "hf_interpreter.md",
    CHF_OFFERING: _PROMPT_DIRECTORY / "chf_interpreter.md",
}
_SUBMIT_DESCRIPTION = (
    "Submit the complete HF or CHF NMRPeak request, copying every reported "
    "scientific value from the source without correction or inference."
)
_REPORT_DESCRIPTION = (
    "Report required source data that is missing or conflicting, or a "
    "source-explicit value this product cannot represent. Cite source evidence; "
    "a rejected candidate alone is not proof that the source is wrong."
)


def _value_schema_for(lane: LifecycleLane) -> dict[str, object]:
    """Describe the selected product shape; the typed parser remains authoritative."""

    proton_peak = {
        "type": "object",
        "properties": {
            "shift_lo": {"type": "string"},
            "shift_hi": {"type": "string"},
            "integral": {"type": "string"},
            # Do not enum the vocabulary: unsupported source labels must not
            # be silently substituted before the product parser can reject them.
            "multiplicity": {"type": "string"},
            "j_hz": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["shift_lo", "shift_hi", "integral", "multiplicity", "j_hz"],
        "additionalProperties": False,
    }
    proton_spectrum = {
        "type": "object",
        "properties": {
            "peaks": {"type": "array", "minItems": 1, "items": proton_peak}
        },
        "required": ["peaks"],
        "additionalProperties": False,
    }
    spectra: dict[str, object] = {"1H": proton_spectrum}
    if lane.offering is CHF_OFFERING:
        spectra["13C"] = {
            "type": "object",
            "properties": {
                "peaks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {"shift": {"type": "string"}},
                        "required": ["shift"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["peaks"],
            "additionalProperties": False,
        }
    elif lane.offering is not HF_OFFERING:
        raise AssertionError("NMRPeak tool schema requires a product-owned lane")
    return {
        "type": "object",
        "properties": {
            "schema_id": {"const": INPUT_SCHEMA_ID},
            "model_input": {
                "type": "object",
                "properties": {
                    "formula": {"type": "string"},
                    "spectra": {
                        "type": "object",
                        "properties": spectra,
                        "required": list(spectra),
                        "additionalProperties": False,
                    },
                },
                "required": ["formula", "spectra"],
                "additionalProperties": False,
            },
        },
        "required": ["schema_id", "model_input"],
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class InputInterpreter:
    """Own immutable endpoint facts used by both lifecycle lanes."""

    endpoint_specs: tuple[OpenAIChatEndpointSpec, ...]
    policy: InterpreterPolicy

    @property
    def diagnostic_secrets(self) -> tuple[str, ...]:
        """Supply configured keys to diagnostic projections before truncation."""
        return tuple(spec.api_key for spec in self.endpoint_specs)

    def validate_freeform_input(
        self,
        *,
        source: bytes,
        lane: LifecycleLane,
        session: RunnerSession,
        execution_attempt_ref: str,
        provider_attempt_key: str,
        record_diagnostic: Callable[[str], None] | None = None,
    ) -> ValidatedRunnerRequest:
        """Interpret prose and return the runner capability that admitted it."""

        async def validate_input(runner_input):
            return session.validate(
                execution_attempt_ref=execution_attempt_ref,
                provider_attempt_key=provider_attempt_key,
                model_input=runner_input,
            )

        return asyncio.run(self.validate_freeform_input_async(
            source=source, lane=lane, validate_input=validate_input,
            execution_attempt_ref=execution_attempt_ref,
            record_diagnostic=record_diagnostic,
        ))

    def new_session(self):
        """Adopt a per-Attempt interpretation owner before HTTP admission."""
        return InterpretationSession(self)

    async def validate_freeform_input_async(self, **arguments):
        """Settle an isolated convenience call within this explicit boundary."""
        session = self.new_session()
        primary = None
        try:
            return await session.validate_freeform_input_async(**arguments)
        except BaseException as cause:
            primary = cause
            raise
        finally:
            try:
                await session.retire()
            except BaseException as cleanup:
                if primary is not None and cleanup is not primary:
                    raise BaseExceptionGroup("Interpretation and HTTP retirement failed", [primary, cleanup]) from None
                raise


class InterpretationSession:
    """Retain one Attempt's interpreter HTTP owner through its final retirement."""

    def __init__(self, configured):
        self._configured = configured
        self._endpoints = None
        self._closed = False

    @property
    def diagnostic_secrets(self):
        return self._configured.diagnostic_secrets

    async def retire(self):
        self._closed = True
        if self._endpoints is not None:
            await self._endpoints.retire()

    async def validate_freeform_input_async(
        self, *, source, lane, validate_input, execution_attempt_ref,
        record_diagnostic=None,
    ):
        """Interpret prose while the owning session awaits each runner admission."""
        if self._closed:
            raise RuntimeError("Cannot interpret input: this Attempt's interpretation session is retiring")
        source_text = _admit_source_text(source)
        _LOG.info(
            'Interpreting input text; attempt=%s endpoints=%d',
            execution_attempt_ref,
            len(self._configured.endpoint_specs),
        )
        return await self._validate_freeform_input(
            source_text=source_text, lane=lane, validate_input=validate_input,
            execution_attempt_ref=execution_attempt_ref,
            record_diagnostic=record_diagnostic,
        )

    async def _validate_freeform_input(
        self,
        *,
        source_text: UserProvidedText,
        lane: LifecycleLane,
        validate_input,
        execution_attempt_ref: str,
        record_diagnostic: Callable[[str], None] | None = None,
    ) -> ValidatedRunnerRequest:
        capability = _InterpretationCapability(lane, self.diagnostic_secrets)

        async def admit_interpretation(
            model_input: object,
        ) -> ValidatedRunnerRequest:
            runner_input = lane.bind_runner_input(model_input)
            outcome = await validate_input(runner_input)
            if type(outcome) is RunnerInputRejected:
                _LOG.info(
                    'Interpretation rejected by runner; attempt=%s reason=runner_input_rejected',
                    execution_attempt_ref,
                )
                raise InterpretationCandidateRejected(
                    outcome.message, outcome.token_count
                )
            _LOG.info(
                'Interpretation validated by runner; attempt=%s',
                execution_attempt_ref,
            )
            return outcome

        def report_endpoint_failure(event: InterpreterEndpointFailure) -> None:
            event = replace(event, **{
                name: diagnostic_text(value, secrets=self.diagnostic_secrets, limit=4096)
                for name in ("explanation", "error_type", "error_code", "request_id")
                if (value := getattr(event, name)) is not None
            })
            _report_endpoint_failure(
                event,
                execution_attempt_ref=execution_attempt_ref,
                record_diagnostic=record_diagnostic,
            )

        if self._endpoints is None:
            self._endpoints = bind_openai_chat_endpoints(
                self._configured.endpoint_specs,
                self._configured.policy.call_policy,
                submit_interpretation_description=_SUBMIT_DESCRIPTION,
                submit_interpretation_value_schema=_value_schema_for(lane),
                report_input_problem_description=_REPORT_DESCRIPTION,
                report_endpoint_failure=report_endpoint_failure,
            )
        try:
            result = await interpret(
                source_text=source_text,
                capability=capability,
                endpoints=self._endpoints.endpoints,
                interpretation_timeout_seconds=(
                    self._configured.policy.interpretation_timeout_seconds
                ),
                report_endpoint_failure=report_endpoint_failure,
                admit_interpretation=admit_interpretation,
            )
        except InterpreterUnavailable as unavailable:
            _LOG.warning(
                "%s",
                render_provider_event(InterpreterRoute(
                    execution_attempt_ref=execution_attempt_ref,
                    disposition=unavailable.reason.value,
                    configuration_id=None,
                    attempted_configuration_ids=unavailable.attempted_configuration_ids,
                )),
            )
            exported = InterpreterUnavailable(unavailable.reason, unavailable.attempted_configuration_ids)
            exported.add_note(exception_diagnostic(
                unavailable, secrets=self.diagnostic_secrets, limit=8192,
            ))
            raise exported from None
        _LOG.info("%s", render_provider_event(InterpreterRoute(
            execution_attempt_ref=execution_attempt_ref,
            disposition="accepted",
            configuration_id=result.configuration_id,
            attempted_configuration_ids=result.attempted_configuration_ids,
        )))
        return result.admitted


@dataclass(frozen=True, slots=True)
class _InterpretationCapability:
    lane: LifecycleLane
    diagnostic_secrets: tuple[str, ...] = ()

    @property
    def interpreter_prompt_path(self) -> Path:
        return _PROMPT_PATHS[self.lane.offering]

    def construct_interpretation(self, value: object, /) -> object:
        try:
            encoded = canonical_json_bytes(value)
        except (TypeError, ValueError, UnicodeError) as error:
            raise InterpreterProtocolError(str(error)) from error
        try:
            return parse_job_input(encoded, self.lane.offering, diagnostic_secrets=self.diagnostic_secrets)
        except InputRejected as error:
            raise InterpreterCandidateConstructionRejected(
                render_candidate_correction(error.issue),
                error.issue,
            ) from error


def _admit_source_text(source: bytes) -> UserProvidedText:
    if type(source) is not bytes:
        raise TypeError("Freeform input must be exact bytes")
    if not source:
        raise InputRejected(InputRejectionReason.EMPTY_INPUT)
    if len(source) > MAX_FREEFORM_INPUT_BYTES:
        raise InputRejected(InputRejectionReason.DOCUMENT_TOO_LARGE)
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise InputRejected(InputRejectionReason.INVALID_UTF8) from error
    if any(
        character == "\x00"
        or (
            unicodedata.category(character) in {"Cc", "Cf"}
            and character not in {"\n", "\t"}
        )
        for character in text
    ):
        raise InputRejected(InputRejectionReason.DISALLOWED_CONTROL)
    return UserProvidedText(text)


def _report_endpoint_failure(
    event: InterpreterEndpointFailure,
    *,
    execution_attempt_ref: str,
    record_diagnostic: Callable[[str], None] | None = None,
) -> None:
    rendered = render_provider_event(InterpreterEndpointFailed(
        execution_attempt_ref=execution_attempt_ref,
        configuration_id=event.configuration_id,
        failure_kind=event.failure_kind,
        failure_reason=event.failure_reason,
        failure_state=event.failure_state,
        http_status=event.http_status,
        error_type=event.error_type,
        error_code=event.error_code,
        request_id=event.request_id,
        explanation=event.explanation,
    ))
    if record_diagnostic is not None:
        record_diagnostic(rendered)
    _LOG.warning("%s", rendered)


__all__ = [
    "INTERPRETER_CONFIG_DIRECTORY",
    "InputInterpreter",
]
