"""Compose the shared lifecycle and delivery owners for the two NMR lanes."""

import asyncio
from dataclasses import dataclass
import logging
import secrets

from provider_core.api import ApiFailure
from provider_core.analysis import AnalysisFailure, AnalysisSuccess
from provider_core.attempt_record import RecoveryScope, StartPending
from provider_core.attempt_request import start_body
from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.task_boundary import contain_task_exit
from provider_core.diagnostics import failure_evidence
from provider_core.execution import AttemptExecutor, ConditionRecovery, ExecutionRefused, ObservationRecovery
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull, OwnershipRefused
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint

from .provider_diagnostics import describe_nmrpeak_failure

from .analysis_session import NmrpeakAnalysisSession
from .canonical_json import canonical_json_bytes
from .provider_config import ProviderProcessPolicy
from .run_generation import run_generation_fingerprint


_LOG = logging.getLogger(__name__)
# NMR results are at most 65536 bytes before the SDK's base64 envelope.
MAXIMUM_TERMINAL_BYTES = 131072


@dataclass(frozen=True, slots=True)
class ProviderAnnouncement:
    display_name: str
    description: str
    analysis_offerings: tuple


@dataclass(slots=True)
class _LaneOwner:
    generation: object
    runner: object
    controller: AttemptController | None = None
    cursor: str | None = None


class ProviderProcess:
    """Keep loaded science owners while SDK controllers own each live Attempt."""

    def __init__(self, *, runtime, hf_session, chf_session, interpreter,
                 new_api, new_delivery, policy, announcement):
        if type(policy) is not ProviderProcessPolicy:
            raise TypeError("Provider process requires admitted timing policy")
        self.policy, self.announcement, self.interpreter = policy, announcement, interpreter
        self._new_api, self._new_delivery = new_api, new_delivery
        self.api, self.hello_api, self.delivery = new_api(), new_api(), new_delivery()
        self.lanes = (_LaneOwner(runtime.hf, hf_session), _LaneOwner(runtime.chf, chf_session))
        for lane in self.lanes:
            if lane.runner.result_facts != lane.generation.result_facts:
                raise ValueError("Provider lane and loaded runner belong to different generations")
        provider_ref = runtime.hf.generation.provider_ref
        if (runtime.chf.generation.provider_ref != provider_ref
                or self.delivery.endpoint.provider_ref != provider_ref
                or self.delivery.endpoint.api_origin != self.api.origin):
            raise ValueError("Provider lanes, API and delivery must share one provider binding")
        self._claims = AttemptClaims.live(limits=LiveAttemptLimits(2, 262144, 2 * MAXIMUM_TERMINAL_BYTES))
        self._admission = asyncio.Lock()
        self._admission_paused = asyncio.Event()
        self._delivery_waiters = set()
        self._stop = asyncio.Event()
        self._scientific_stopping = asyncio.Event()
        self._state = "new"
        self.diagnostic_failure = None

    def _controllers(self):
        return tuple(dict.fromkeys((*self._claims.live_owners(), *(
            lane.controller for lane in self.lanes if lane.controller is not None))))

    @property
    def shutdown_ready(self):
        return (self._state == "stopped" and all(owner.shutdown_ready for owner in self._controllers())
                and self.api.retirement_confirmed and self.hello_api.retirement_confirmed
                and self.delivery.retirement_confirmed)

    def request_stop(self):
        """Stop admission while existing scientific/report drivers drain."""
        self._admission_paused.set()
        self._stop.set()

    async def run(self, *, on_ready):
        if self._state != "new":
            raise ValueError("Provider process accepts only its original invocation")
        self._state = "running"
        primary = None
        try:
            if self._stop.is_set():
                return
            if not self._stop.is_set():
                await self._announce()
            if not self._stop.is_set():
                on_ready()
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(contain_task_exit(self._lane_loop, self.lanes[0]))
                    tasks.create_task(contain_task_exit(self._lane_loop, self.lanes[1]))
                    tasks.create_task(contain_task_exit(self._hello_loop))
        except BaseException as cause:
            primary = cause
            self._scientific_stopping.set()
        finally:
            self.request_stop()
            self._state = "stopping"
            try:
                await self._drain()
            except BaseException as cleanup:
                if primary is not None and cleanup is not primary:
                    raise BaseExceptionGroup(
                        "Provider failure and independent shutdown failure", [primary, cleanup],
                    ) from None
                raise
        if primary is not None:
            raise primary

    async def _lane_loop(self, lane):
        while not self._stop.is_set():
            selected = await self._select(lane)
            if selected is None:
                await self._pause(self.policy.feed_interval_seconds)
                continue
            pending, instructions = selected
            session = NmrpeakAnalysisSession(lane.generation, lane.runner, self.interpreter,
                                             retry_seconds=self.policy.feed_interval_seconds)
            lane.controller = self._controller(lane, session, pending, instructions)
            try:
                result = await lane.controller.run(pending)
            except ControllerFailed as failure:
                duplicate = (type(failure.cause) is OwnershipRefused
                             and self._claims.owns_job(pending.scope, pending.input.job_ref))
                if failure.cleanup_failures or not (type(failure.cause) is LiveCapacityFull or duplicate):
                    raise
                await lane.controller.settle()
                await self._pause(self.policy.feed_interval_seconds)
            else:
                if type(result) is DeliveryAcceptedCheckpoint:
                    self._delivery_waiters.discard(lane.controller)
                if lane.runner.retired:
                    raise RuntimeError("The loaded NMR lane retired; retain its owners before replacing it")

    def _controller(self, lane, session, pending, instructions):
        policy = self.policy
        return AttemptController.live(
            self._new_api(), self._claims, session, scope=pending.scope, stopping=self._scientific_stopping,
            admission_stopping=self._admission_paused,
            start_policy=LiveStartPolicy(policy.feed_interval_seconds, self._start_retry, self._start_recovered),
            maximum_terminal_bytes=MAXIMUM_TERMINAL_BYTES, preverified_input=instructions,
            allow_active_start_replay=True, delivery=self._new_delivery(),
            on_delivery_retry=lambda acknowledged, failure, seconds: self._delivery_retry(lane, failure),
            delivery_retry_initial_seconds=policy.feed_interval_seconds,
            delivery_retry_max_seconds=30,
            allowed_job_states=frozenset({"open"}),
            poll_seconds=policy.observation.poll_interval_seconds,
            task_settle_seconds=policy.forced_join_seconds,
            observation_recovery=ObservationRecovery(policy.observation.retry_initial_seconds, policy.observation.retry_max_seconds,
                                                       self._observation_retry, self._observation_recovered),
            condition_recovery=ConditionRecovery(policy.observation.retry_initial_seconds, policy.observation.retry_max_seconds,
                                                  self._condition_retry, self._condition_recovered),
            on_unconfirmed_phase_progress=self._phase_unconfirmed,
        )

    async def _select(self, lane):
        async with self._admission:
            if self._stop.is_set():
                return None
            self._clear_delivery_pressure()
            if self._admission_paused.is_set():
                return None
            generation = lane.generation.generation
            scope = RecoveryScope(self.api.origin, generation.provider_ref,
                                  lane.generation.result_facts.identity.runner_ref,
                                  run_generation_fingerprint(generation))
            for _ in range(self.policy.inventory_maximum_pages):
                try:
                    page = await self.api.jobs(analysis_kind_ref=generation.analysis_kind_ref,
                                               has_provider_execution_attempt=False, limit=50,
                                               cursor=lane.cursor, owner=self)
                    lane.cursor = page.next_cursor
                    for job in page.jobs:
                        if (not generation.scope.contains(job.created_at)
                                or self._claims.owns_job(scope, job.input.job_ref)):
                            continue
                        instructions = await self.api.job_input(job.input,
                                                                analysis_kind_ref=generation.analysis_kind_ref,
                                                                owner=self)
                        if self._stop.is_set() or self._admission_paused.is_set():
                            return None
                        # A new selection cannot replay an admission from a lost process.
                        key = "nmrpeak-provider.v1:" + secrets.token_hex(32)
                        return StartPending(scope, generation.analysis_kind_ref, job.input,
                                            start_body(job.input.job_ref, key)), instructions
                except ApiFailure as cause:
                    await self.api.retire(owner=self)
                    if not (self.api.can_retry_job_discovery(cause) or self.api.can_retry_read(cause)):
                        raise
                    self._notice("Job discovery temporarily unavailable", cause)
                    return None
                if lane.cursor is None:
                    return None
            return None

    def _delivery_retry(self, lane, cause):
        self._admission_paused.set()
        self._delivery_waiters.add(lane.controller)
        self._notice("Exact report remains with its original shared controller", cause)

    def _clear_delivery_pressure(self):
        for controller in tuple(self._delivery_waiters):
            operation = controller.operation
            if (type(operation) is AttemptExecutor and operation.last_failure is not None
                    and type(operation.last_failure.attempt) is DeliveryAcceptedCheckpoint):
                self._delivery_waiters.discard(controller)
        if not self._delivery_waiters and not self._stop.is_set():
            self._admission_paused.clear()

    async def _announce(self):
        while not self._stop.is_set():
            try:
                await self.hello_api.publish_hello(
                    provider_ref=self.lanes[0].generation.generation.provider_ref,
                    display_name=self.announcement.display_name, description=self.announcement.description,
                    analysis_offerings=self.announcement.analysis_offerings, owner=self,
                )
                return
            except ApiFailure as cause:
                await self.hello_api.retire(owner=self)
                if not self.hello_api.can_refresh_hello(cause):
                    raise
                self._notice("Provider Hello remains unconfirmed", cause)
                await self._pause(self.policy.feed_interval_seconds)

    async def _hello_loop(self):
        while not await self._pause(self.policy.hello_interval_seconds):
            await self._announce()

    async def _pause(self, seconds):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def _drain(self):
        controllers = self._controllers()
        blocked = set()
        failures = []
        for controller in controllers:
            controller.request_stop()
        for controller in controllers:
            try:
                await controller.join()
            except asyncio.CancelledError as cause:
                failures.append(cause)
            except BaseException as cause:
                self._notice("Original Attempt driver stopped with retained evidence", cause)
        while any(not controller.shutdown_ready and controller not in blocked for controller in controllers):
            for controller in controllers:
                if not controller.shutdown_ready and controller not in blocked:
                    operation = controller.operation
                    disposition_attempted = False
                    try:
                        if (type(operation) is AttemptExecutor and operation.work_settled
                                and operation.last_failure is not None
                                and type(operation.last_failure.outcome) in (AnalysisSuccess, AnalysisFailure)):
                            disposition_attempted = True
                            await operation.retain_suppressed_disposition()
                        await controller.settle()
                    except (ExecutionRefused, asyncio.CancelledError) as cause:
                        blocked.add(controller)
                        failures.append(cause)
                    except BaseException as cause:
                        self._notice("Original Attempt cleanup or report remains owned", cause)
                        if disposition_attempted:
                            blocked.add(controller)
                            failures.append(cause)
            if any(not controller.shutdown_ready and controller not in blocked for controller in controllers):
                try:
                    await asyncio.sleep(self.policy.feed_interval_seconds)
                except asyncio.CancelledError as cause:
                    blocked.update(controller for controller in controllers if not controller.shutdown_ready)
                    failures.append(cause)
                    break
        for retire in (lambda: self.api.retire(owner=self),
                       lambda: self.hello_api.retire(owner=self), self.delivery.retire):
            try:
                await retire()
            except BaseException as cause:
                failures.append(cause)
        if (not blocked and self.api.retirement_confirmed and self.hello_api.retirement_confirmed
                and self.delivery.retirement_confirmed):
            self._state = "stopped"
        if failures:
            raise BaseExceptionGroup("Provider shutdown retained owners or retirement failures", failures)

    def _notice(self, message, cause):
        try:
            evidence = failure_evidence(cause, describe_owner=describe_nmrpeak_failure, secrets=self.interpreter.diagnostic_secrets)
            _LOG.warning("%s; %s", message, canonical_json_bytes(evidence).decode("utf-8"))
        except Exception as diagnostic:
            if self.diagnostic_failure is None:
                self.diagnostic_failure = diagnostic

    def _start_retry(self, pending, failure, seconds):
        self._notice("Original live START remains unconfirmed", failure)

    def _start_recovered(self, acknowledged, failure):
        self._notice("Original live START acknowledgement recovered", failure)

    def _observation_retry(self, acknowledged, failure, seconds):
        self._notice("Attempt observation temporarily unavailable", failure)

    def _observation_recovered(self, acknowledged, failure):
        self._notice("Attempt observation recovered", failure)

    def _condition_retry(self, acknowledged, phase, condition, failure, seconds):
        self._notice("Current scientific condition remains unconfirmed", failure)

    def _condition_recovered(self, acknowledged, phase, condition, failure):
        self._notice("Current scientific condition confirmed", failure)

    def _phase_unconfirmed(self, acknowledged, phase, failure):
        self._notice("Scientific phase announcement remains unconfirmed", failure)
