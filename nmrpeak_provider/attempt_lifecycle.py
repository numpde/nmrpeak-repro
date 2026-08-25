"""Own one fixed NMRPeak lane's Job admission and Attempt lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
from threading import Event, Thread
from typing import TYPE_CHECKING

from .attempt_identity import derive_provider_attempt_key
from .attempt_journal import (
    ActiveAttempt,
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
from .generation_runtime import GenerationRuntime
from .lifecycle_lane import LifecycleLane
from .interpreter import (
    InterpretationRejected,
    InterpreterUnavailable,
    ReportedInputProblem,
)
from .runner_session import (
    RunnerInputRejected,
    RunnerSession,
    RunnerSessionRetired,
    GeneratedRunnerCandidates,
    ValidatedRunnerRequest,
)
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
from .product_input import InputRejected, parse_job_input
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

if TYPE_CHECKING:
    from .input_interpreter import InputInterpreter


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
    """The fixed input rejection is durable and awaits API delivery."""

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


TerminalDeliveryOutcome = (
    TerminalDelivered
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

    resolved = runtime.resolve(record)
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

    try:
        model_input = parse_job_input(canonical_input, lane.offering)
    except InputRejected:
        model_input = None

    progress = prepare_execution_attempt_progress(
        execution_attempt_ref=record.execution_attempt_ref,
        phase="preparing",
        condition_code=None,
    )
    progress_outcome = interpret_execution_attempt_progress(
        progress,
        api.send(progress),
    )
    if type(progress_outcome) is not AttemptMutationCommitted:
        return progress_outcome

    if model_input is not None:
        validated = session.validate(
            execution_attempt_ref=record.execution_attempt_ref,
            provider_attempt_key=record.provider_attempt_key,
            model_input=lane.bind_runner_input(model_input),
        )
        if type(validated) is RunnerInputRejected:
            return _retain_input_rejection(journal, record)
    else:
        try:
            validated = interpreter.validate_freeform_input(
                source=canonical_input,
                lane=lane,
                session=session,
                execution_attempt_ref=record.execution_attempt_ref,
                provider_attempt_key=record.provider_attempt_key,
            )
        except InputRejected:
            return _retain_input_rejection(journal, record)
        except ReportedInputProblem as problem:
            return _retain_input_rejection(journal, record, problem.message)
        except InterpretationRejected as rejection:
            return _retain_input_rejection(journal, record, rejection.diagnostic)
        except InterpreterUnavailable as unavailable:
            return InputInterpretationUnavailable(unavailable)
    return PreparedForExecution(record, validated)


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

    running = prepare_execution_attempt_progress(
        execution_attempt_ref=record.execution_attempt_ref,
        phase="running",
        condition_code=None,
    )
    running_outcome = interpret_execution_attempt_progress(
        running,
        api.send(running),
    )
    if type(running_outcome) is not AttemptMutationCommitted:
        session.cancel()
        return running_outcome

    entered = mark_execution_entered(record)
    try:
        journal.replace(record, entered)
    except BaseException as error:
        try:
            session.cancel()
        except RunnerSessionRetired:
            error.add_note(
                "The validated NMRPeak session also failed to stop before generation."
            )
        raise

    initial_observation = observe_attempt(api=api, record=entered)
    if not _observation_allows_execution(initial_observation):
        session.cancel()
        return _stopped_execution_outcome(journal, entered, initial_observation)

    work = _GenerationWork()
    worker = Thread(
        target=work.run,
        args=(session, prepared.request),
        name="nmrpeak-generation",
    )
    worker.start()
    try:
        while not work.done.is_set():
            current = observe_attempt(api=api, record=entered)
            if not _observation_allows_execution(current):
                _cancel_and_join_generation(session, worker, observation)
                return _stopped_execution_outcome(journal, entered, current)
            work.done.wait(observation.poll_interval_seconds)

        worker.join(observation.shutdown_join_seconds)
        if worker.is_alive():
            raise ExecutionShutdownFailed(
                "NMRPeak generation signalled completion but its worker did not stop"
            )
        final_observation = observe_attempt(api=api, record=entered)
        if not _observation_allows_execution(final_observation):
            session.cancel()
            return _stopped_execution_outcome(journal, entered, final_observation)
        if work.error is not None:
            raise work.error
        if work.candidates is None:
            raise AssertionError(
                "NMRPeak generation finished without candidates or an error"
            )
        return CandidatesGenerated(entered, work.candidates, session)
    except BaseException as error:
        if worker.is_alive():
            try:
                _cancel_and_join_generation(session, worker, observation)
            except ExecutionShutdownFailed:
                error.add_note(
                    "The NMRPeak generation worker also failed to stop after the error."
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
    except RunnerResultRejected as error:
        try:
            generated.session.cancel()
        except RunnerSessionRetired:
            error.add_note(
                "The rejected NMRPeak result's runner session also failed to stop."
            )
        raise
    prepared = prepare_execution_attempt_complete(
        execution_attempt_ref=record.execution_attempt_ref,
        result_schema_id=RESULT_SCHEMA_ID,
        canonical_result=result,
    )
    terminal = retain_terminal_command(record, prepared)
    journal.replace(record, terminal)
    return CompletionPending(terminal)


def deliver_terminal(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    record: TerminalPending,
) -> TerminalDeliveryOutcome:
    """Send one exact retained terminal command and retire only its receipt."""

    if type(record) is not TerminalPending:
        raise TypeError("NMRPeak terminal delivery requires a retained command")
    prepared = prepared_terminal_replay(record)
    sent = api.send(prepared)
    outcome = (
        interpret_execution_attempt_complete(prepared, sent)
        if record.terminal_operation is TerminalOperation.COMPLETE
        else interpret_execution_attempt_fail(prepared, sent)
    )
    if type(outcome) is not AttemptMutationCommitted:
        return outcome
    journal.retire(record)
    return TerminalDelivered(outcome.receipt)


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
    resolved = runtime.resolve(record)
    if type(record) is StartPending:
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

    observed = observe_attempt(api=api, record=record)
    if type(observed) is AttemptObservationFailed:
        return observed
    decision = decide_restart(record, observed.snapshot)
    if type(decision) is ResumePreExecution:
        return _recover_input(resolved.lane, api, decision.record)
    if type(decision) is PublishInterruptedFailure:
        prepared = prepare_execution_attempt_fail(
            execution_attempt_ref=decision.record.execution_attempt_ref,
            failure_code=decision.failure_code,
            failure_message=_INTERRUPTED_FAILURE_MESSAGE,
        )
        terminal = retain_terminal_command(decision.record, prepared)
        journal.replace(decision.record, terminal)
        return InterruptedFailurePending(terminal)
    if type(decision) in {ObserveUntilExpiry, RetainTerminalConflict}:
        return decision
    if type(decision) is ReplayTerminal:
        return deliver_terminal(
            api=api,
            journal=journal,
            record=decision.record,
        )
    if type(decision) is RetireResolved:
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
    if snapshot.state is not AttemptState.IN_PROGRESS:
        journal.retire(record)
        return ExecutionResolved(snapshot)
    return ExecutionCutOff(record, snapshot)


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


def _retain_input_rejection(
    journal: AttemptJournalStore,
    record: ActiveAttempt,
    message: str = InputRejected.public_message,
) -> InputFailurePending:
    prepared = prepare_execution_attempt_fail(
        execution_attempt_ref=record.execution_attempt_ref,
        failure_code="input_rejected",
        failure_message=message,
    )
    terminal = retain_terminal_command(record, prepared)
    journal.replace(record, terminal)
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
