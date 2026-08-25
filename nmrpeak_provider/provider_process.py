"""Serve the fixed HF and CHF lanes under one bounded process owner."""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import Event, Thread
import time

from .attempt_inventory import (
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
from .provider_problems import ProviderProblem
from .provider_https import ProviderRequestUnavailable
from .runner_session import RunnerSession, RunnerSessionRetired


class ProviderStartupUnavailable(RuntimeError):
    """Server A did not yield the complete inventory required for startup."""


class ProviderLaneFailed(RuntimeError):
    """One fixed lane stopped before the provider process requested shutdown."""


class ProviderShutdownFailed(RuntimeError):
    """At least one fixed lane remained live after forced bounded shutdown."""


class ProviderProtocolFailed(RuntimeError):
    """Server A evidence is not safe to retry as ordinary unavailability."""


class ProviderLaneUnavailable(RuntimeError):
    """One lane exhausted its bounded consecutive API-unavailability budget."""


@dataclass(frozen=True, slots=True)
class ProviderProcessPolicy:
    """The bounded waits and inventory extent owned by one provider process."""

    feed_interval_seconds: float
    shutdown_drain_seconds: float
    forced_join_seconds: float
    inventory_maximum_pages: int
    maximum_consecutive_unavailable: int
    observation: ObservationPolicy

    def __post_init__(self) -> None:
        for value in (
            self.feed_interval_seconds,
            self.shutdown_drain_seconds,
            self.forced_join_seconds,
        ):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("NMRPeak process waits must be positive finite seconds")
        if type(self.inventory_maximum_pages) is not int or self.inventory_maximum_pages < 1:
            raise ValueError("NMRPeak inventory page bound must be positive")
        if (
            type(self.maximum_consecutive_unavailable) is not int
            or self.maximum_consecutive_unavailable < 1
        ):
            raise ValueError("NMRPeak unavailability budget must be positive")
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
        policy: ProviderProcessPolicy,
        stop: Event,
    ) -> None:
        try:
            _run_lane(
                runtime=runtime,
                api=api,
                journal=journal,
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
    hf_session: RunnerSession,
    chf_session: RunnerSession,
    policy: ProviderProcessPolicy,
    stop: Event,
) -> None:
    """Recover startup state, then serve exactly two sibling lane owners."""

    if type(runtime) is not GenerationRuntime:
        raise TypeError("NMRPeak process requires one admitted generation runtime")
    if type(hf_session) is not RunnerSession or type(chf_session) is not RunnerSession:
        raise TypeError("NMRPeak process requires both admitted runner sessions")
    if type(policy) is not ProviderProcessPolicy:
        raise TypeError("NMRPeak process requires an admitted process policy")
    if not isinstance(stop, Event):
        raise TypeError("NMRPeak process requires one stop event")

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
            owners=owners,
            policy=policy,
            stop=stop,
        )
        if stop.is_set():
            return

        finished = Event()
        works = tuple(_LaneWork(owner, finished) for owner in owners)
        threads = tuple(
            Thread(
                target=work.run,
                kwargs={
                    "runtime": runtime,
                    "api": api,
                    "journal": journal,
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

        while not stop.is_set() and not finished.wait(policy.feed_interval_seconds):
            pass
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
        for work in works:
            if work.error is not None:
                lane = work.owner.generation.lane.offering.implementation_ref
                failure = ProviderLaneFailed(
                    f"NMRPeak {lane} lane stopped before process shutdown"
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


def _recover_startup(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    owners: tuple[_LaneOwner, _LaneOwner],
    policy: ProviderProcessPolicy,
    stop: Event,
) -> None:
    records = journal.records()
    for attempt in range(policy.maximum_consecutive_unavailable):
        inventory = read_attempt_inventory(
            api=api,
            maximum_pages=policy.inventory_maximum_pages,
        )
        if type(inventory) is not AttemptInventoryReadFailed:
            break
        _outcome_is_unavailable(inventory)
        if attempt + 1 == policy.maximum_consecutive_unavailable:
            raise ProviderStartupUnavailable(
                "NMRPeak startup exhausted complete-inventory read attempts"
            )
        if stop.wait(policy.feed_interval_seconds):
            return
    else:
        raise AssertionError("NMRPeak startup inventory loop produced no outcome")
    validate_startup_inventory(
        runtime=runtime,
        records=records,
        inventory=inventory,
    )
    for record in records:
        if stop.is_set():
            return
        current = record
        for attempt in range(policy.maximum_consecutive_unavailable):
            owner = _owner_for_record(runtime, owners, current)
            outcome = run_recovery_record(
                runtime=runtime,
                api=api,
                journal=journal,
                session=owner.session,
                record=current,
                observation=policy.observation,
            )
            if not _outcome_is_unavailable(outcome):
                break
            retained = tuple(
                candidate
                for candidate in journal.records()
                if candidate.provider_attempt_key == record.provider_attempt_key
            )
            if not retained:
                raise ProviderProtocolFailed(
                    "Unavailable recovery outcome lost its durable Attempt record"
                )
            current = retained[0]
            if attempt + 1 == policy.maximum_consecutive_unavailable:
                raise ProviderStartupUnavailable(
                    "NMRPeak startup exhausted retained-Attempt recovery attempts"
                )
            if stop.wait(policy.feed_interval_seconds):
                return


def _run_lane(
    *,
    runtime: GenerationRuntime,
    api: ProviderApiClient,
    journal: AttemptJournalStore,
    policy: ProviderProcessPolicy,
    stop: Event,
    owner: _LaneOwner,
) -> None:
    cursor: str | None = None
    consecutive_unavailable = 0
    primary_error: BaseException | None = None
    try:
        while not stop.is_set():
            unavailable = False
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
                        session=owner.session,
                        record=record,
                        observation=policy.observation,
                    )
                    unavailable = _outcome_is_unavailable(outcome)
                    if unavailable:
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
                        admitted=admitted,
                        observation=policy.observation,
                    )
                    unavailable = _outcome_is_unavailable(outcome)
                elif type(admitted) is PageExhausted:
                    cursor = admitted.next_cursor
                elif admitted is not None:
                    unavailable = _outcome_is_unavailable(admitted)
            if unavailable:
                consecutive_unavailable += 1
                if (
                    consecutive_unavailable
                    >= policy.maximum_consecutive_unavailable
                ):
                    raise ProviderLaneUnavailable(
                        "NMRPeak lane exhausted consecutive API-unavailability attempts"
                    )
            else:
                consecutive_unavailable = 0
            if owner.session.retired:
                raise RunnerSessionRetired(
                    "NMRPeak lane cannot continue with a retired runner session"
                )
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


def _outcome_is_unavailable(outcome: object) -> bool:
    if type(outcome) in {
        AttemptInventoryReadFailed,
        AttemptObservationFailed,
        FeedReadFailed,
        InputReadFailed,
        AttemptMutationCommitPossible,
        AttemptMutationNotCommitted,
    }:
        evidence = outcome.evidence
    else:
        return False
    if type(evidence) is ProviderRequestUnavailable:
        return True
    if type(evidence) is ProviderProblem:
        if evidence.status in {404, 408, 500, 503}:
            return True
        if evidence.status == 409 and type(outcome) in {
            AttemptMutationCommitPossible,
            AttemptMutationNotCommitted,
        }:
            return True
    raise ProviderProtocolFailed(
        "NMRPeak provider operation returned fatal authentication or contract evidence"
    )


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
