"""Own the concrete CHF Job admission and Attempt lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
from threading import Event, Thread

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
from .chf_binding import bind_chf_runner_input
from .runner_session import (
    RunnerInputRejected,
    RunnerSession,
    RunnerSessionRetired,
    GeneratedRunnerCandidates,
    ValidatedRunnerRequest,
)
from .product import CHF_OFFERING
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


_FEED_PAGE_LIMIT = 50
_INTERRUPTED_FAILURE_MESSAGE = (
    "The provider process was interrupted before this execution completed."
)

ChfReadFailureEvidence = (
    ProviderProblem
    | ProviderProblemRejected
    | ProviderRequestUnavailable
    | ProviderResponseRejected
    | ProviderTlsRejected
    | ProviderSuccessRejected
)


@dataclass(frozen=True, slots=True)
class ChfJobAdmitted:
    """One durable start obligation and its transient exact input bytes."""

    record: StartPending
    canonical_input: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChfPageExhausted:
    """No Job on this page belongs to the admitted run-generation window."""

    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ChfFeedReadFailed:
    """The selected CHF Job page did not yield an admitted response."""

    evidence: ChfReadFailureEvidence


@dataclass(frozen=True, slots=True)
class ChfInputReadFailed:
    """The selected Job's immutable input did not yield admitted bytes."""

    evidence: ChfReadFailureEvidence


ChfAdmissionOutcome = (
    ChfJobAdmitted
    | ChfPageExhausted
    | ChfFeedReadFailed
    | ChfInputReadFailed
)


@dataclass(frozen=True, slots=True)
class ChfStartContinues:
    """The API Attempt and its local pre-execution record are both durable."""

    record: ActiveAttempt


@dataclass(frozen=True, slots=True)
class ChfStartResolved:
    """An idempotent start replay found the Attempt already terminal."""

    receipt: ExecutionAttemptStarted


ChfStartOutcome = (
    ChfStartContinues
    | ChfStartResolved
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
)


@dataclass(frozen=True, slots=True)
class ChfPreparedForExecution:
    """A PRE_EXECUTION Attempt and its session-owned validation capability."""

    record: ActiveAttempt
    request: ValidatedRunnerRequest = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChfInputFailurePending:
    """The fixed input rejection is durable and awaits API delivery."""

    record: TerminalPending


ChfPreExecutionOutcome = (
    ChfPreparedForExecution
    | ChfInputFailurePending
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
)


@dataclass(frozen=True, slots=True)
class ChfAttemptObserved:
    """One authoritative point snapshot bound to the retained Attempt and Job."""

    snapshot: ExecutionAttemptSnapshot


@dataclass(frozen=True, slots=True)
class ChfAttemptObservationFailed:
    """Server A did not yield an admitted point snapshot."""

    evidence: ChfReadFailureEvidence


ChfAttemptObservation = ChfAttemptObserved | ChfAttemptObservationFailed


@dataclass(frozen=True, slots=True)
class ChfObservationPolicy:
    """Bound coordinator waits around fail-closed live point observation."""

    poll_interval_seconds: float
    shutdown_join_seconds: float

    def __post_init__(self) -> None:
        for value in (self.poll_interval_seconds, self.shutdown_join_seconds):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    "CHF observation waits must be positive finite seconds"
                )


@dataclass(frozen=True, slots=True)
class ChfCandidatesGenerated:
    """Candidates completed while Server A still admitted local execution."""

    record: ActiveAttempt
    candidates: GeneratedRunnerCandidates = field(repr=False)
    session: RunnerSession = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChfCompletionPending:
    """The exact canonical completion command is durable for delivery."""

    record: TerminalPending


@dataclass(frozen=True, slots=True)
class ChfTerminalDelivered:
    """A command-bound receipt and durable journal retirement both succeeded."""

    receipt: ExecutionAttemptCompleted | ExecutionAttemptFailed


ChfTerminalDeliveryOutcome = (
    ChfTerminalDelivered
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
)


@dataclass(frozen=True, slots=True)
class ChfRecoveryResumes:
    """A retained pre-execution Attempt and its re-read exact input bytes."""

    record: ActiveAttempt
    canonical_input: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChfInterruptedFailurePending:
    """A fixed restart failure is durable and awaits exact delivery."""

    record: TerminalPending


@dataclass(frozen=True, slots=True)
class ChfRecoveryResolved:
    """An authoritative terminal state retired one local obligation."""

    record: ActiveAttempt | TerminalPending
    snapshot: ExecutionAttemptSnapshot


ChfRecoveryOutcome = (
    ChfStartOutcome
    | ChfRecoveryResumes
    | ChfInterruptedFailurePending
    | ChfRecoveryResolved
    | ObserveUntilExpiry
    | RetainTerminalConflict
    | ChfTerminalDeliveryOutcome
    | ChfAttemptObservationFailed
    | ChfInputReadFailed
)


@dataclass(frozen=True, slots=True)
class ChfExecutionCutOff:
    """The Job closed or was cancelled while its Attempt remained live."""

    record: ActiveAttempt
    snapshot: ExecutionAttemptSnapshot


@dataclass(frozen=True, slots=True)
class ChfExecutionResolved:
    """Server A reached a terminal Attempt state and the journal was retired."""

    snapshot: ExecutionAttemptSnapshot


@dataclass(frozen=True, slots=True)
class ChfObservationLost:
    """Execution stopped because authoritative visibility was lost."""

    record: ActiveAttempt
    evidence: ChfReadFailureEvidence


class ChfExecutionShutdownFailed(RuntimeError):
    """Process-fatal: a generation worker may still be running after cancellation."""


ChfExecutionOutcome = (
    ChfCandidatesGenerated
    | ChfExecutionCutOff
    | ChfExecutionResolved
    | ChfObservationLost
    | AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
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


def admit_next_chf_job(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    generation: RunGenerationIdentity,
    frozen_generation_id: str,
    cursor: str | None = None,
) -> ChfAdmissionOutcome:
    """Read and durably admit the first in-window Job from one CHF page."""

    if type(generation) is not RunGenerationIdentity:
        raise TypeError("CHF admission requires an exact run generation")
    if generation.analysis_kind_ref != CHF_OFFERING.analysis_kind_ref:
        raise ValueError("CHF admission requires the product-owned analysis kind")
    validate_frozen_generation_id(frozen_generation_id)

    feed_request = prepare_jobs_list(
        analysis_kind_ref=CHF_OFFERING.analysis_kind_ref,
        has_provider_execution_attempt=False,
        limit=_FEED_PAGE_LIMIT,
        cursor=cursor,
    )
    feed_response = api.send(feed_request)
    if type(feed_response) is not ProviderHttpResponse or feed_response.status != 200:
        return ChfFeedReadFailed(_read_failure(feed_request.operation, feed_response))
    page = parse_jobs_list_success(feed_request, feed_response)
    if type(page) is ProviderSuccessRejected:
        return ChfFeedReadFailed(page)

    selected_job = _first_in_generation(page.jobs, generation)
    if selected_job is None:
        return ChfPageExhausted(page.next_cursor)

    input_request = prepare_job_input_read(
        job_ref=selected_job.job_ref,
        analysis_kind_ref=CHF_OFFERING.analysis_kind_ref,
    )
    input_response = api.send(input_request)
    if type(input_response) is not ProviderHttpResponse or input_response.status != 200:
        return ChfInputReadFailed(_read_failure(input_request.operation, input_response))
    job_input = parse_job_input_read_success(
        input_request,
        input_response,
        expected_job=selected_job,
    )
    if type(job_input) is ProviderSuccessRejected:
        return ChfInputReadFailed(job_input)

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
    return ChfJobAdmitted(record=record, canonical_input=job_input.canonical_input)


def start_chf_attempt(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    generation: RunGenerationIdentity,
    frozen_generation_id: str,
    record: StartPending,
) -> ChfStartOutcome:
    """Send one exact start and persist the command-bound server outcome."""

    if type(record) is not StartPending:
        raise TypeError("CHF start requires a durable pending-start record")
    _require_chf_generation(record, generation, frozen_generation_id)

    prepared = prepare_execution_attempt_start(
        job_ref=record.job_ref,
        provider_attempt_key=record.provider_attempt_key,
    )
    outcome = interpret_execution_attempt_start(
        prepared,
        api.send(prepared),
        expected_provider_ref=generation.provider_ref,
        expected_analysis_kind_ref=CHF_OFFERING.analysis_kind_ref,
    )
    if type(outcome) is not AttemptMutationCommitted:
        return outcome
    receipt = outcome.receipt
    if receipt.state is AttemptState.IN_PROGRESS:
        active = bind_started_attempt(record, receipt)
        journal.replace(record, active)
        return ChfStartContinues(active)
    journal.retire(record)
    return ChfStartResolved(receipt)


def prepare_chf_execution(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    session: RunnerSession,
    record: ActiveAttempt,
    canonical_input: bytes,
) -> ChfPreExecutionOutcome:
    """Validate one active CHF Attempt without entering model execution."""

    if type(record) is not ActiveAttempt:
        raise TypeError("CHF preparation requires an active Attempt record")
    if record.local_phase is not LocalExecutionPhase.PRE_EXECUTION:
        raise ValueError("CHF preparation requires a pre-execution Attempt")
    if type(canonical_input) is not bytes:
        raise TypeError("CHF preparation requires exact input bytes")
    if "sha256:" + sha256(canonical_input).hexdigest() != record.input_fingerprint:
        raise ValueError("CHF preparation input does not match the Attempt journal")

    try:
        model_input = parse_job_input(canonical_input, CHF_OFFERING)
    except InputRejected:
        return _retain_chf_input_rejection(journal, record)
    runner_input = bind_chf_runner_input(model_input)

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

    validated = session.validate(
        execution_attempt_ref=record.execution_attempt_ref,
        provider_attempt_key=record.provider_attempt_key,
        model_input=runner_input,
    )
    if type(validated) is RunnerInputRejected:
        return _retain_chf_input_rejection(journal, record)
    return ChfPreparedForExecution(record, validated)


def observe_chf_attempt(
    *,
    api: ProviderApiClient,
    record: ActiveAttempt | TerminalPending,
) -> ChfAttemptObservation:
    """Read Server A's current state for one retained CHF Attempt."""

    if type(record) not in {ActiveAttempt, TerminalPending}:
        raise TypeError("CHF observation requires a retained Attempt reference")
    prepared = prepare_execution_attempt_read(record.execution_attempt_ref)
    response = api.send(prepared)
    if type(response) is not ProviderHttpResponse or response.status != 200:
        return ChfAttemptObservationFailed(
            _read_failure(prepared.operation, response)
        )
    snapshot = parse_execution_attempt_read_success(
        prepared,
        response,
        expected_job_ref=record.job_ref,
    )
    if type(snapshot) is ProviderSuccessRejected:
        return ChfAttemptObservationFailed(snapshot)
    return ChfAttemptObserved(snapshot)


def execute_prepared_chf(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    session: RunnerSession,
    prepared: ChfPreparedForExecution,
    observation: ChfObservationPolicy,
) -> ChfExecutionOutcome:
    """Generate only while bounded point reads keep the Attempt executable."""

    if type(prepared) is not ChfPreparedForExecution:
        raise TypeError("CHF execution requires a prepared runner request")
    if type(observation) is not ChfObservationPolicy:
        raise TypeError("CHF execution requires an admitted observation policy")
    record = prepared.record
    if record.local_phase is not LocalExecutionPhase.PRE_EXECUTION:
        raise ValueError("CHF execution requires a pre-execution Attempt")

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
                "The validated CHF session also failed to stop before generation."
            )
        raise

    initial_observation = observe_chf_attempt(api=api, record=entered)
    if not _observation_allows_execution(initial_observation):
        session.cancel()
        return _stopped_execution_outcome(journal, entered, initial_observation)

    work = _GenerationWork()
    worker = Thread(
        target=work.run,
        args=(session, prepared.request),
        name="nmrpeak-chf-generation",
    )
    worker.start()
    try:
        while not work.done.is_set():
            current = observe_chf_attempt(api=api, record=entered)
            if not _observation_allows_execution(current):
                _cancel_and_join_generation(session, worker, observation)
                return _stopped_execution_outcome(journal, entered, current)
            work.done.wait(observation.poll_interval_seconds)

        worker.join(observation.shutdown_join_seconds)
        if worker.is_alive():
            raise ChfExecutionShutdownFailed(
                "CHF generation signalled completion but its worker did not stop"
            )
        final_observation = observe_chf_attempt(api=api, record=entered)
        if not _observation_allows_execution(final_observation):
            session.cancel()
            return _stopped_execution_outcome(journal, entered, final_observation)
        if work.error is not None:
            raise work.error
        if work.candidates is None:
            raise AssertionError(
                "CHF generation finished without candidates or an error"
            )
        return ChfCandidatesGenerated(entered, work.candidates, session)
    except BaseException as error:
        if worker.is_alive():
            try:
                _cancel_and_join_generation(session, worker, observation)
            except ChfExecutionShutdownFailed:
                error.add_note(
                    "The CHF generation worker also failed to stop after the error."
                )
        raise


def select_chf_completion(
    *,
    journal: AttemptJournalStore,
    generated: ChfCandidatesGenerated,
) -> ChfCompletionPending:
    """Durably select one canonical completion without sending it."""

    if type(generated) is not ChfCandidatesGenerated:
        raise TypeError("CHF completion requires generated candidates")
    record = generated.record
    if record.local_phase is not LocalExecutionPhase.EXECUTION_ENTERED:
        raise ValueError("CHF completion requires an entered execution")
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
                "The rejected CHF result's runner session also failed to stop."
            )
        raise
    prepared = prepare_execution_attempt_complete(
        execution_attempt_ref=record.execution_attempt_ref,
        result_schema_id=RESULT_SCHEMA_ID,
        canonical_result=result,
    )
    terminal = retain_terminal_command(record, prepared)
    journal.replace(record, terminal)
    return ChfCompletionPending(terminal)


def deliver_chf_terminal(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    record: TerminalPending,
) -> ChfTerminalDeliveryOutcome:
    """Send one exact retained terminal command and retire only its receipt."""

    if type(record) is not TerminalPending:
        raise TypeError("CHF terminal delivery requires a retained command")
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
    return ChfTerminalDelivered(outcome.receipt)


def reconcile_chf_record(
    *,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    generation: RunGenerationIdentity,
    frozen_generation_id: str,
    record: StartPending | ActiveAttempt | TerminalPending,
) -> ChfRecoveryOutcome:
    """Apply the existing restart decision to one durable CHF obligation."""

    if type(record) not in {StartPending, ActiveAttempt, TerminalPending}:
        raise TypeError("CHF recovery requires an exact journal record")
    if type(record) is StartPending:
        decision = decide_restart(record, None)
        if type(decision) is not ReplayStart:
            raise AssertionError("Pending CHF start produced an unsupported restart action")
        return start_chf_attempt(
            api=api,
            journal=journal,
            generation=generation,
            frozen_generation_id=frozen_generation_id,
            record=decision.record,
        )

    _require_chf_generation(record, generation, frozen_generation_id)
    observed = observe_chf_attempt(api=api, record=record)
    if type(observed) is ChfAttemptObservationFailed:
        return observed
    decision = decide_restart(record, observed.snapshot)
    if type(decision) is ResumePreExecution:
        return _recover_chf_input(api, decision.record)
    if type(decision) is PublishInterruptedFailure:
        prepared = prepare_execution_attempt_fail(
            execution_attempt_ref=decision.record.execution_attempt_ref,
            failure_code=decision.failure_code,
            failure_message=_INTERRUPTED_FAILURE_MESSAGE,
        )
        terminal = retain_terminal_command(decision.record, prepared)
        journal.replace(decision.record, terminal)
        return ChfInterruptedFailurePending(terminal)
    if type(decision) in {ObserveUntilExpiry, RetainTerminalConflict}:
        return decision
    if type(decision) is ReplayTerminal:
        return deliver_chf_terminal(
            api=api,
            journal=journal,
            record=decision.record,
        )
    if type(decision) is RetireResolved:
        journal.retire(decision.record)
        return ChfRecoveryResolved(decision.record, observed.snapshot)
    raise AssertionError("CHF recovery received an unsupported restart action")


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
    observation: ChfAttemptObservation,
) -> bool:
    return (
        type(observation) is ChfAttemptObserved
        and observation.snapshot.state is AttemptState.IN_PROGRESS
        and observation.snapshot.job_state is JobState.OPEN
    )


def _stopped_execution_outcome(
    journal: AttemptJournalStore,
    record: ActiveAttempt,
    observation: ChfAttemptObservation,
) -> ChfExecutionCutOff | ChfExecutionResolved | ChfObservationLost:
    if type(observation) is ChfAttemptObservationFailed:
        return ChfObservationLost(record, observation.evidence)
    snapshot = observation.snapshot
    if snapshot.state is not AttemptState.IN_PROGRESS:
        journal.retire(record)
        return ChfExecutionResolved(snapshot)
    return ChfExecutionCutOff(record, snapshot)


def _cancel_and_join_generation(
    session: RunnerSession,
    worker: Thread,
    policy: ChfObservationPolicy,
) -> None:
    cancellation_error: RunnerSessionRetired | None = None
    try:
        session.cancel()
    except RunnerSessionRetired as error:
        cancellation_error = error
    worker.join(policy.shutdown_join_seconds)
    if worker.is_alive() or cancellation_error is not None:
        failure = ChfExecutionShutdownFailed(
            "CHF generation cancellation did not reach a confirmed stopped state"
        )
        if worker.is_alive():
            failure.add_note("The CHF generation worker is still running.")
        raise failure from cancellation_error


def _read_failure(
    operation: ProviderOperation,
    outcome: ProviderHttpsOutcome,
) -> ChfReadFailureEvidence:
    if type(outcome) is ProviderHttpResponse:
        return parse_provider_problem(operation, outcome)
    if type(outcome) in {
        ProviderRequestUnavailable,
        ProviderResponseRejected,
        ProviderTlsRejected,
    }:
        return outcome
    raise TypeError("CHF provider read returned unsupported transport evidence")


def _retain_chf_input_rejection(
    journal: AttemptJournalStore,
    record: ActiveAttempt,
) -> ChfInputFailurePending:
    prepared = prepare_execution_attempt_fail(
        execution_attempt_ref=record.execution_attempt_ref,
        failure_code="input_rejected",
        failure_message=InputRejected.public_message,
    )
    terminal = retain_terminal_command(record, prepared)
    journal.replace(record, terminal)
    return ChfInputFailurePending(terminal)


def _recover_chf_input(
    api: ProviderApiClient,
    record: ActiveAttempt,
) -> ChfRecoveryResumes | ChfInputReadFailed:
    prepared = prepare_job_input_read(
        job_ref=record.job_ref,
        analysis_kind_ref=CHF_OFFERING.analysis_kind_ref,
    )
    response = api.send(prepared)
    if type(response) is not ProviderHttpResponse or response.status != 200:
        return ChfInputReadFailed(_read_failure(prepared.operation, response))
    recovered = parse_retained_job_input_read_success(
        prepared,
        response,
        expected_job_ref=record.job_ref,
        expected_input_fingerprint=record.input_fingerprint,
    )
    if type(recovered) is ProviderSuccessRejected:
        return ChfInputReadFailed(recovered)
    return ChfRecoveryResumes(record, recovered.canonical_input)


def _require_chf_generation(
    record: StartPending | ActiveAttempt | TerminalPending,
    generation: RunGenerationIdentity,
    frozen_generation_id: str,
) -> None:
    if type(generation) is not RunGenerationIdentity:
        raise TypeError("CHF lifecycle requires an exact run generation")
    if generation.analysis_kind_ref != CHF_OFFERING.analysis_kind_ref:
        raise ValueError("CHF lifecycle requires the product-owned analysis kind")
    validate_frozen_generation_id(frozen_generation_id)
    if record.frozen_generation_id != frozen_generation_id:
        raise ValueError("CHF lifecycle resolved the wrong frozen generation")
    expected_attempt_key = derive_provider_attempt_key(
        provider_ref=generation.provider_ref,
        run_generation_fingerprint=run_generation_fingerprint(generation),
        job_ref=record.job_ref,
        input_fingerprint=record.input_fingerprint,
    )
    if record.provider_attempt_key != expected_attempt_key:
        raise ValueError("CHF journal record does not belong to this run generation")
