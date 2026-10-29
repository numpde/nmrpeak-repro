"""Supply NMR scientific preparation and results to the shared Attempt owner."""

import asyncio
from functools import partial
from hashlib import sha256
import json

from provider_core.analysis import (
    AnalysisFailure, AnalysisOutcomeUnresolved, AnalysisSuccess, ObservedWorkerCrash, PreparedAnalysis,
)
from provider_core.attempt_record import ProtectedEvidence

from .canonical_json import canonical_json_bytes
from .diagnostic_text import diagnostic_text, exception_diagnostic
from .failure_contract import ClassifiedPreparationFailure, FailureContractError
from .input_issue_message import (
    render_candidate_failure, render_candidate_runner_failure,
    render_direct_runner_rejection, render_source_issue,
)
from .interpreter import (
    CandidateConstructionExhausted, InterpretationRejected, InterpreterUnavailable,
    InterpreterUnavailableReason, ReportedInputProblem,
)
from .preparation_failure_policy import FailureKind
from .product_input import InputRejected, is_structured_source, parse_job_input
from .product_result import RESULT_SCHEMA_ID, canonical_result_bytes
from .run_generation import run_generation_fingerprint
from .runner_session import NativeWorkerExited, RunnerInputRejected
from .text_provenance import ProviderDiagnosticText


class NmrpeakAnalysisSession:
    """Own one Attempt's capability while its lane retains the loaded worker."""

    def __init__(self, generation, runner, interpreter, *, retry_seconds):
        if runner.result_facts != generation.result_facts:
            raise ValueError("Cannot bind analysis: runner and generation facts differ")
        self.generation, self.runner, self.interpreter = generation, runner, interpreter
        self._retry_seconds = retry_seconds
        self._acknowledged = self._prepared = self._request = None
        self._phase = "new"
        self._closed = False
        self._interpretation = interpreter.new_session()
        self._candidate_digest = None

    async def prepare(self, acknowledged, instructions, *, report_condition):
        if self._closed or self._phase != "new":
            raise ValueError("Cannot prepare analysis: this session already owns an Attempt")
        lane = self.generation.lane
        if (acknowledged.start.input != instructions.identity
                or acknowledged.admission.analysis_kind_ref != lane.offering.analysis_kind_ref
                or acknowledged.admission.provider_ref != self.generation.generation.provider_ref
                or acknowledged.start.scope.generation_id != run_generation_fingerprint(self.generation.generation)):
            raise ValueError("Cannot prepare analysis: Attempt input, lane or generation differs")
        self._acknowledged, self._phase = acknowledged, "preparing"
        structured = True
        try:
            structured = is_structured_source(instructions.specification_bytes)
            if structured:
                parsed = parse_job_input(instructions.specification_bytes, lane.offering,
                                         diagnostic_secrets=self.interpreter.diagnostic_secrets)
                validated = await self._validate_input(lane.bind_runner_input(parsed), report_condition=report_condition)
                if type(validated) is RunnerInputRejected:
                    return self._failure(FailureKind.DIRECT_RUNNER_REJECTED,
                                         render_direct_runner_rejection(validated.token_count),
                                         reason=validated.reason.value)
            else:
                validated = await self._prepare_prose(instructions.specification_bytes, report_condition)
                if type(validated) is AnalysisFailure:
                    return validated
        except InputRejected as rejection:
            return self._failure(FailureKind.DIRECT_SOURCE_ISSUE,
                                 render_source_issue(rejection.issue, structured=structured),
                                 reason=rejection.reason.value, pointer=rejection.issue.pointer)
        self._request = validated
        self._prepared = PreparedAnalysis(self._evidence("prepared", candidate_sha256=self._candidate_digest))
        self._phase = "prepared"
        return self._prepared

    async def _prepare_prose(self, source, report_condition):
        condition = False
        while True:
            try:
                validated = await self._interpretation.validate_freeform_input_async(
                    source=source, lane=self.generation.lane, validate_input=partial(self._validate_input, report_condition=report_condition),
                    execution_attempt_ref=self._acknowledged.admission.execution_attempt_ref,
                )
            except InterpreterUnavailable as unavailable:
                if not self.runner.quiescent:
                    raise AnalysisOutcomeUnresolved(
                        "Cannot confirm NMR validation after the interpreter became unavailable"
                    ) from unavailable
                codes = {
                    InterpreterUnavailableReason.PROMPT_UNAVAILABLE: "interpreter_prompt_unavailable",
                    InterpreterUnavailableReason.DEADLINE_EXCEEDED: "interpreter_deadline_exceeded",
                    InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED: "interpreter_endpoints_exhausted",
                }
                await report_condition(codes[unavailable.reason])
                condition = True
                await asyncio.sleep(self._retry_seconds)
                continue
            except ReportedInputProblem as problem:
                if condition:
                    await report_condition(None)
                return self._failure(FailureKind.MODEL_REPORTED_PROBLEM, diagnostic_text(
                    "The interpreter reported (not independently verified): "
                    + diagnostic_text(problem.message, limit=900) + ". Generation did not start.", limit=1024,
                ), reason="model_report", route=list(problem.attempted_configuration_ids))
            except CandidateConstructionExhausted as exhausted:
                if condition:
                    await report_condition(None)
                return self._failure(FailureKind.CANDIDATE_ISSUE, render_candidate_failure(exhausted.issue),
                                     reason=exhausted.issue.reason.value, pointer=exhausted.issue.pointer,
                                     route=list(exhausted.attempted_configuration_ids))
            except InterpretationRejected as rejection:
                if condition:
                    await report_condition(None)
                return self._failure(FailureKind.CANDIDATE_RUNNER_REJECTED,
                                     render_candidate_runner_failure(rejection.token_count),
                                     reason="runner_candidate_rejected",
                                     route=list(rejection.attempted_configuration_ids))
            except InputRejected:
                if condition:
                    await report_condition(None)
                raise
            if condition:
                await report_condition(None)
            return validated

    async def _validate_input(self, model_input, *, report_condition):
        digest = "sha256:" + sha256(canonical_json_bytes(model_input.wire_document())).hexdigest()
        acknowledged = self._acknowledged
        validated = await self._call_science(partial(
            self.runner.validate, execution_attempt_ref=acknowledged.admission.execution_attempt_ref,
            provider_attempt_key=json.loads(acknowledged.start.body)["provider_attempt_key"],
            model_input=model_input,
        ), report_condition)
        if type(validated) is not RunnerInputRejected:
            self._candidate_digest = digest
        return validated

    async def execute(self, prepared, *, report_condition):
        if self._closed or self._phase != "prepared" or prepared is not self._prepared:
            raise ValueError("Cannot execute analysis: prepared handle is not current for this session")
        request = self._request
        self._prepared = self._request = None
        self._phase = "running"
        generated = await self._call_science(partial(self.runner.generate, request), report_condition)
        result = canonical_result_bytes(self.runner.candidates_for_attempt(
            generated, execution_attempt_ref=self._acknowledged.admission.execution_attempt_ref,
            provider_attempt_key=json.loads(self._acknowledged.start.body)["provider_attempt_key"],
        ), self.runner.result_facts)
        evidence = self._evidence("result", candidate_sha256=self._candidate_digest,
                                  result_sha256="sha256:" + sha256(result).hexdigest())
        self._phase = "finished"
        return AnalysisSuccess(RESULT_SCHEMA_ID, result, evidence)

    async def _call_science(self, operation, report_condition):
        if self._closed:
            raise ValueError("Cannot begin scientific work: this Attempt session is retiring")
        report_condition.require_execution()
        try:
            return await self.runner.join_science(operation())
        except NativeWorkerExited as cause:
            raise ObservedWorkerCrash.from_session(
                self._acknowledged, phase=self._phase, owner=self, runner=cause.native,
                evidence=self._evidence("native_exit", cause=exception_diagnostic(cause)), cause=cause,
            ) from cause

    def _failure(self, kind, message, **facts):
        published = self.generation.lane.failure_policy.resolve(
            ClassifiedPreparationFailure(kind.value, ProviderDiagnosticText(message)))
        if published is None:
            raise FailureContractError("preparation_failure_requires_local_recovery")
        self._phase = "finished"
        return AnalysisFailure(published.failure_code, published.failure_message,
                               self._evidence("preparation_failure", kind=kind.value, **facts))

    def _evidence(self, stage, **facts):
        return ProtectedEvidence("nmrpeak.analysis_evidence.v1", canonical_json_bytes({
            "stage": stage,
            "execution_attempt_ref": self._acknowledged.admission.execution_attempt_ref,
            "input_fingerprint": self._acknowledged.start.input.input_fingerprint,
            "generation": run_generation_fingerprint(self.generation.generation),
            **facts,
        }))

    async def retire(self):
        self._closed = True
        failures = []
        try:
            await self._interpretation.retire()
        except BaseException as cause:
            failures.append(cause)
        if not self.runner.quiescent:
            try:
                await self.runner.retire()
            except BaseException as cause:
                failures.append(cause)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Interpreter and scientific retirement failed independently", failures)
