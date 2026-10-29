"""Compose SDK-owned Attempts and API I/O with the two loaded science lanes."""

import asyncio
import json
import logging
from pathlib import Path
import signal

from provider_core.api import ProviderAPI
from provider_core.delivery_client import DeliveryClient
from provider_core.diagnostics import failure_evidence
from provider_core.hello import AnalysisOffering

from .provider_diagnostics import describe_nmrpeak_failure

from .attempt_journal_store import AttemptJournalStateRejected, AttemptJournalStore
from .frozen_generation import load_frozen_generation
from .input_interpreter import INTERPRETER_CONFIG_DIRECTORY, InputInterpreter
from .openai_chat_interpreter import load_openai_chat_endpoint_specs
from .process_logging import configure_process_logging
from .provider_config import (
    CHF_SOCKET_PATH, CONFIG_PATH, FROZEN_ROOT, HF_SOCKET_PATH, IDENTITY_LOCK_PATH,
    JOURNAL_PATH, ProviderConfigurationRejected,
)
from .provider_identity_lock import ProviderIdentityLock
from .provider_process import ProviderAnnouncement, ProviderProcess
from .provider_readiness import ProviderReadiness
from .provider_startup import load_provider_startup
from .runner_session import RunnerAdmissionError, prepare_runner_session

_LOG = logging.getLogger(__name__)
_DISPLAY_NAME = "NMRPeak"
_DESCRIPTION = (
    "Generates candidate molecular structures from molecular formula and NMR "
    "peak-list input. License, attribution, and source details: "
    "https://numpde.github.io/nmrpeak-repro/"
)
_HELLO_FILES = ("hello/hf.txt", "hello/chf.txt")
_FROZEN_FILES = {*_HELLO_FILES, "deployment/topology.json"}


def run_provider(config_path: Path = CONFIG_PATH) -> None:
    """Admit the frozen binding and guard legacy obligations before runtime effects."""
    readiness = ProviderReadiness.begin()
    primary = None
    try:
        identity_lock = ProviderIdentityLock.acquire_volume(IDENTITY_LOCK_PATH)
        try:
            startup = load_provider_startup(config_path)
            configured = startup.configured
            policy = configured.policy
            definition, binding, settings = startup.definition, startup.binding, startup.settings
            provider_ref = binding.api["provider_ref"]
            if provider_ref != identity_lock.provider_ref:
                raise ProviderConfigurationRejected("Selected API binding belongs to another provider identity lock")
            frozen = load_frozen_generation(
                FROZEN_ROOT, expected_frozen_generation_id=configured.frozen_generation_id,
            )
            if provider_ref != frozen.runtime.hf.generation.provider_ref:
                raise ProviderConfigurationRejected("Selected API binding belongs to another frozen provider generation")
            interpreter = InputInterpreter(
                load_openai_chat_endpoint_specs(INTERPRETER_CONFIG_DIRECTORY), policy.interpreter,
            )
            announcement = _prepare_hello(frozen)
            _require_empty_legacy_journal(JOURNAL_PATH)
        except BaseException as cause:
            try:
                identity_lock.close()
            except BaseException as cleanup:
                if cleanup is not cause:
                    raise BaseExceptionGroup("Provider admission and identity cleanup failure", [cause, cleanup]) from None
                raise
            raise
        asyncio.run(_serve(
            policy=policy, frozen=frozen, interpreter=interpreter, settings=settings,
            binding=binding, delivery_seconds=definition.request_seconds,
            announcement=announcement, readiness=readiness, identity_lock=identity_lock,
        ))
    except BaseException as cause:
        primary = cause
        raise
    finally:
        try:
            readiness.close()
        except BaseException as cleanup:
            if primary is not None and cleanup is not primary:
                raise BaseExceptionGroup("Provider failure and readiness cleanup failure", [primary, cleanup]) from None
            raise


def _require_empty_legacy_journal(path):
    """Preserve retained obligations; explicit archival owns their resolution."""
    with AttemptJournalStore(path, maximum_records=10000, read_only=True) as journal:
        if journal.records():
            raise AttemptJournalStateRejected(
                "Cannot start the shared provider while legacy Attempt records remain. "
                "Inspect the journal and explicitly archive confirmed closed Attempts before restarting."
            )


async def _serve(*, policy, frozen, interpreter, settings, binding, delivery_seconds,
                 announcement, readiness, identity_lock):
    hf = chf = process = None
    primary = None
    startup_stop = object()
    task = asyncio.current_task()
    signal_requested = False

    def stop(*_arguments):
        nonlocal signal_requested
        if signal_requested:
            return
        signal_requested = True
        if process is None:
            task.cancel(startup_stop)
        else:
            process.request_stop()

    previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}

    def new_api():
        return ProviderAPI(settings.identity, settings.http.new_transport(),
                           expected_topology=settings.expected_topology)

    def new_delivery():
        return DeliveryClient(binding.endpoint, exchange_seconds=delivery_seconds,
                              retirement_seconds=settings.http.retirement_seconds)

    try:
        hf = await prepare_runner_session(HF_SOCKET_PATH, frozen.runtime.hf.result_facts,
                                          policy.runner, frozen.runtime.hf.runner_codec)
        await hf.connect()
        chf = await prepare_runner_session(CHF_SOCKET_PATH, frozen.runtime.chf.result_facts,
                                           policy.runner, frozen.runtime.chf.runner_codec)
        await chf.connect()
        process = ProviderProcess(
            runtime=frozen.runtime, hf_session=hf, chf_session=chf, interpreter=interpreter,
            new_api=new_api, new_delivery=new_delivery, policy=policy.process,
            announcement=announcement,
        )
        if signal_requested:
            process.request_stop()
        await process.run(on_ready=readiness.publish)
    except BaseException as cause:
        primary = cause
    finally:
        failures = await _retire_runners(hf, chf)
        settled = (all(runner is None or runner.retirement_confirmed for runner in (hf, chf))
                   and (process is None or process.shutdown_ready))
        if settled:
            try:
                identity_lock.close()
            except BaseException as cleanup:
                failures.append(cleanup)
        elif not failures:
            failures.append(RuntimeError(
                "Provider work remains unsettled; identity exclusion stays with this process until it exits"
            ))
        # An unfinished SCI thread can keep Python alive after this coroutine
        # fails. Unconfirmed owners leave the FD to OS process teardown.
        for number, handler in previous.items():
            signal.signal(number, handler)
    if failures:
        raise BaseExceptionGroup("Provider startup/runtime and runner retirement failures",
                                 ([primary] if primary is not None else []) + failures) from None
    if primary is not None:
        interruption = primary.__cause__ if isinstance(primary, RunnerAdmissionError) else primary
        expected_stop = (signal_requested and type(interruption) is asyncio.CancelledError
                         and len(interruption.args) == 1 and interruption.args[0] is startup_stop
                         and task.cancelling() == 1)
        if expected_stop:
            task.uncancel()
        else:
            raise primary


async def _retire_runners(hf, chf):
    failures = []
    for runner in (hf, chf):
        if runner is not None and not runner.retirement_confirmed:
            try:
                await runner.retire()
            except BaseException as cause:
                failures.append(cause)
    return failures


def _prepare_hello(frozen):
    files = {file.path: file.content for file in frozen.files}
    if set(files) != _FROZEN_FILES:
        raise ValueError("Frozen generation has an invalid public file inventory")
    try:
        descriptions = tuple(files[path].decode("utf-8") for path in _HELLO_FILES)
    except UnicodeDecodeError as cause:
        raise ValueError("Frozen hello description is not UTF-8 text") from cause
    return ProviderAnnouncement(_DISPLAY_NAME, _DESCRIPTION, tuple(
        AnalysisOffering(lane.generation.analysis_kind_ref, description)
        for lane, description in zip((frozen.runtime.hf, frozen.runtime.chf), descriptions, strict=True)
    ))


def main() -> int:
    configure_process_logging()
    try:
        run_provider()
    except BaseException as cause:
        try:
            _LOG.error("Provider stopped unexpectedly; cleanup evidence: %s", json.dumps(
                failure_evidence(cause, describe_owner=describe_nmrpeak_failure, secrets=()), ensure_ascii=True,
            ))
        except BaseException as diagnostic:
            if diagnostic is cause:
                raise
            raise BaseExceptionGroup(
                "Provider failure and independent diagnostic failure", [cause, diagnostic],
            ) from None
        return 1
    _LOG.info("Provider stopped; local cleanup completed")
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except BaseException as cause:
        raise SystemExit(1) from cause
    raise SystemExit(status)
