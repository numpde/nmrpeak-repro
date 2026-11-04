"""Translate bounded freeform NMR prose into validated model requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import json
from pathlib import Path
import unicodedata

from .diagnostic_text import diagnostic_text
from .canonical_json import canonical_json_bytes
from provider_core.conversation import ToolFinished, independent_interruption_failure
from provider_core.chat_errors import ChatFailed
from provider_core.diagnostics import failure_evidence
from provider_core.transport import HttpResponse
from provider_core.task_boundary import contain_task_exit
from provider_core.interpreter import (
    Interpreter, InterpretationFailed, InterpretationInputRejected, ModelReportedInterpretationProblem,
    ModelWindowExpired,
)

from .interpreter import (
    InterpreterCandidateConstructionRejected,
    InterpretationCandidateRejected,
    InterpreterProtocolError,
    InterpreterUnavailable,
    InterpreterUnavailableReason,
    InterpreterTool,
    ReportedInputProblem,
    _ScientificRejection,
    _dispatch,
    _read_prompt,
    _SYSTEM_PROMPT_PATH,
)
from .input_issue_message import render_candidate_correction
from .lifecycle_lane import LifecycleLane
from .openai_chat_interpreter import (
    OpenAIChatEndpointSpec,
    endpoint_factories,
    interpreter_tools,
)
from .interpreter_policy import InterpreterPolicy
from .product import CHF_OFFERING, HF_OFFERING
from .product_input import (
    INPUT_SCHEMA_ID,
    InputRejected,
    InputRejectionReason,
    SUPPORTED_MULTIPLICITIES,
    parse_job_input,
)
from .provider_events import (
    InterpreterEndpointFailed,
    InterpreterRoute,
    bounded_exception_type,
    render_provider_event,
)
from .runner_session import (
    RunnerInputRejected,
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
    "Report difficulty producing a complete, source-faithful interpretation. "
    "Cite source evidence when available; a rejected candidate alone does not prove the source is wrong."
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

    def new_session(self):
        """Adopt a per-Attempt interpretation owner before HTTP admission."""
        return InterpretationSession(self)



class InterpretationSession:
    """Retain one Attempt's model owner and original interpretation task."""

    def __init__(self, configured):
        self._configured = configured
        self._shared = None
        self._run_task = None
        self._run_observed = False
        self.interruption_failure = None
        self.diagnostic_failure = None
        self._closed = False

    @property
    def diagnostic_secrets(self):
        return self._configured.diagnostic_secrets

    async def retire(self):
        """Stop model I/O; the enclosing analysis owner also settles science."""
        self._closed = True
        if self._shared is not None:
            await self._shared.retire()

    async def join(self):
        """Observe the original task after its model and scientific owners settle."""
        if self._run_task is None:
            return
        cancellation_count = asyncio.current_task().cancelling()
        try:
            await asyncio.shield(self._run_task)
        except asyncio.CancelledError as interruption:
            if (asyncio.current_task().cancelling() != cancellation_count
                    or not self._closed or not self._run_task.cancelled()):
                raise
            if not self._run_observed:
                self.interruption_failure = independent_interruption_failure(interruption)
                if self.interruption_failure is None:
                    try:
                        self._run_task.result()
                    except asyncio.CancelledError as original:
                        self.interruption_failure = independent_interruption_failure(original)
                self._run_observed = True
                if self.interruption_failure is not None:
                    raise self.interruption_failure
        except BaseException:
            if not self._run_observed:
                self._run_observed = True
                raise

    async def validate_freeform_input_async(
        self, *, source, lane, validate_input, execution_attempt_ref, record_diagnostic=None,
    ):
        if self._closed or self._shared is not None:
            raise RuntimeError("Cannot interpret input: this Attempt's interpretation session already ran or is retiring")
        source_text = _admit_source_text(source)
        capability = _InterpretationCapability(lane, self.diagnostic_secrets)
        try:
            messages = [
                {"role": "system", "content": _read_prompt(_SYSTEM_PROMPT_PATH)},
                {"role": "user", "content": _read_prompt(capability.interpreter_prompt_path)},
                {"role": "user", "content": source_text},
            ]
        except (OSError, UnicodeError, ValueError) as error:
            raise InterpreterUnavailable(InterpreterUnavailableReason.PROMPT_UNAVAILABLE) from error
        tools = interpreter_tools(submit_description=_SUBMIT_DESCRIPTION, value_schema=_value_schema_for(lane),
                                  report_description=_REPORT_DESCRIPTION)
        factories = endpoint_factories(self._configured.endpoint_specs)
        self._shared = Interpreter(factories,
                                   timeout_seconds=self._configured.policy.interpretation_timeout_seconds,
                                   context_bytes=min(factory.context_bytes for factory in factories),
                                   secrets=self.diagnostic_secrets)

        async def dispatch(name, arguments, *, not_after):
            try:
                candidate = _dispatch(name, arguments, capability=capability)
                runner_input = lane.bind_runner_input(candidate)
                if asyncio.get_running_loop().time() >= not_after:
                    raise ModelWindowExpired()
                outcome = await validate_input(runner_input)
            except ReportedInputProblem as report:
                raise ModelReportedInterpretationProblem(
                    model_summary="A previous model could not produce a source-faithful interpretation."
                ) from report
            except InterpreterProtocolError as rejection:
                raise _ScientificRejection(rejection) from rejection
            if type(outcome) is RunnerInputRejected:
                rejection = InterpretationCandidateRejected(outcome.message, outcome.token_count)
                raise _ScientificRejection(rejection) from rejection
            return ToolFinished(outcome)

        async def constraints(tool, field, *, not_after):
            path = "value.model_input.spectra.1H.peaks.items.multiplicity"
            if tool == InterpreterTool.SUBMIT_INTERPRETATION and field == path:
                return {"field": path, "allowed_values": sorted(SUPPORTED_MULTIPLICITIES),
                        "note": "Copy the source label; do not substitute a supported value."}
            return NotImplemented

        self._run_task = asyncio.create_task(contain_task_exit(
            self._shared.run, messages, tools, dispatch, describe_constraints=constraints,
        ))
        try:
            result = await asyncio.shield(self._run_task)
        except asyncio.CancelledError:
            raise
        except InterpretationInputRejected as failure:
            self._run_observed = True
            raise InterpreterUnavailable(InterpreterUnavailableReason.INPUT_REJECTED) from failure
        except InterpretationFailed as failure:
            self._run_observed = True
            self._report_failures(failure.failures, execution_attempt_ref, record_diagnostic)
            visited = max((visit for visit, _ in failure.failures), default=0)
            route = tuple(spec.configuration_id for spec in self._configured.endpoint_specs[:visited])
            unavailable = InterpreterUnavailable(InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED, route)
            unavailable.add_note(diagnostic_text(
                json.dumps(failure_evidence(failure, secrets=self.diagnostic_secrets), ensure_ascii=True),
                secrets=self.diagnostic_secrets, limit=8192,
            ))
            self._report_route(InterpreterRoute(
                execution_attempt_ref=execution_attempt_ref, disposition=unavailable.reason.value,
                configuration_id=None, attempted_configuration_ids=route,
            ), warning=True)
            # Correction records remain protected; never send them to the model or caller.
            facts = []
            for visit, turn, rejection in failure.correction_failures:
                original = getattr(rejection, "original", rejection)
                entry = {"visit": visit, "turn": turn, "kind": type(original).__name__}
                if isinstance(original, InterpreterCandidateConstructionRejected):
                    entry.update(reason=original.issue.reason.value, pointer=original.issue.pointer)
                elif isinstance(original, InterpretationCandidateRejected):
                    entry.update(reason="runner_input_rejected", token_count=original.token_count)
                facts.append(entry)
            if facts:
                unavailable.add_note(json.dumps({"corrections": facts}, ensure_ascii=True))
            raise unavailable from failure
        except BaseException:
            self._run_observed = True
            self._report_failures(self._shared.failures, execution_attempt_ref, record_diagnostic)
            raise
        self._run_observed = True
        self._report_failures(result.failures, execution_attempt_ref, record_diagnostic)
        route = tuple(spec.configuration_id for spec in self._configured.endpoint_specs[:result.model_visit])
        self._report_route(InterpreterRoute(
            execution_attempt_ref=execution_attempt_ref, disposition="accepted",
            configuration_id=route[-1], attempted_configuration_ids=route,
        ))
        return result.value

    def _report_route(self, event, *, warning=False):
        try:
            log = _LOG.warning if warning else _LOG.info
            log("%s", render_provider_event(event))
        except Exception as failure:
            if self.diagnostic_failure is None:
                self.diagnostic_failure = failure

    def _report_failures(self, failures, execution_attempt_ref, record_diagnostic):
        events = [(visit, failure, None) for visit, failure in failures]
        events.extend((visit, failure, f"turn_{turn}")
                      for visit, turn, failure in self._shared.correction_failures)
        for visit, failure, state in events:
            try:
                original = getattr(failure, "original", failure)
                reason = bounded_exception_type(original)
                status = None
                if type(original) is ChatFailed:
                    if type(original.reason) is str and original.reason in {"retirement_unconfirmed", "request_limit", "request_cancelled",
                                           "exchange_failed", "http_status", "completion_limit",
                                           "invalid_response", "model_deadline"}:
                        reason = original.reason
                    response = original.response
                    if type(response) is HttpResponse and type(response.status) is int and 100 <= response.status <= 599:
                        status = response.status
                event = InterpreterEndpointFailed(
                    execution_attempt_ref=execution_attempt_ref,
                    configuration_id=self._configured.endpoint_specs[visit - 1].configuration_id,
                    failure_kind="correction" if state else "interpreter_failure",
                    failure_reason=reason, failure_state=state, http_status=status,
                )
                rendered = render_provider_event(event)
            except Exception as failure:
                if self.diagnostic_failure is None:
                    self.diagnostic_failure = failure
                continue
            if record_diagnostic is not None:
                try:
                    record_diagnostic(rendered)
                except Exception as failure:
                    if self.diagnostic_failure is None:
                        self.diagnostic_failure = failure
            try:
                _LOG.warning("%s", rendered)
            except Exception as failure:
                if self.diagnostic_failure is None:
                    self.diagnostic_failure = failure


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
