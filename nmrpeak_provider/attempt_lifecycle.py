"""Own one fixed NMRPeak lane's Job admission and Attempt lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, UTC
from hashlib import sha256
import math
import logging
import json
import time
from threading import Event, Thread
from typing import TYPE_CHECKING

from .attempt_identity import derive_provider_attempt_key
from .attempt_journal import (
    ActiveAttempt,
    LatestDiagnostic,
    LocalExecutionPhase,
    ObserveUntilExpiry,
    PublishInterruptedFailure,
    ReplayStart,
    ReplayTerminal,
    ResumePreExecution,
    RetainTerminalConflict,
    RetireResolved,
    StartPending,
    TerminalOperation,
    TerminalPending,
    bind_started_attempt,
    decide_restart,
    mark_execution_entered,
    prepared_terminal_replay,
    retain_terminal_command,
    validate_frozen_generation_id,
)
from .attempt_journal_store import AttemptJournalStore
from .generation_runtime import GenerationRuntime, GenerationRuntimeRejected
from .failure_contract import (
    ClassifiedPreparationFailure,
    FailureContractError,
    PreparationFailurePolicy,
)
from ._nmr_api_failures import terminal_report_condition
from .input_issue_message import (
    render_candidate_failure,
    render_candidate_runner_failure,
    render_direct_runner_rejection,
    render_source_issue,
)
from .preparation_failure_policy import FailureKind
from .lifecycle_lane import LifecycleLane
from .interpreter import (
    CandidateConstructionExhausted,
    InterpretationRejected,
    InterpreterUnavailable,
    InterpreterUnavailableReason,
    ReportedInputProblem,
)
from .runner_session import (
    RunnerInputRejected,
    RunnerSession,
    RunnerSessionRetired,
    GeneratedRunnerCandidates,
    ValidatedRunnerRequest,
)
from .runner_protocol import RunnerRejectionReason
from .provider_api import ProviderApiClient
from .provider_https import (
    ProviderHttpResponse,
    ProviderHttpsOutcome,
    ProviderOperation,
    ProviderRequestUnavailable,
    ProviderResponseRejected,
    ProviderTlsRejected,
)
from .provider_problems import (
    ProviderProblem,
    ProviderProblemRejected,
    parse_provider_problem,
)
from .provider_outcomes import (
    AttemptMutationCommitPossible,
    AttemptMutationCommitted,
    AttemptMutationNotCommitted,
    interpret_execution_attempt_complete,
    interpret_execution_attempt_fail,
    interpret_execution_attempt_progress,
    interpret_execution_attempt_start,
)
from .provider_requests import (
    prepare_execution_attempt_complete,
    prepare_execution_attempt_fail,
    prepare_execution_attempt_read,
    prepare_execution_attempt_progress,
    prepare_execution_attempt_start,
    prepare_job_input_read,
    prepare_jobs_list,
)
from .provider_events import (
    AttemptConditionConfirmed,
    AttemptConditionUnconfirmed,
    ExecutionObservationLost,
    ExecutionProcessFailed,
    ExecutionStopRequired,
    PreparationFailurePolicyDrift,
    PreparationFailureRetained,
    TerminalRecoveryHeld,
    bounded_exception_type,
    render_provider_event,
)
from .product_input import (
    InputIssue,
    InputRejected,
    InputRejectionReason,
    parse_job_input,
)
from .product_result import (
    RESULT_SCHEMA_ID,
    RunnerResultRejected,
    canonical_result_bytes,
)
from .provider_success import (
    AttemptState,
    ExecutionAttemptSnapshot,
    ExecutionAttemptStarted,
    ExecutionAttemptCompleted,
    ExecutionAttemptFailed,
    JobState,
    JobFeedItem,
    ProviderSuccessRejected,
    parse_execution_attempt_read_success,
    parse_job_input_read_success,
    parse_jobs_list_success,
    parse_retained_job_input_read_success,
)
from .run_generation import (
    RunGenerationIdentity,
    parse_canonical_utc_timestamp,
    run_generation_fingerprint,
)
from .text_provenance import ProviderDiagnosticText

if TYPE_CHECKING:
    from .input_interpreter import InputInterpreter


_LOG = logging.getLogger(__name__)
_FEED_PAGE_LIMIT = 50
_INTERRUPTED_FAILURE_MESSAGE = (
    "The provider process was interrupted before this execution completed."
)

ReadFailureEvidence = (
    ProviderProblem
    | ProviderProblemRejected
    | ProviderRequestUnavailable
    | ProviderResponseRejected
    | ProviderTlsRejected
    | ProviderSuccessRejected
)


@dataclass(frozen=True, slots=True)
class JobAdmitted:
    """One durable start obligation and its transient exact input bytes."""

    record: StartPending
    canonical_input: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class PageExhausted:
    """No Job on this page belongs to the admitted run-generation window."""

    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class FeedReadFailed:
    """The selected lane's Job page did not yield an admitted response."""

    evidence: ReadFailureEvidence


@dataclass(frozen=True, slots=True)
class InputReadFailed:
    """The selected Job's immutable input did not yield admitted bytes."""

    evidence: ReadFailureEvidence


AdmissionOutcome = (
    JobAdmitted
    | PageExhausted
    | FeedReadFailed
    | InputReadFailed
)


@dataclass(frozen=True, slots=True)
class StartContinues:
    """The API Attempt and its local pre-execution record are both durable."""

    record: ActiveAttempt


@dataclass(frozen=True, slots=True)
class StartResolved:
    """An idempotent start replay found the Attempt already terminal."""

    receipt: ExecutionAttemptStarted


StartOutcome = (
    StartContinues
    | StartResolved
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
)


@dataclass(frozen=True, slots=True)
class PreparedForExecution:
    """A PRE_EXECUTION Attempt and its session-owned validation capability."""

    record: ActiveAttempt
    request: ValidatedRunnerRequest = field(repr=False)


@dataclass(frozen=True, slots=True)
class InputFailurePending:
    """A fixed pre-execution failure is durable and awaits API delivery."""

    record: TerminalPending


@dataclass(frozen=True, slots=True)
class InputInterpretationUnavailable:
    """No configured interpreter produced a trustworthy answer in time."""

    evidence: InterpreterUnavailable


PreExecutionOutcome = (
    PreparedForExecution
    | InputFailurePending
    | InputInterpretationUnavailable
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
)


@dataclass(frozen=True, slots=True)
class AttemptObserved:
    """One authoritative point snapshot bound to the retained Attempt and Job."""

    snapshot: ExecutionAttemptSnapshot


@dataclass(frozen=True, slots=True)
class AttemptObservationFailed:
    """Server A did not yield an admitted point snapshot."""

    evidence: ReadFailureEvidence


AttemptObservation = AttemptObserved | AttemptObservationFailed


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    """Bound coordinator waits around fail-closed live point observation."""

    poll_interval_seconds: float
    shutdown_join_seconds: float

    def __post_init__(self) -> None:
        for value in (self.poll_interval_seconds, self.shutdown_join_seconds):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    "NMRPeak observation waits must be positive finite seconds"
                )


@dataclass(frozen=True, slots=True)
class CandidatesGenerated:
    """Candidates completed while Server A still admitted local execution."""

    record: ActiveAttempt
    candidates: GeneratedRunnerCandidates = field(repr=False)
    session: RunnerSession = field(repr=False)


@dataclass(frozen=True, slots=True)
class CompletionPending:
    """The exact canonical completion command is durable for delivery."""

    record: TerminalPending


@dataclass(frozen=True, slots=True)
class TerminalDelivered:
    """A command-bound receipt and durable journal retirement both succeeded."""

    receipt: ExecutionAttemptCompleted | ExecutionAttemptFailed


@dataclass(frozen=True, slots=True)
class TerminalPublicationHeld:
    """A durable local hold prevents any automatic terminal resend."""

    record: TerminalPending
    action: str
    description: str


@dataclass(frozen=True, slots=True)
class TerminalReconciliationPending:
    """Only the retained Attempt read may retry after a terminal refusal."""

    record: TerminalPending
    evidence: object


TerminalDeliveryOutcome = (
    TerminalDelivered
    | TerminalPublicationHeld
    | TerminalReconciliationPending
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
)


@dataclass(frozen=True, slots=True)
class RecoveryResumes:
    """A retained pre-execution Attempt and its re-read exact input bytes."""

    record: ActiveAttempt
    canonical_input: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class InterruptedFailurePending:
    """A fixed restart failure is durable and awaits exact delivery."""

    record: TerminalPending


@dataclass(frozen=True, slots=True)
class RecoveryResolved:
    """An authoritative terminal state retired one local obligation."""

    record: ActiveAttempt | TerminalPending
    snapshot: ExecutionAttemptSnapshot


RecoveryOutcome = (
    StartOutcome
    | RecoveryResumes
    | InterruptedFailurePending
    | RecoveryResolved
    | ObserveUntilExpiry
    | RetainTerminalConflict
    | TerminalDeliveryOutcome
    | AttemptObservationFailed
    | InputReadFailed
)


@dataclass(frozen=True, slots=True)
class ExecutionCutOff:
    """The Job closed or was cancelled while its Attempt remained live."""

    record: ActiveAttempt
    snapshot: ExecutionAttemptSnapshot


@dataclass(frozen=True, slots=True)
class ExecutionResolved:
    """Server A reached a terminal Attempt state and the journal was retired."""

    snapshot: ExecutionAttemptSnapshot


@dataclass(frozen=True, slots=True)
class ObservationLost:
    """Execution stopped because authoritative visibility was lost."""

    record: ActiveAttempt
    evidence: ReadFailureEvidence


class ExecutionShutdownFailed(RuntimeError):
    """Process-fatal: a generation worker may still be running after cancellation."""


ExecutionOutcome = (
    CandidatesGenerated
    | ExecutionCutOff
    | ExecutionResolved
    | ObservationLost
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
)

AdmittedJobOutcome = (
    StartOutcome
    | PreExecutionOutcome
    | ExecutionOutcome
    | TerminalDeliveryOutcome
)
RecoveryRunOutcome = (
    RecoveryOutcome
    | PreExecutionOutcome
    | ExecutionOutcome
    | TerminalDeliveryOutcome
)


@dataclass(slots=True)
class _GenerationWork:
    """One worker's result slot and completion signal, owned by its coordinator."""

    done: Event = field(default_factory=Event)
    candidates: GeneratedRunnerCandidates | None = None
    error: BaseException | None = None

    def run(self, session: RunnerSession, request: ValidatedRunnerRequest) -> None:
        """Signal every thread exit and preserve it for coordinator re-raise."""

        try:
            self.candidates = session.generate(request)
        except BaseException as error:
            self.error = error
        finally:
            self.done.set()


def admit_next_job(
    *,
    lane: LifecycleLane,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    generation: RunGenerationIdentity,
    frozen_generation_id: str,
    cursor: str | None = None,
) -> AdmissionOutcome:
    """Read and durably admit the first in-window Job from one lane page."""

    if type(generation) is not RunGenerationIdentity:
        raise TypeError("NMRPeak admission requires an exact run generation")
    if generation.analysis_kind_ref != lane.offering.analysis_kind_ref:
        raise ValueError("NMRPeak admission requires the lane-owned analysis kind")
    validate_frozen_generation_id(frozen_generation_id)

    feed_request = prepare_jobs_list(
        analysis_kind_ref=lane.offering.analysis_kind_ref,
        has_provider_execution_attempt=False,
        limit=_FEED_PAGE_LIMIT,
        cursor=cursor,
    )
    feed_response = api.send(feed_request)
    if type(feed_response) is not ProviderHttpResponse or feed_response.status != 200:
        return FeedReadFailed(_read_failure(feed_request.operation, feed_response))
    page = parse_jobs_list_success(feed_request, feed_response)
    if type(page) is ProviderSuccessRejected:
        return FeedReadFailed(page)

    selected_job = _first_in_generation(page.jobs, generation)
    if selected_job is None:
        return PageExhausted(page.next_cursor)

    _LOG.info(
        'Reading selected job input; job=%s analysis=%s',
        selected_job.job_ref,
        lane.offering.analysis_kind_ref,
    )
    input_request = prepare_job_input_read(
        job_ref=selected_job.job_ref,
        analysis_kind_ref=lane.offering.analysis_kind_ref,
    )
    input_response = api.send(input_request)
    if type(input_response) is not ProviderHttpResponse or input_response.status != 200:
        return InputReadFailed(_read_failure(input_request.operation, input_response))
    job_input = parse_job_input_read_success(
        input_request,
        input_response,
        expected_job=selected_job,
    )
    if type(job_input) is ProviderSuccessRejected:
        return InputReadFailed(job_input)

    generation_fingerprint = run_generation_fingerprint(generation)
    record = StartPending(
        job_ref=job_input.job_ref,
        provider_attempt_key=derive_provider_attempt_key(
            provider_ref=generation.provider_ref,
            run_generation_fingerprint=generation_fingerprint,
            job_ref=job_input.job_ref,
            input_fingerprint=job_input.input_fingerprint,
        ),
        input_fingerprint=job_input.input_fingerprint,
        frozen_generation_id=frozen_generation_id,
    )
    journal.admit(record)
    _LOG.info(
        "Job admitted to journal; job=%s attempt_key=%s analysis=%s input_fingerprint=%s",
        record.job_ref, record.provider_attempt_key,
        lane.offering.analysis_kind_ref, record.input_fingerprint,
    )
    return JobAdmitted(record=record, canonical_input=job_input.canonical_input)


def run_admitted_job(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    session: RunnerSession,
    interpreter: InputInterpreter,
    admitted: JobAdmitted,
    observation: ObservationPolicy,
) -> AdmittedJobOutcome:
    """Run one durable Job through its admitted lane until policy must decide again."""

    if type(admitted) is not JobAdmitted:
        raise TypeError("NMRPeak Job execution requires one admitted Job")
    if type(session) is not RunnerSession:
        raise TypeError("NMRPeak Job execution requires one admitted runner session")
    if type(observation) is not ObservationPolicy:
        raise TypeError("NMRPeak Job execution requires an admitted observation policy")
    resolved = runtime.resolve(admitted.record)
    if session.result_facts != resolved.result_facts:
        raise ValueError("NMRPeak Job execution received another lane's runner session")

    started = start_attempt(
        lane=resolved.lane,
        api=api,
        journal=journal,
        generation=resolved.generation,
        frozen_generation_id=runtime.frozen_generation_id,
        record=admitted.record,
    )
    if type(started) is not StartContinues:
        return started

    prepared = prepare_execution(
        lane=resolved.lane,
        api=api,
        journal=journal,
        session=session,
        interpreter=interpreter,
        record=started.record,
        canonical_input=admitted.canonical_input,
    )
    return _run_prepared_input(
        api=api,
        journal=journal,
        session=session,
        prepared=prepared,
        observation=observation,
    )


def run_recovery_record(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    session: RunnerSession | None,
    interpreter: InputInterpreter,
    record: StartPending | ActiveAttempt | TerminalPending,
    observation: ObservationPolicy | None,
) -> RecoveryRunOutcome:
    """Reconcile one startup obligation through any safe resumed execution."""

    recovered = reconcile_record(
        runtime=runtime,
        api=api,
        journal=journal,
        record=record,
    )
    if type(recovered) is StartContinues:
        recovered = reconcile_record(
            runtime=runtime,
            api=api,
            journal=journal,
            record=recovered.record,
        )
    if type(recovered) is InterruptedFailurePending:
        return deliver_terminal(api=api, journal=journal, record=recovered.record)
    if type(recovered) is not RecoveryResumes:
        return recovered

    resolved = runtime.resolve(recovered.record)
    if type(session) is not RunnerSession:
        raise TypeError("Resumed NMRPeak execution requires an admitted runner session")
    if type(observation) is not ObservationPolicy:
        raise TypeError("Resumed NMRPeak execution requires an observation policy")
    if session.result_facts != resolved.result_facts:
        raise ValueError("NMRPeak recovery received another lane's runner session")
    prepared = prepare_execution(
        lane=resolved.lane,
        api=api,
        journal=journal,
        session=session,
        interpreter=interpreter,
        record=recovered.record,
        canonical_input=recovered.canonical_input,
    )
    return _run_prepared_input(
        api=api,
        journal=journal,
        session=session,
        prepared=prepared,
        observation=observation,
    )


def _run_prepared_input(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    session: RunnerSession,
    prepared: PreExecutionOutcome,
    observation: ObservationPolicy,
) -> PreExecutionOutcome | ExecutionOutcome | TerminalDeliveryOutcome:
    if type(prepared) is InputFailurePending:
        return deliver_terminal(api=api, journal=journal, record=prepared.record)
    if type(prepared) is not PreparedForExecution:
        return prepared
    generated = execute_prepared(
        api=api,
        journal=journal,
        session=session,
        prepared=prepared,
        observation=observation,
    )
    if type(generated) is not CandidatesGenerated:
        return generated
    completion = select_completion(journal=journal, generated=generated)
    return deliver_terminal(api=api, journal=journal, record=completion.record)


def start_attempt(
    *,
    lane: LifecycleLane,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    generation: RunGenerationIdentity,
    frozen_generation_id: str,
    record: StartPending,
) -> StartOutcome:
    """Send one exact start and persist the command-bound server outcome."""

    if type(record) is not StartPending:
        raise TypeError("NMRPeak start requires a durable pending-start record")
    _require_generation(lane, record, generation, frozen_generation_id)

    _LOG.info(
        'Sending attempt start; job=%s attempt_key=%s',
        record.job_ref,
        record.provider_attempt_key,
    )
    prepared = prepare_execution_attempt_start(
        job_ref=record.job_ref,
        provider_attempt_key=record.provider_attempt_key,
    )
    outcome = interpret_execution_attempt_start(
        prepared,
        api.send(prepared),
        expected_provider_ref=generation.provider_ref,
        expected_analysis_kind_ref=lane.offering.analysis_kind_ref,
    )
    if type(outcome) is not AttemptMutationCommitted:
        return outcome
    receipt = outcome.receipt
    # Record the confirmed API effect even if the following journal update fails.
    _LOG.info(
        "API accepted attempt start; job=%s attempt=%s state=%s started_at=%s replayed=%s",
        record.job_ref, receipt.execution_attempt_ref, receipt.state.value,
        receipt.started_at, receipt.replayed,
    )
    if receipt.state is AttemptState.IN_PROGRESS:
        active = bind_started_attempt(record, receipt)
        journal.replace(record, active)
        return StartContinues(active)
    journal.retire(record)
    return StartResolved(receipt)


def prepare_execution(
    *,
    lane: LifecycleLane,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    session: RunnerSession,
    interpreter: InputInterpreter,
    record: ActiveAttempt,
    canonical_input: bytes,
) -> PreExecutionOutcome:
    """Validate one active Attempt without entering model execution."""

    if type(record) is not ActiveAttempt:
        raise TypeError("NMRPeak preparation requires an active Attempt record")
    if record.local_phase is not LocalExecutionPhase.PRE_EXECUTION:
        raise ValueError("NMRPeak preparation requires a pre-execution Attempt")
    if type(canonical_input) is not bytes:
        raise TypeError("NMRPeak preparation requires exact input bytes")
    if "sha256:" + sha256(canonical_input).hexdigest() != record.input_fingerprint:
        raise ValueError("NMRPeak preparation input does not match the Attempt journal")

    direct_rejection: InputRejected | None = None
    try:
        structured = _is_structured_source(canonical_input)
    except InputRejected as rejection:
        structured = True
        direct_rejection = rejection
    model_input = None
    if structured and direct_rejection is None:
        try:
            model_input = parse_job_input(canonical_input, lane.offering)
        except InputRejected as rejection:
            direct_rejection = rejection

    _LOG.info(
        'Reporting preparing phase; job=%s attempt=%s',
        record.job_ref,
        record.execution_attempt_ref,
    )
    progress = prepare_execution_attempt_progress(
        execution_attempt_ref=record.execution_attempt_ref,
        phase="preparing",
        condition_code=None,
    )
    progress_outcome = interpret_execution_attempt_progress(
        progress,
        api.send(progress),
    )
    clearing_interpreter_condition = (
        record.latest_diagnostic is not None
        and record.latest_diagnostic.kind == "interpreter_unavailable"
    )
    if clearing_interpreter_condition:
        _log_condition_outcome(record, "interpreter", None, progress_outcome)
    if type(progress_outcome) is not AttemptMutationCommitted:
        return progress_outcome

    _LOG.info(
        "Preparing input; job=%s attempt=%s source=%s",
        record.job_ref, record.execution_attempt_ref,
        "structured input" if structured else "freeform interpretation",
    )
    if direct_rejection is not None:
        return _retain_preparation_failure(
            journal,
            record,
            lane.failure_policy,
            ClassifiedPreparationFailure(
                FailureKind.DIRECT_SOURCE_ISSUE.value,
                ProviderDiagnosticText(
                    render_source_issue(direct_rejection.issue, structured=structured)
                ),
            ),
            direct_rejection.reason.value,
            direct_rejection.issue.pointer,
        )
    if structured:
        assert model_input is not None
        validated = session.validate(
            execution_attempt_ref=record.execution_attempt_ref,
            provider_attempt_key=record.provider_attempt_key,
            model_input=lane.bind_runner_input(model_input),
        )
        if type(validated) is RunnerInputRejected:
            if validated.reason is not RunnerRejectionReason.TOKEN_LIMIT_EXCEEDED:
                raise FailureContractError("runner_rejection_reason_not_input_constraint")
            return _retain_preparation_failure(
                journal, record, lane.failure_policy,
                ClassifiedPreparationFailure(
                    FailureKind.DIRECT_RUNNER_REJECTED.value,
                    ProviderDiagnosticText(
                        render_direct_runner_rejection(validated.token_count)
                    ),
                ),
                validated.reason.value,
            )
    else:
        try:
            validated = interpreter.validate_freeform_input(
                source=canonical_input,
                lane=lane,
                session=session,
                execution_attempt_ref=record.execution_attempt_ref,
                provider_attempt_key=record.provider_attempt_key,
            )
        except InputRejected as rejection:
            _LOG.warning(
                'Input preparation rejected before structure generation; '
                'job=%s attempt=%s reason=%s; provider ops: '
                'inspect input admission and interpretation',
                record.job_ref, record.execution_attempt_ref, rejection.reason.value,
            )
            return _retain_preparation_failure(
                journal,
                record,
                lane.failure_policy,
                ClassifiedPreparationFailure(
                    FailureKind.DIRECT_SOURCE_ISSUE.value,
                    ProviderDiagnosticText(
                        render_source_issue(rejection.issue, structured=False)
                    ),
                ),
                rejection.reason.value,
                rejection.issue.pointer,
            )
        except ReportedInputProblem as problem:
            return _retain_preparation_failure(
                journal, record, lane.failure_policy,
                ClassifiedPreparationFailure(FailureKind.MODEL_REPORTED_PROBLEM.value),
                "model_report",
                route=problem.attempted_configuration_ids,
            )
        except CandidateConstructionExhausted as exhausted:
            if type(exhausted.issue) is not InputIssue:
                raise TypeError("candidate exhaustion requires a product input issue")
            return _retain_preparation_failure(
                journal, record, lane.failure_policy,
                ClassifiedPreparationFailure(
                    FailureKind.CANDIDATE_ISSUE.value,
                    ProviderDiagnosticText(render_candidate_failure(exhausted.issue)),
                ),
                exhausted.issue.reason.value,
                exhausted.issue.pointer,
                exhausted.attempted_configuration_ids,
            )
        except InterpretationRejected as rejection:
            return _retain_preparation_failure(
                journal, record, lane.failure_policy,
                ClassifiedPreparationFailure(
                    FailureKind.CANDIDATE_RUNNER_REJECTED.value,
                    ProviderDiagnosticText(
                        render_candidate_runner_failure(rejection.token_count)
                    ),
                ),
                "runner_candidate_rejected",
                route=rejection.attempted_configuration_ids,
            )
        except InterpreterUnavailable as unavailable:
            held = replace(
                record,
                latest_diagnostic=LatestDiagnostic(
                    stage="preparation",
                    kind="interpreter_unavailable",
                    producer="interpreter",
                    reason=unavailable.reason.value,
                    observed_at=datetime.now(UTC).isoformat(),
                    endpoint_route=unavailable.attempted_configuration_ids,
                ),
            )
            journal.replace(record, held)
            _report_interpreter_condition(api, held, unavailable.reason)
            return InputInterpretationUnavailable(unavailable)
    _LOG.info(
        'Runner input validated; job=%s attempt=%s',
        record.job_ref,
        record.execution_attempt_ref,
    )
    return PreparedForExecution(record, validated)


def _is_structured_source(raw: bytes) -> bool:
    """Route exact Job bytes once; a failed structured parse never becomes prose."""

    prefix = raw.lstrip(b" \t\r\n")
    if prefix.startswith(b"\xef\xbb\xbf"):
        raise InputRejected(InputIssue(
            InputRejectionReason.INVALID_STRUCTURE,
            expected="UTF-8 text without a byte-order mark",
        ))
    if prefix and prefix[0] < 0x20:
        raise InputRejected(InputIssue(
            InputRejectionReason.INVALID_STRUCTURE,
            expected="printable UTF-8 text after JSON whitespace",
        ))
    return prefix.startswith((b"{", b"["))


def _report_interpreter_condition(
    api: ProviderApiClient,
    record: ActiveAttempt,
    reason: InterpreterUnavailableReason,
) -> None:
    condition = {
        InterpreterUnavailableReason.PROMPT_UNAVAILABLE:
            "interpreter_prompt_unavailable",
        InterpreterUnavailableReason.DEADLINE_EXCEEDED:
            "interpreter_deadline_exceeded",
        InterpreterUnavailableReason.ENDPOINTS_EXHAUSTED:
            "interpreter_endpoints_exhausted",
    }[reason]
    _report_progress_condition(api, record, "preparing", "interpreter", condition)


def _report_progress_condition(api, record, phase, context, condition) -> None:
    command = prepare_execution_attempt_progress(
        execution_attempt_ref=record.execution_attempt_ref, phase=phase,
        condition_code=condition,
    )
    try:
        observed = interpret_execution_attempt_progress(command, api.send(command))
    except Exception as error:
        evidence_type = bounded_exception_type(error)
        _LOG.warning("%s", render_provider_event(AttemptConditionUnconfirmed(
            job_ref=record.job_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            context=context,
            condition_code=condition,
            outcome_type="exception",
            evidence_type=evidence_type,
        )))
        return
    _log_condition_outcome(record, context, condition, observed)


def _log_execution_process_failure(
    record: ActiveAttempt,
    *,
    stage: str,
    failure_kind: str,
    error: BaseException,
    cleanup_state: str,
) -> None:
    _LOG.error("%s", render_provider_event(ExecutionProcessFailed(
        job_ref=record.job_ref,
        execution_attempt_ref=record.execution_attempt_ref,
        stage=stage,
        failure_kind=failure_kind,
        error_type=bounded_exception_type(error),
        result_state="unknown",
        recovery="restart_reconciliation",
        cleanup_state=cleanup_state,
    )))


def _cancel_session_after_failure(
    session: RunnerSession,
    error: BaseException,
    *,
    note: str,
) -> str:
    """Stop one runner without replacing the exception that triggered cleanup."""

    try:
        session.cancel()
    except BaseException as cleanup_error:
        error.add_note(
            f"{note} Cleanup failure type: {bounded_exception_type(cleanup_error)}."
        )
        return "unconfirmed"
    return "confirmed"


def _log_condition_outcome(record, context, condition, observed) -> None:
    if type(observed) is AttemptMutationCommitted:
        _LOG.info("%s", render_provider_event(AttemptConditionConfirmed(
            job_ref=record.job_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            context=context,
            condition_code=condition,
            updated_at=observed.receipt.updated_at,
        )))
    else:
        _LOG.warning("%s", render_provider_event(
            _condition_unconfirmed_event(record, context, condition, observed)
        ))


def _condition_unconfirmed_event(
    record: ActiveAttempt,
    context: str,
    condition: str | None,
    observed: AttemptMutationNotCommitted | AttemptMutationCommitPossible,
) -> AttemptConditionUnconfirmed:
    evidence = observed.evidence
    facts = _provider_evidence_facts(evidence)
    return AttemptConditionUnconfirmed(
        job_ref=record.job_ref,
        execution_attempt_ref=record.execution_attempt_ref,
        context=context,
        condition_code=condition,
        outcome_type=type(observed).__name__,
        evidence_type=type(evidence).__name__,
        **facts,
    )


def _provider_evidence_facts(evidence) -> dict[str, object]:
    status = None
    reason = None
    code = None
    delivery = None
    problem_type = None
    problem_title = None
    transport_request_id = None
    body_request_id = None
    if type(evidence) is ProviderProblem:
        status, code = evidence.status, evidence.code
        problem_type, problem_title = evidence.problem_type, evidence.title
        transport_request_id = evidence.transport_request_id
        body_request_id = evidence.body_request_id
    elif type(evidence) is ProviderProblemRejected:
        status, reason = evidence.status, evidence.reason.value
        if evidence.diagnostic is not None:
            problem_type = evidence.diagnostic.problem_type
            problem_title = evidence.diagnostic.title
            transport_request_id = evidence.diagnostic.header_request_id
            body_request_id = evidence.diagnostic.body_request_id
    elif type(evidence) is ProviderRequestUnavailable:
        status, delivery = evidence.status, evidence.delivery.value
    elif type(evidence) is ProviderResponseRejected:
        status, reason, delivery = (
            evidence.status, evidence.reason.value, evidence.delivery.value,
        )
    elif type(evidence) is ProviderTlsRejected:
        delivery = evidence.delivery.value
    elif type(evidence) is ProviderSuccessRejected:
        reason = evidence.reason.value
    else:
        raise AssertionError("unhandled provider evidence")
    return dict(
        http_status=status,
        reason=reason,
        code=code,
        delivery=delivery,
        problem_type=problem_type,
        problem_title=problem_title,
        transport_request_id=transport_request_id,
        body_request_id=body_request_id,
    )


def observe_attempt(
    *,
    api: ProviderApiClient,
    record: ActiveAttempt | TerminalPending,
) -> AttemptObservation:
    """Read Server A's current state for one retained NMRPeak Attempt."""

    if type(record) not in {ActiveAttempt, TerminalPending}:
        raise TypeError("NMRPeak observation requires a retained Attempt reference")
    prepared = prepare_execution_attempt_read(record.execution_attempt_ref)
    response = api.send(prepared)
    if type(response) is not ProviderHttpResponse or response.status != 200:
        return AttemptObservationFailed(
            _read_failure(prepared.operation, response)
        )
    snapshot = parse_execution_attempt_read_success(
        prepared,
        response,
        expected_job_ref=record.job_ref,
    )
    if type(snapshot) is ProviderSuccessRejected:
        return AttemptObservationFailed(snapshot)
    return AttemptObserved(snapshot)


def execute_prepared(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    session: RunnerSession,
    prepared: PreparedForExecution,
    observation: ObservationPolicy,
) -> ExecutionOutcome:
    """Generate only while bounded point reads keep the Attempt executable."""

    if type(prepared) is not PreparedForExecution:
        raise TypeError("NMRPeak execution requires a prepared runner request")
    if type(observation) is not ObservationPolicy:
        raise TypeError("NMRPeak execution requires an admitted observation policy")
    record = prepared.record
    if record.local_phase is not LocalExecutionPhase.PRE_EXECUTION:
        raise ValueError("NMRPeak execution requires a pre-execution Attempt")

    _LOG.info(
        'Reporting running phase; job=%s attempt=%s',
        record.job_ref,
        record.execution_attempt_ref,
    )
    running = prepare_execution_attempt_progress(
        execution_attempt_ref=record.execution_attempt_ref,
        phase="running",
        condition_code=None,
    )
    try:
        running_outcome = interpret_execution_attempt_progress(
            running,
            api.send(running),
        )
    except BaseException as error:
        cleanup_state = _cancel_session_after_failure(
            session,
            error,
            note="The validated NMRPeak session also failed to stop.",
        )
        _log_execution_process_failure(
            record,
            stage="running_progress",
            failure_kind="running_progress_failed",
            error=error,
            cleanup_state=cleanup_state,
        )
        raise
    if type(running_outcome) is not AttemptMutationCommitted:
        _LOG.warning("%s", render_provider_event(
            _condition_unconfirmed_event(
                record, "execution_running", None, running_outcome
            )
        ))
        try:
            session.cancel()
        except RunnerSessionRetired as error:
            failure = ExecutionShutdownFailed(
                "NMRPeak runner stop was unconfirmed after the running update"
            )
            _log_execution_process_failure(
                record,
                stage="generation_shutdown",
                failure_kind="running_progress_unconfirmed",
                error=error,
                cleanup_state="unconfirmed",
            )
            raise failure from error
        return running_outcome

    entered = mark_execution_entered(record)
    try:
        journal.replace(record, entered)
    except BaseException as error:
        cleanup_state = "confirmed"
        try:
            session.cancel()
        except RunnerSessionRetired:
            cleanup_state = "unconfirmed"
            error.add_note(
                "The validated NMRPeak session also failed to stop before generation."
            )
        _log_execution_process_failure(
            record,
            stage="execution_entry_retention",
            failure_kind="journal_retention_failed",
            error=error,
            cleanup_state=cleanup_state,
        )
        raise

    try:
        initial_observation = observe_attempt(api=api, record=entered)
    except BaseException as error:
        cleanup_state = _cancel_session_after_failure(
            session,
            error,
            note="The validated NMRPeak session also failed to stop.",
        )
        _log_execution_process_failure(
            entered,
            stage="attempt_observation",
            failure_kind="initial_observation_failed",
            error=error,
            cleanup_state=cleanup_state,
        )
        raise
    if not _observation_allows_execution(initial_observation):
        _log_execution_stop_trigger(entered, initial_observation)
        try:
            session.cancel()
        except RunnerSessionRetired as error:
            failure = ExecutionShutdownFailed(
                "NMRPeak runner stop was unconfirmed before generation"
            )
            _log_execution_process_failure(
                entered,
                stage="generation_shutdown",
                failure_kind="pre_generation_stop_unconfirmed",
                error=error,
                cleanup_state="unconfirmed",
            )
            raise failure from error
        try:
            return _stopped_execution_outcome(journal, entered, initial_observation)
        except BaseException as error:
            _log_execution_process_failure(
                entered,
                stage="execution_state_retention",
                failure_kind="journal_retention_failed",
                error=error,
                cleanup_state="confirmed",
            )
            raise

    work = _GenerationWork()
    worker = Thread(
        target=work.run,
        args=(session, prepared.request),
        name="nmrpeak-generation",
    )
    failure_stage = "generation_start"
    failure_kind = "generation_start_failed"
    try:
        started_at = time.monotonic()
        worker.start()
        _LOG.info(
            'Generation worker started; job=%s attempt=%s',
            record.job_ref,
            record.execution_attempt_ref,
        )
        while not work.done.is_set():
            failure_stage = "attempt_observation"
            failure_kind = "ongoing_observation_failed"
            current = observe_attempt(api=api, record=entered)
            if not _observation_allows_execution(current):
                _log_execution_stop_trigger(entered, current)
                failure_stage = "generation_shutdown"
                failure_kind = "generation_shutdown_unconfirmed"
                _cancel_and_join_generation(session, worker, observation)
                failure_stage = "execution_state_retention"
                failure_kind = "journal_retention_failed"
                return _stopped_execution_outcome(journal, entered, current)
            failure_stage = "generation_exchange"
            failure_kind = "generation_coordination_failed"
            work.done.wait(observation.poll_interval_seconds)

        failure_stage = "generation_shutdown"
        failure_kind = "generation_shutdown_failed"
        worker.join(observation.shutdown_join_seconds)
        if worker.is_alive():
            raise ExecutionShutdownFailed(
                "NMRPeak generation signalled completion but its worker did not stop"
            )
        failure_stage = "attempt_observation"
        failure_kind = "final_observation_failed"
        final_observation = observe_attempt(api=api, record=entered)
        if not _observation_allows_execution(final_observation):
            _log_execution_stop_trigger(entered, final_observation)
            failure_stage = "generation_shutdown"
            failure_kind = "generation_shutdown_unconfirmed"
            try:
                session.cancel()
            except RunnerSessionRetired as error:
                raise ExecutionShutdownFailed(
                    "NMRPeak runner stop was unconfirmed after generation"
                ) from error
            failure_stage = "execution_state_retention"
            failure_kind = "journal_retention_failed"
            return _stopped_execution_outcome(journal, entered, final_observation)
        failure_stage = "generation_exchange"
        if work.error is not None:
            failure_kind = (
                "runner_session_retired"
                if type(work.error) is RunnerSessionRetired
                else "generation_failed"
            )
            raise work.error
        if work.candidates is None:
            failure_kind = "generation_failed"
            raise AssertionError(
                "NMRPeak generation finished without candidates or an error"
            )
        _LOG.info(
            'Generation returned candidates; job=%s attempt=%s elapsed_seconds=%.3f; awaiting '
            'result validation and API delivery',
            record.job_ref,
            record.execution_attempt_ref,
            time.monotonic() - started_at,
        )
        return CandidatesGenerated(entered, work.candidates, session)
    except BaseException as error:
        error.add_note(
            f"During NMRPeak generation for job {record.job_ref}, "
            f"attempt {record.execution_attempt_ref}."
        )
        cleanup_state = "unconfirmed"
        if worker.is_alive():
            try:
                _cancel_and_join_generation(session, worker, observation)
                cleanup_state = "confirmed"
            except ExecutionShutdownFailed:
                cleanup_state = "unconfirmed"
                error.add_note(
                    "The NMRPeak generation worker also failed to stop after the error."
                )
        elif type(error) not in {ExecutionShutdownFailed, RunnerSessionRetired}:
            cleanup_state = _cancel_session_after_failure(
                session,
                error,
                note="The NMRPeak runner session also failed to stop after the error.",
            )
        if type(error) is ExecutionShutdownFailed:
            failure_stage = "generation_shutdown"
            failure_kind = "generation_shutdown_unconfirmed"
        elif type(error) is RunnerSessionRetired and failure_stage == "generation_exchange":
            failure_kind = "runner_session_retired"
        _log_execution_process_failure(
            entered,
            stage=failure_stage,
            failure_kind=failure_kind,
            error=error,
            cleanup_state=cleanup_state,
        )
        raise


def select_completion(
    *,
    journal: AttemptJournalStore,
    generated: CandidatesGenerated,
) -> CompletionPending:
    """Durably select one canonical completion without sending it."""

    if type(generated) is not CandidatesGenerated:
        raise TypeError("NMRPeak completion requires generated candidates")
    record = generated.record
    if record.local_phase is not LocalExecutionPhase.EXECUTION_ENTERED:
        raise ValueError("NMRPeak completion requires an entered execution")
    try:
        result = canonical_result_bytes(
            generated.session.candidates_for_attempt(
                generated.candidates,
                execution_attempt_ref=record.execution_attempt_ref,
                provider_attempt_key=record.provider_attempt_key,
            ),
            generated.session.result_facts,
        )
    except BaseException as error:
        cleanup_state = _cancel_session_after_failure(
            generated.session,
            error,
            note="The rejected NMRPeak result's runner session also failed to stop.",
        )
        failure_kind = (
            error.reason.value
            if type(error) is RunnerResultRejected
            else "result_correlation_failed"
            if type(error) is ValueError
            else "result_validation_failed"
        )
        _log_execution_process_failure(
            record,
            stage="result_validation",
            failure_kind=failure_kind,
            error=error,
            cleanup_state=cleanup_state,
        )
        raise
    try:
        prepared = prepare_execution_attempt_complete(
            execution_attempt_ref=record.execution_attempt_ref,
            result_schema_id=RESULT_SCHEMA_ID,
            canonical_result=result,
        )
        terminal = retain_terminal_command(record, prepared)
    except BaseException as error:
        cleanup_state = _cancel_session_after_failure(
            generated.session,
            error,
            note="The NMRPeak runner session also failed to stop after completion preparation failed.",
        )
        _log_execution_process_failure(
            record,
            stage="completion_preparation",
            failure_kind="completion_preparation_failed",
            error=error,
            cleanup_state=cleanup_state,
        )
        raise
    try:
        journal.replace(record, terminal)
    except BaseException as error:
        cleanup_state = _cancel_session_after_failure(
            generated.session,
            error,
            note="The NMRPeak runner session also failed to stop after completion retention failed.",
        )
        _log_execution_process_failure(
            record,
            stage="completion_retention",
            failure_kind="journal_retention_failed",
            error=error,
            cleanup_state=cleanup_state,
        )
        raise
    _LOG.info(
        "Completion retained for API delivery; job=%s attempt=%s result_sha256=%s result_bytes=%d",
        record.job_ref, record.execution_attempt_ref,
        sha256(result).hexdigest(), len(result),
    )
    return CompletionPending(terminal)


def deliver_terminal(
    *, api: ProviderApiClient, journal: AttemptJournalStore, record: TerminalPending,
) -> TerminalDeliveryOutcome:
    """Prioritize the retained terminal report, then one disposable observation."""
    outcome = _deliver_terminal(api=api, journal=journal, record=record)
    if type(outcome) is not TerminalDelivered:
        latest = outcome.record if type(outcome) in {TerminalPublicationHeld, TerminalReconciliationPending} else record
        automation = ("reconciling" if type(outcome) is TerminalReconciliationPending else
                      "held" if latest.terminal_hold_action is not None else "retrying")
        _report_terminal_condition(api, latest, automation)
    return outcome


def _report_terminal_condition(api, record, automation):
    if record.terminal_observed_state in {"succeeded", "failed", "expired", "not_visible"}:
        return
    if record.local_phase is None:
        _LOG.warning("Attempt %s: phase provenance is unavailable; no condition was sent. "
                     "Exact terminal report remains retained; provider ops can inspect this legacy journal record.",
                     record.execution_attempt_ref)
        return
    phase = "preparing" if record.local_phase is LocalExecutionPhase.PRE_EXECUTION else "running"
    condition = terminal_report_condition(automation)
    # This disposable progress send never changes terminal delivery certainty.
    _report_progress_condition(api, record, phase, "terminal_recovery", condition)


def _deliver_terminal(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    record: TerminalPending,
) -> TerminalDeliveryOutcome:
    """Send one exact retained terminal command and retire only its receipt."""

    if type(record) is not TerminalPending:
        raise TypeError("NMRPeak terminal delivery requires a retained command")
    journal.require_current(record)
    if record.terminal_hold_action is not None:
        return _reconcile_terminal(api, journal, record) if record.terminal_reconciling else _terminal_held(record)
    _LOG.info(
        'Sending retained %s command; job=%s attempt=%s',
        record.terminal_operation.value,
        record.job_ref,
        record.execution_attempt_ref,
    )
    prepared = prepared_terminal_replay(record)
    sent = api.send(prepared)
    outcome = (
        interpret_execution_attempt_complete(prepared, sent)
        if record.terminal_operation is TerminalOperation.COMPLETE
        else interpret_execution_attempt_fail(prepared, sent)
    )
    if type(outcome) is not AttemptMutationCommitted:
        evidence = getattr(outcome, "evidence", None)
        if type(evidence) is ProviderProblem and evidence.status == 409:
            problem = evidence
            if problem.conflict_action is None:
                return outcome
            held = replace(record, terminal_hold_action=problem.conflict_action,
                           terminal_hold_description=problem.conflict_description or problem.detail or "API conflict requires operator reconciliation",
                           terminal_hold_code=problem.code, terminal_hold_detail=problem.detail,
                           terminal_hold_request_id=problem.transport_request_id)
            journal.replace(record, held)
            return _reconcile_terminal(api, journal, held) if held.terminal_reconciling else _terminal_held(held)
        return outcome
    receipt = outcome.receipt
    # Record the confirmed API effect even if the following journal update fails.
    _LOG.info(
        "API confirmed %s; job=%s attempt=%s committed_at=%s replayed=%s",
        record.terminal_operation.value, record.job_ref, record.execution_attempt_ref,
        receipt.committed_at, receipt.replayed,
    )
    if type(receipt) is ExecutionAttemptCompleted:
        _LOG.info(
            'Analysis result published; job=%s result=%s',
            record.job_ref,
            receipt.analysis_result_ref,
        )
    journal.retire(record)
    _LOG.info(
        'Attempt journal record retired; job=%s attempt=%s',
        record.job_ref,
        record.execution_attempt_ref,
    )
    return TerminalDelivered(receipt)


def terminal_recovery_facts(record: TerminalPending) -> dict:
    """Describe retained local recovery without creating an HTTP outcome."""
    pending = record.terminal_reconciling
    return {"job_ref": record.job_ref, "execution_attempt_ref": record.execution_attempt_ref,
            "operation": record.terminal_operation.value, "command_fingerprint": record.terminal_request_fingerprint,
            "command_retained": True, "delivery": "unconfirmed", "automatic_resends": "stopped_including_restart",
            "automatic_reads": "retry_with_backoff" if pending else "stopped", "new_work_for_attempt": "stopped",
            "action": record.terminal_hold_action, "description": record.terminal_hold_description,
            "code": record.terminal_hold_code, "detail": record.terminal_hold_detail,
            "request_id": record.terminal_hold_request_id, "observed_state": record.terminal_observed_state,
            "next_actor": "provider" if pending else "provider_operator",
            "next_action": "retry only the Attempt read" if pending else "reconcile the original command and API outcome; involve the provider developer to investigate mismatches"}


def _log_terminal_recovery(record):
    facts = terminal_recovery_facts(record)
    _LOG.warning("%s", render_provider_event(TerminalRecoveryHeld(
        job_ref=facts["job_ref"],
        execution_attempt_ref=facts["execution_attempt_ref"],
        operation=facts["operation"],
        command_fingerprint=facts["command_fingerprint"],
        delivery=facts["delivery"],
        automatic_resends=facts["automatic_resends"],
        automatic_reads=facts["automatic_reads"],
        new_work_for_attempt=facts["new_work_for_attempt"],
        action=facts["action"],
        description=facts["description"],
        code=facts["code"], detail=facts["detail"], request_id=facts["request_id"],
        observed_state=facts["observed_state"], next_actor=facts["next_actor"],
        next_action=facts["next_action"],
    )))


def _terminal_held(record):
    _log_terminal_recovery(record)
    return TerminalPublicationHeld(record, record.terminal_hold_action, record.terminal_hold_description)


def _reconcile_terminal(api, journal, record):
    observed = observe_attempt(api=api, record=record)
    if type(observed) is AttemptObservationFailed:
        evidence = observed.evidence
        if type(evidence) is ProviderProblem and evidence.status == 404:
            state = "not_visible"
        else:
            _log_terminal_recovery(record)
            return TerminalReconciliationPending(record, evidence)
    else:
        state = observed.snapshot.state.value
    held = replace(record, terminal_observed_state=state)
    journal.replace(record, held)
    return _terminal_held(held)


def reconcile_record(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    record: StartPending | ActiveAttempt | TerminalPending,
) -> RecoveryOutcome:
    """Apply the existing restart decision to one durable NMRPeak obligation."""

    if type(record) not in {StartPending, ActiveAttempt, TerminalPending}:
        raise TypeError("NMRPeak recovery requires an exact journal record")
    _LOG.debug(
        'Reconciling retained attempt; job=%s attempt_key=%s local_record=%s',
        record.job_ref,
        record.provider_attempt_key,
        type(record).__name__,
    )
    if type(record) is StartPending:
        if record.frozen_generation_id != runtime.frozen_generation_id:
            raise GenerationRuntimeRejected(
                "Cannot replay a pending Attempt start from frozen generation "
                f"{record.frozen_generation_id} under current generation "
                f"{runtime.frozen_generation_id}. The journal record remains retained."
            )
        resolved = runtime.resolve(record)
        decision = decide_restart(record, None)
        if type(decision) is not ReplayStart:
            raise AssertionError("Pending NMRPeak start produced an unsupported restart action")
        return start_attempt(
            lane=resolved.lane,
            api=api,
            journal=journal,
            generation=resolved.generation,
            frozen_generation_id=runtime.frozen_generation_id,
            record=decision.record,
        )

    if type(record) is TerminalPending and record.terminal_hold_action is not None:
        return deliver_terminal(api=api, journal=journal, record=record)

    observed = observe_attempt(api=api, record=record)
    if type(observed) is AttemptObservationFailed:
        return observed
    decision = decide_restart(record, observed.snapshot)
    if type(decision) is ResumePreExecution:
        if record.frozen_generation_id != runtime.frozen_generation_id:
            decision = PublishInterruptedFailure(record)
        else:
            resolved = runtime.resolve(record)
            return _recover_input(resolved.lane, api, decision.record)
    if type(decision) is PublishInterruptedFailure:
        _LOG.warning(
            'Execution cannot resume after interruption; retaining failure for API delivery; '
            'job=%s attempt=%s',
            record.job_ref,
            record.execution_attempt_ref,
        )
        prepared = prepare_execution_attempt_fail(
            execution_attempt_ref=decision.record.execution_attempt_ref,
            failure_code=decision.failure_code,
            failure_message=_INTERRUPTED_FAILURE_MESSAGE,
        )
        terminal = retain_terminal_command(decision.record, prepared)
        journal.replace(decision.record, terminal)
        return InterruptedFailurePending(terminal)
    if type(decision) in {ObserveUntilExpiry, RetainTerminalConflict}:
        if type(decision) is RetainTerminalConflict and type(record) is TerminalPending and record.terminal_hold_action is None:
            action = "do_not_resend" if observed.snapshot.state is AttemptState.EXPIRED else "reconcile_original"
            held = replace(record, terminal_hold_action=action, terminal_observed_state=observed.snapshot.state.value, terminal_hold_description=("The Attempt expired before the exact terminal report was confirmed; do not resend." if action == "do_not_resend" else "The API reports a conflicting terminal state; reconcile the retained exact terminal report before any action."))
            journal.replace(record, held)
            return RetainTerminalConflict(held)
        return decision
    if type(decision) is ReplayTerminal:
        return deliver_terminal(
            api=api,
            journal=journal,
            record=decision.record,
        )
    if type(decision) is RetireResolved:
        _LOG.info(
            'API resolved retained attempt; job=%s attempt=%s state=%s',
            record.job_ref,
            record.execution_attempt_ref,
            observed.snapshot.state.value,
        )
        journal.retire(decision.record)
        return RecoveryResolved(decision.record, observed.snapshot)
    raise AssertionError("NMRPeak recovery received an unsupported restart action")


def _first_in_generation(
    jobs: tuple[JobFeedItem, ...],
    generation: RunGenerationIdentity,
) -> JobFeedItem | None:
    for job in jobs:
        created_at = parse_canonical_utc_timestamp(job.created_at)
        if generation.scope.contains(created_at):
            return job
    return None


def _observation_allows_execution(
    observation: AttemptObservation,
) -> bool:
    return (
        type(observation) is AttemptObserved
        and observation.snapshot.state is AttemptState.IN_PROGRESS
        and observation.snapshot.job_state is JobState.OPEN
    )


def _stopped_execution_outcome(
    journal: AttemptJournalStore,
    record: ActiveAttempt,
    observation: AttemptObservation,
) -> ExecutionCutOff | ExecutionResolved | ObservationLost:
    if type(observation) is AttemptObservationFailed:
        return ObservationLost(record, observation.evidence)
    snapshot = observation.snapshot
    _LOG.info(
        'Generation stopped on API state; job=%s attempt=%s attempt_state=%s job_state=%s',
        record.job_ref,
        record.execution_attempt_ref,
        snapshot.state.value,
        snapshot.job_state.value,
    )
    if snapshot.state is not AttemptState.IN_PROGRESS:
        journal.retire(record)
        return ExecutionResolved(snapshot)
    return ExecutionCutOff(record, snapshot)


def _log_execution_stop_trigger(
    record: ActiveAttempt,
    observation: AttemptObservation,
) -> None:
    if type(observation) is AttemptObserved:
        snapshot = observation.snapshot
        _LOG.warning("%s", render_provider_event(ExecutionStopRequired(
            job_ref=record.job_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            attempt_state=snapshot.state.value,
            job_state=snapshot.job_state.value,
        )))
        return
    evidence = observation.evidence
    _LOG.warning("%s", render_provider_event(ExecutionObservationLost(
        job_ref=record.job_ref,
        execution_attempt_ref=record.execution_attempt_ref,
        evidence_type=type(evidence).__name__,
        **_provider_evidence_facts(evidence),
    )))


def _cancel_and_join_generation(
    session: RunnerSession,
    worker: Thread,
    policy: ObservationPolicy,
) -> None:
    cancellation_error: RunnerSessionRetired | None = None
    try:
        session.cancel()
    except RunnerSessionRetired as error:
        cancellation_error = error
    worker.join(policy.shutdown_join_seconds)
    if worker.is_alive() or cancellation_error is not None:
        failure = ExecutionShutdownFailed(
            "NMRPeak generation cancellation did not reach a confirmed stopped state"
        )
        if worker.is_alive():
            failure.add_note("The NMRPeak generation worker is still running.")
        raise failure from cancellation_error


def _read_failure(
    operation: ProviderOperation,
    outcome: ProviderHttpsOutcome,
) -> ReadFailureEvidence:
    if type(outcome) is ProviderHttpResponse:
        return parse_provider_problem(operation, outcome)
    if type(outcome) in {
        ProviderRequestUnavailable,
        ProviderResponseRejected,
        ProviderTlsRejected,
    }:
        return outcome
    raise TypeError("NMRPeak provider read returned unsupported transport evidence")


def _retain_preparation_failure(
    journal: AttemptJournalStore,
    record: ActiveAttempt,
    policy: PreparationFailurePolicy,
    failure: ClassifiedPreparationFailure,
    reason: str,
    path: str = "",
    route: tuple[str, ...] = (),
) -> InputFailurePending:
    try:
        publication = policy.resolve(failure)
    except FailureContractError as error:
        _LOG.error("%s", render_provider_event(PreparationFailurePolicyDrift(
            job_ref=record.job_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            failure_kind=failure.kind,
            reason=error.reason,
        )))
        raise
    if publication is None:
        _LOG.error("%s", render_provider_event(PreparationFailurePolicyDrift(
            job_ref=record.job_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            failure_kind=failure.kind,
            reason="terminal_failure_has_no_publication",
        )))
        raise FailureContractError("terminal_preparation_failure_has_local_policy")
    prepared = prepare_execution_attempt_fail(
        execution_attempt_ref=record.execution_attempt_ref,
        failure_code=publication.failure_code,
        failure_message=publication.failure_message,
    )
    producer = {
        FailureKind.DIRECT_SOURCE_ISSUE.value: "provider",
        FailureKind.DIRECT_RUNNER_REJECTED.value: "runner",
        FailureKind.MODEL_REPORTED_PROBLEM.value: "interpreter",
        FailureKind.CANDIDATE_ISSUE.value: "interpreter_candidate",
        FailureKind.CANDIDATE_RUNNER_REJECTED.value: "runner",
    }[failure.kind]
    diagnosed = replace(
        record,
        latest_diagnostic=LatestDiagnostic(
            stage="preparation",
            kind=failure.kind,
            producer=producer,
            reason=reason,
            observed_at=datetime.now(UTC).isoformat(),
            path=path or None,
            endpoint_route=route,
        ),
    )
    terminal = retain_terminal_command(diagnosed, prepared)
    journal.replace(record, terminal)
    _LOG.info("%s", render_provider_event(PreparationFailureRetained(
        job_ref=record.job_ref,
        execution_attempt_ref=record.execution_attempt_ref,
        failure_kind=failure.kind,
        failure_code=publication.failure_code,
        reason=reason,
        path=path or "root",
        endpoint_route=route,
    )))
    return InputFailurePending(terminal)


def _recover_input(
    lane: LifecycleLane,
    api: ProviderApiClient,
    record: ActiveAttempt,
) -> RecoveryResumes | InputReadFailed:
    prepared = prepare_job_input_read(
        job_ref=record.job_ref,
        analysis_kind_ref=lane.offering.analysis_kind_ref,
    )
    response = api.send(prepared)
    if type(response) is not ProviderHttpResponse or response.status != 200:
        return InputReadFailed(_read_failure(prepared.operation, response))
    recovered = parse_retained_job_input_read_success(
        prepared,
        response,
        expected_job_ref=record.job_ref,
        expected_input_fingerprint=record.input_fingerprint,
    )
    if type(recovered) is ProviderSuccessRejected:
        return InputReadFailed(recovered)
    return RecoveryResumes(record, recovered.canonical_input)


def _require_generation(
    lane: LifecycleLane,
    record: StartPending | ActiveAttempt | TerminalPending,
    generation: RunGenerationIdentity,
    frozen_generation_id: str,
) -> None:
    if type(generation) is not RunGenerationIdentity:
        raise TypeError("NMRPeak lifecycle requires an exact run generation")
    if generation.analysis_kind_ref != lane.offering.analysis_kind_ref:
        raise ValueError("NMRPeak lifecycle requires the lane-owned analysis kind")
    validate_frozen_generation_id(frozen_generation_id)
    if record.frozen_generation_id != frozen_generation_id:
        raise ValueError("NMRPeak lifecycle resolved the wrong frozen generation")
    expected_attempt_key = derive_provider_attempt_key(
        provider_ref=generation.provider_ref,
        run_generation_fingerprint=run_generation_fingerprint(generation),
        job_ref=record.job_ref,
        input_fingerprint=record.input_fingerprint,
    )
    if record.provider_attempt_key != expected_attempt_key:
        raise ValueError("NMRPeak journal record does not belong to this run generation")
