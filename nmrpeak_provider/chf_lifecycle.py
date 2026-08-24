"""Own the concrete CHF Job admission and Attempt lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field

from .attempt_identity import derive_provider_attempt_key
from .attempt_journal import (
    ActiveAttempt,
    StartPending,
    bind_started_attempt,
    validate_frozen_generation_id,
)
from .attempt_journal_store import AttemptJournalStore
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
    interpret_execution_attempt_start,
)
from .provider_requests import (
    prepare_execution_attempt_start,
    prepare_job_input_read,
    prepare_jobs_list,
)
from .provider_success import (
    AttemptState,
    ExecutionAttemptStarted,
    JobFeedItem,
    ProviderSuccessRejected,
    parse_job_input_read_success,
    parse_jobs_list_success,
)
from .run_generation import (
    RunGenerationIdentity,
    parse_canonical_utc_timestamp,
    run_generation_fingerprint,
)


_FEED_PAGE_LIMIT = 50

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

    if type(generation) is not RunGenerationIdentity:
        raise TypeError("CHF start requires an exact run generation")
    if generation.analysis_kind_ref != CHF_OFFERING.analysis_kind_ref:
        raise ValueError("CHF start requires the product-owned analysis kind")
    if type(record) is not StartPending:
        raise TypeError("CHF start requires a durable pending-start record")
    validate_frozen_generation_id(frozen_generation_id)
    if record.frozen_generation_id != frozen_generation_id:
        raise ValueError("CHF start resolved the wrong frozen generation")
    expected_attempt_key = derive_provider_attempt_key(
        provider_ref=generation.provider_ref,
        run_generation_fingerprint=run_generation_fingerprint(generation),
        job_ref=record.job_ref,
        input_fingerprint=record.input_fingerprint,
    )
    if record.provider_attempt_key != expected_attempt_key:
        raise ValueError("CHF start record does not belong to this run generation")

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


def _first_in_generation(
    jobs: tuple[JobFeedItem, ...],
    generation: RunGenerationIdentity,
) -> JobFeedItem | None:
    for job in jobs:
        created_at = parse_canonical_utc_timestamp(job.created_at)
        if generation.scope.contains(created_at):
            return job
    return None


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
