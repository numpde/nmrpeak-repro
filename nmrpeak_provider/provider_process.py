"""Serve the fixed HF and CHF lanes under one bounded process owner."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from threading import Event, Thread
import time
from typing import TYPE_CHECKING, Callable

from .attempt_inventory import (
    AttemptInventoryRejected,
    AttemptInventoryReadFailed,
    read_attempt_inventory,
    validate_startup_inventory,
)
from .attempt_journal import AttemptJournalRecord
from .attempt_journal_store import (
    AttemptJournalAdmissionRejected,
    AttemptJournalStore,
)
from .attempt_lifecycle import (
    AttemptObservationFailed,
    FeedReadFailed,
    InputInterpretationUnavailable,
    InputReadFailed,
    JobAdmitted,
    ObservationPolicy,
    PageExhausted,
    admit_next_job,
    run_admitted_job,
    run_recovery_record,
)
from .generation_runtime import GenerationLane, GenerationRuntime
from .provider_api import ProviderApiClient
from .provider_outcomes import (
    AttemptMutationCommitPossible,
    AttemptMutationNotCommitted,
)
from .provider_problems import (
    ProviderProblem,
    ProviderProblemRejected,
    parse_provider_problem,
)
from .provider_https import (
    ProviderHttpResponse,
    ProviderOperation,
    ProviderRequestUnavailable,
    ProviderResponseRejected,
    ProviderTlsRejected,
)
from .provider_requests import _PreparedProviderRequest
from .provider_success import (
    ProviderHelloAccepted,
    ProviderSuccessRejected,
    parse_provider_hello_success,
)
from .runner_session import RunnerSession, RunnerSessionRetired

if TYPE_CHECKING:
    from .input_interpreter import InputInterpreter


_LOG = logging.getLogger(__name__)
_MAX_REMOTE_RETRY_SECONDS = 300.0


class ProviderLaneFailed(RuntimeError):
    """One fixed lane stopped before the provider process requested shutdown."""


class ProviderShutdownFailed(RuntimeError):
    """At least one fixed lane remained live after forced bounded shutdown."""


@dataclass(frozen=True, slots=True)
class ProviderProcessPolicy:
    """The bounded waits and inventory extent owned by one provider process."""

    feed_interval_seconds: float
    hello_interval_seconds: float
    shutdown_drain_seconds: float
    forced_join_seconds: float
    inventory_maximum_pages: int
    observation: ObservationPolicy

    def __post_init__(self) -> None:
        for value in (
            self.feed_interval_seconds,
            self.hello_interval_seconds,
            self.shutdown_drain_seconds,
            self.forced_join_seconds,
        ):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("NMRPeak process waits must be positive finite seconds")
        if type(self.inventory_maximum_pages) is not int or self.inventory_maximum_pages < 1:
            raise ValueError("NMRPeak inventory page bound must be positive")
        if type(self.observation) is not ObservationPolicy:
            raise TypeError("NMRPeak process requires an admitted observation policy")


@dataclass(frozen=True, slots=True)
class _LaneOwner:
    generation: GenerationLane
    session: RunnerSession


@dataclass(slots=True)
class _LaneWork:
    owner: _LaneOwner
    finished: Event
    error: BaseException | None = None

    def run(
        self,
        *,
        runtime: GenerationRuntime,
        api: ProviderApiClient,
        journal: AttemptJournalStore,
        interpreter: InputInterpreter,
        policy: ProviderProcessPolicy,
        stop: Event,
    ) -> None:
        try:
            _run_lane(
                runtime=runtime,
                api=api,
                journal=journal,
                interpreter=interpreter,
                policy=policy,
                stop=stop,
                owner=self.owner,
            )
        except BaseException as error:
            self.error = error
        finally:
            self.finished.set()


def run_provider_process(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    interpreter: InputInterpreter,
    hf_session: RunnerSession,
    chf_session: RunnerSession,
    hello: _PreparedProviderRequest,
    policy: ProviderProcessPolicy,
    stop: Event,
    on_ready: Callable[[], None],
) -> None:
    """Recover startup state, then serve exactly two sibling lane owners."""

    if type(runtime) is not GenerationRuntime:
        raise TypeError("NMRPeak process requires one admitted generation runtime")
    if type(hf_session) is not RunnerSession or type(chf_session) is not RunnerSession:
        raise TypeError("NMRPeak process requires both admitted runner sessions")
    if type(policy) is not ProviderProcessPolicy:
        raise TypeError("NMRPeak process requires an admitted process policy")
    if (
        type(hello) is not _PreparedProviderRequest
        or hello.operation is not ProviderOperation.PROVIDER_HELLO
    ):
        raise TypeError("NMRPeak process requires one prepared hello snapshot")
    if not isinstance(stop, Event):
        raise TypeError("NMRPeak process requires one stop event")
    if not callable(on_ready):
        raise TypeError("NMRPeak process requires one readiness publisher")

    owners = (
        _LaneOwner(runtime.hf, hf_session),
        _LaneOwner(runtime.chf, chf_session),
    )
    if stop.is_set():
        _retire_sessions(owners)
        return

    started = False
    primary_error: BaseException | None = None
    try:
        for owner in owners:
            if owner.session.result_facts != owner.generation.result_facts:
                raise ValueError(
                    "NMRPeak process received a runner session for another lane generation"
                )
        _recover_startup(
            runtime=runtime,
            api=api,
            journal=journal,
            interpreter=interpreter,
            owners=owners,
            policy=policy,
            stop=stop,
        )
        if stop.is_set():
            return

        provider_ref = runtime.hf.generation.provider_ref
        _await_initial_hello(
            api=api,
            prepared=hello,
            provider_ref=provider_ref,
            policy=policy,
            stop=stop,
        )
        if stop.is_set():
            return
        next_hello_at = time.monotonic() + policy.hello_interval_seconds
        hello_retry_seconds = _initial_retry_delay(policy.feed_interval_seconds)
        hello_outage_active = False

        finished = Event()
        works = tuple(_LaneWork(owner, finished) for owner in owners)
        threads = tuple(
            Thread(
                target=work.run,
                kwargs={
                    "runtime": runtime,
                    "api": api,
                    "journal": journal,
                    "interpreter": interpreter,
                    "policy": policy,
                    "stop": stop,
                },
                name=f"nmrpeak-{work.owner.generation.lane.offering.implementation_ref}",
            )
            for work in works
        )
        started_threads: list[Thread] = []
        try:
            for thread in threads:
                thread.start()
                started_threads.append(thread)
        except BaseException as error:
            if started_threads:
                started = True
                stop.set()
                cancellation_errors: list[RunnerSessionRetired] = []
                for owner in owners:
                    try:
                        owner.session.cancel()
                    except RunnerSessionRetired as cancellation_error:
                        cancellation_errors.append(cancellation_error)
                _join_threads(tuple(started_threads), policy.forced_join_seconds)
                if cancellation_errors:
                    error.add_note(
                        "Partial process startup could not confirm every session closure."
                    )
                if any(thread.is_alive() for thread in started_threads):
                    error.add_note(
                        "A partially started NMRPeak lane remained live after cancellation."
                    )
            raise
        started = True
        coordination_error: BaseException | None = None
        try:
            on_ready()
        except BaseException as error:
            coordination_error = error
            stop.set()
        while not stop.is_set():
            until_hello = max(0.0, next_hello_at - time.monotonic())
            if finished.wait(min(policy.feed_interval_seconds, until_hello)):
                break
            if stop.is_set():
                break
            if time.monotonic() >= next_hello_at:
                hello_failure = _publish_hello(
                    api=api,
                    prepared=hello,
                    provider_ref=provider_ref,
                )
                if hello_failure is None:
                    if hello_outage_active:
                        _LOG.info("Provider Hello recovered")
                    hello_outage_active = False
                    hello_retry_seconds = _initial_retry_delay(
                        policy.feed_interval_seconds
                    )
                    next_hello_at = (
                        time.monotonic() + policy.hello_interval_seconds
                    )
                else:
                    if not hello_outage_active:
                        _LOG.warning(
                            "Provider Hello is unavailable; retrying: %s",
                            _remote_evidence_message(hello_failure),
                        )
                    hello_outage_active = True
                    next_hello_at = time.monotonic() + hello_retry_seconds
                    hello_retry_seconds = _next_retry_delay(hello_retry_seconds)
        if finished.is_set() and not stop.is_set():
            stop.set()

        _join_threads(threads, policy.shutdown_drain_seconds)
        live = tuple(thread for thread in threads if thread.is_alive())
        cancellation_errors: list[RunnerSessionRetired] = []
        if live:
            for owner in owners:
                try:
                    owner.session.cancel()
                except RunnerSessionRetired as error:
                    cancellation_errors.append(error)
            _join_threads(threads, policy.forced_join_seconds)
        live = tuple(thread for thread in threads if thread.is_alive())
        if coordination_error is not None:
            if live:
                raise ProviderShutdownFailed(
                    "NMRPeak lane threads did not stop after session cancellation"
                ) from coordination_error
            if cancellation_errors:
                raise ProviderShutdownFailed(
                    "NMRPeak forced shutdown could not confirm every session closure"
                ) from cancellation_errors[0]
            for work in works:
                if work.error is not None:
                    lane = work.owner.generation.lane.offering.implementation_ref
                    coordination_error.add_note(
                        f"The {lane} lane also failed during coordinated shutdown."
                    )
            raise coordination_error
        for work in works:
            if work.error is not None:
                lane = work.owner.generation.lane.offering.implementation_ref
                failure = ProviderLaneFailed(
                    f"The {lane} provider lane stopped unexpectedly, so coordinated "
                    "provider shutdown began."
                )
                if live:
                    failure.add_note(
                        "A sibling lane remained live after forced session cancellation."
                    )
                if cancellation_errors:
                    failure.add_note(
                        "Forced shutdown could not confirm every session closure."
                    )
                raise failure from work.error
        if live:
            failure = ProviderShutdownFailed(
                "NMRPeak lane threads did not stop after session cancellation"
            )
            for error in cancellation_errors:
                failure.add_note(str(error))
            raise failure
        if cancellation_errors:
            raise ProviderShutdownFailed(
                "NMRPeak forced shutdown could not confirm every session closure"
            ) from cancellation_errors[0]
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if not started:
            try:
                _retire_sessions(owners)
            except RunnerSessionRetired as error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    "Provider startup failure was followed by a runner retirement failure."
                )


def _publish_hello(
    *,
    api: ProviderApiClient,
    prepared: _PreparedProviderRequest,
    provider_ref: str,
) -> object | None:
    """Publish once, returning remote evidence instead of process policy."""

    outcome = api.send(prepared)
    if type(outcome) in {
        ProviderRequestUnavailable,
        ProviderResponseRejected,
        ProviderTlsRejected,
    }:
        return outcome
    if type(outcome) is not ProviderHttpResponse:
        raise TypeError("Provider Hello returned unsupported transport evidence")
    if outcome.status == 200:
        receipt = parse_provider_hello_success(
            prepared,
            outcome,
            expected_provider_ref=provider_ref,
        )
        if type(receipt) is ProviderHelloAccepted:
            return None
        if type(receipt) is ProviderSuccessRejected:
            return receipt
        raise AssertionError("NMRPeak hello parser returned an unknown outcome")
    return parse_provider_problem(prepared.operation, outcome)


def _await_initial_hello(
    *,
    api: ProviderApiClient,
    prepared: _PreparedProviderRequest,
    provider_ref: str,
    policy: ProviderProcessPolicy,
    stop: Event,
) -> None:
    delay = _initial_retry_delay(policy.feed_interval_seconds)
    outage_active = False
    while not stop.is_set():
        failure = _publish_hello(
            api=api,
            prepared=prepared,
            provider_ref=provider_ref,
        )
        if failure is None:
            if outage_active:
                _LOG.info("Initial provider Hello recovered")
            return
        if not outage_active:
            _LOG.warning(
                "Initial provider Hello is unavailable; retrying: %s",
                _remote_evidence_message(failure),
            )
            outage_active = True
        if stop.wait(delay):
            return
        delay = _next_retry_delay(delay)


def _recover_startup(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    interpreter: InputInterpreter,
    owners: tuple[_LaneOwner, _LaneOwner],
    policy: ProviderProcessPolicy,
    stop: Event,
) -> None:
    records = journal.records()
    delay = _initial_retry_delay(policy.feed_interval_seconds)
    outage_active = False
    while not stop.is_set():
        failure: object | None = None
        try:
            inventory = read_attempt_inventory(
                api=api,
                maximum_pages=policy.inventory_maximum_pages,
            )
            if type(inventory) is AttemptInventoryReadFailed:
                failure = inventory.evidence
            else:
                validate_startup_inventory(
                    runtime=runtime,
                    records=records,
                    inventory=inventory,
                )
        except AttemptInventoryRejected as error:
            failure = error
        if failure is None:
            if outage_active:
                _LOG.info("Startup Attempt inventory recovered")
            break
        if not outage_active:
            _LOG.warning(
                "Startup Attempt inventory is unavailable; retrying: %s",
                _remote_evidence_message(failure),
            )
            outage_active = True
        if stop.wait(delay):
            return
        delay = _next_retry_delay(delay)

    for record in records:
        if stop.is_set():
            return
        current = record
        delay = _initial_retry_delay(policy.feed_interval_seconds)
        outage_active = False
        while not stop.is_set():
            owner = (
                _owner_for_record(runtime, owners, current)
                if current.frozen_generation_id == runtime.frozen_generation_id
                else None
            )
            outcome = run_recovery_record(
                runtime=runtime,
                api=api,
                journal=journal,
                interpreter=interpreter,
                session=None if owner is None else owner.session,
                record=current,
                observation=policy.observation,
            )
            failure = _remote_failure_evidence(outcome)
            if failure is None:
                if outage_active:
                    _LOG.info(
                        "Startup recovery for %s recovered",
                        current.execution_attempt_ref,
                    )
                break
            retained = tuple(
                candidate
                for candidate in journal.records()
                if candidate.provider_attempt_key == record.provider_attempt_key
            )
            if not retained:
                raise RuntimeError(
                    "Unavailable recovery outcome lost its durable Attempt record"
                )
            current = retained[0]
            if not outage_active:
                _LOG.warning(
                    "Startup recovery for %s is unavailable; retrying: %s",
                    current.execution_attempt_ref,
                    _remote_evidence_message(failure),
                )
                outage_active = True
            if stop.wait(delay):
                return
            delay = _next_retry_delay(delay)


def _run_lane(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    interpreter: InputInterpreter,
    policy: ProviderProcessPolicy,
    stop: Event,
    owner: _LaneOwner,
) -> None:
    cursor: str | None = None
    retry_delay = _initial_retry_delay(policy.feed_interval_seconds)
    outage_active = False
    primary_error: BaseException | None = None
    try:
        while not stop.is_set():
            failure: object | None = None
            records = tuple(
                record
                for record in journal.records()
                if runtime.resolve(record) is owner.generation
            )
            if records:
                for record in records:
                    if stop.is_set():
                        break
                    outcome = run_recovery_record(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        interpreter=interpreter,
                        session=owner.session,
                        record=record,
                        observation=policy.observation,
                    )
                    failure = _remote_failure_evidence(outcome)
                    if failure is not None:
                        break
            else:
                try:
                    admitted = admit_next_job(
                        lane=owner.generation.lane,
                        api=api,
                        journal=journal,
                        generation=owner.generation.generation,
                        frozen_generation_id=runtime.frozen_generation_id,
                        cursor=cursor,
                    )
                except AttemptJournalAdmissionRejected:
                    admitted = None
                if type(admitted) is JobAdmitted:
                    cursor = None
                    outcome = run_admitted_job(
                        runtime=runtime,
                        api=api,
                        journal=journal,
                        session=owner.session,
                        interpreter=interpreter,
                        admitted=admitted,
                        observation=policy.observation,
                    )
                    failure = _remote_failure_evidence(outcome)
                elif type(admitted) is PageExhausted:
                    cursor = admitted.next_cursor
                elif admitted is not None:
                    failure = _remote_failure_evidence(admitted)
            if owner.session.retired:
                raise RunnerSessionRetired(
                    f"Cannot continue the {owner.generation.lane.offering.implementation_ref} "
                    "lane because its runner session retired. Inspect the runner container "
                    "before restarting the provider."
                )
            if failure is not None:
                if not outage_active:
                    _LOG.warning(
                        "%s provider lane is unavailable; retrying: %s",
                        owner.generation.lane.offering.implementation_ref,
                        _remote_evidence_message(failure),
                    )
                    outage_active = True
                if stop.wait(retry_delay):
                    break
                retry_delay = _next_retry_delay(retry_delay)
                continue
            if outage_active:
                _LOG.info(
                    "%s provider lane recovered",
                    owner.generation.lane.offering.implementation_ref,
                )
            outage_active = False
            retry_delay = _initial_retry_delay(policy.feed_interval_seconds)
            stop.wait(policy.feed_interval_seconds)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            owner.session.retire()
        except RunnerSessionRetired:
            if primary_error is None:
                raise
            primary_error.add_note(
                "The failed NMRPeak lane also could not retire its runner session."
            )


def _owner_for_record(
    runtime: GenerationRuntime,
    owners: tuple[_LaneOwner, _LaneOwner],
    record: AttemptJournalRecord,
) -> _LaneOwner:
    generation = runtime.resolve(record)
    for owner in owners:
        if owner.generation is generation:
            return owner
    raise AssertionError("NMRPeak record resolved outside the two fixed lane owners")


def _remote_failure_evidence(outcome: object) -> object | None:
    """Return external evidence while leaving process policy to the caller."""

    if type(outcome) is InputInterpretationUnavailable:
        return outcome.evidence
    return _outcome_evidence(outcome)


def _outcome_evidence(outcome: object) -> object | None:
    if type(outcome) in {
        AttemptInventoryReadFailed,
        AttemptObservationFailed,
        FeedReadFailed,
        InputReadFailed,
        AttemptMutationCommitPossible,
        AttemptMutationNotCommitted,
    }:
        return outcome.evidence
    return None


def _remote_evidence_message(evidence: object) -> str:
    if type(evidence) is ProviderProblem:
        code = f", code {evidence.code}" if evidence.code is not None else ""
        return (
            f"the API returned HTTP {evidence.status} ({evidence.title.lower()}{code}; "
            f"request {evidence.transport_request_id})"
        )
    if type(evidence) is ProviderProblemRejected:
        return (
            f"the HTTP {evidence.status} problem response failed validation "
            f"({evidence.reason.value})"
        )
    if type(evidence) is ProviderSuccessRejected:
        return f"the HTTP 200 success response failed validation ({evidence.reason.value})"
    if type(evidence) is ProviderResponseRejected:
        status = "without a valid status" if evidence.status is None else f"with HTTP {evidence.status}"
        return f"the API response {status} failed validation ({evidence.reason.value})"
    if type(evidence) is ProviderTlsRejected:
        return "TLS verification failed before the request was sent"
    if type(evidence) is ProviderRequestUnavailable:
        if evidence.status is not None:
            return f"HTTP {evidence.status} ended without a complete API response"
        delivery = evidence.delivery.value.replace("_", " ")
        if evidence.cause is None:
            return f"request delivery was {delivery}"
        return (
            f"request delivery was {delivery}; "
            f"{type(evidence.cause).__name__}: {evidence.cause}"
        )
    if isinstance(evidence, BaseException):
        return f"{type(evidence).__name__}: {evidence}"
    raise AssertionError("Remote provider evidence has no operator description")


def _initial_retry_delay(feed_interval_seconds: float) -> float:
    return min(feed_interval_seconds, _MAX_REMOTE_RETRY_SECONDS)


def _next_retry_delay(current: float) -> float:
    return min(current * 2.0, _MAX_REMOTE_RETRY_SECONDS)


def _join_threads(threads: tuple[Thread, ...], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))


def _retire_sessions(owners: tuple[_LaneOwner, _LaneOwner]) -> None:
    first_error: RunnerSessionRetired | None = None
    for owner in owners:
        try:
            owner.session.retire()
        except RunnerSessionRetired as error:
            if first_error is None:
                first_error = error
            else:
                first_error.add_note(
                    "A second NMRPeak runner session also failed to retire."
                )
    if first_error is not None:
        raise first_error
