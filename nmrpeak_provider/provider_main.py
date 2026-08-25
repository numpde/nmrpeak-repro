"""Production composition for the fixed two-runner NMRPeak provider."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
from threading import Event
import traceback

from .attempt_journal_store import AttemptJournalStore
from .frozen_generation import FrozenGeneration, load_frozen_generation
from .provider_api import ProviderApiClient
from .provider_config import (
    CHF_SOCKET_PATH,
    CONFIG_PATH,
    CREDENTIAL_PATH,
    FROZEN_ROOT,
    HF_SOCKET_PATH,
    IDENTITY_LOCK_PATH,
    JOURNAL_PATH,
    decode_provider_runtime_config,
)
from .provider_credential import (
    PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES,
    parse_provider_signing_credential,
)
from .provider_identity_lock import ProviderIdentityLock
from .input_interpreter import (
    INTERPRETER_CONFIG_DIRECTORY,
    InputInterpreter,
)
from .openai_chat_interpreter import load_openai_chat_endpoint_specs
from .provider_process import run_provider_process
from .provider_readiness import ProviderReadiness
from .provider_requests import HelloOffering, prepare_provider_hello
from .runner_session import RunnerSession, open_runner_session


_CONFIG_MAX_BYTES = 65_536
_DISPLAY_NAME = "NMRPeak"
_DESCRIPTION = (
    "Generates candidate molecular structures from molecular formula and NMR "
    "peak-list input."
)
_HELLO_FILES = ("hello/hf.txt", "hello/chf.txt")
_FROZEN_FILES = {*_HELLO_FILES, "deployment/topology.json"}


def run_provider(config_path: Path = CONFIG_PATH) -> None:
    """Admit every local input, then serve until signal or fatal failure."""

    readiness = ProviderReadiness.begin()
    try:
        _run_provider(config_path, readiness)
    finally:
        readiness.close()


def _run_provider(config_path: Path, readiness: ProviderReadiness) -> None:
    configured = decode_provider_runtime_config(
        _read_regular_file(config_path, _CONFIG_MAX_BYTES)
    )
    interpreter = InputInterpreter(
        load_openai_chat_endpoint_specs(INTERPRETER_CONFIG_DIRECTORY),
        configured.interpreter,
    )
    frozen = load_frozen_generation(
        FROZEN_ROOT,
        expected_frozen_generation_id=configured.frozen_generation_id,
    )
    credential = parse_provider_signing_credential(
        _read_regular_file(CREDENTIAL_PATH, PROVIDER_SIGNING_CREDENTIAL_MAX_BYTES)
    )
    if credential.provider_ref != frozen.runtime.hf.generation.provider_ref:
        raise ValueError("Provider credential belongs to another frozen generation")
    hello = _prepare_hello(frozen)
    stop = Event()
    previous_handlers = {
        signal_number: signal.signal(signal_number, lambda *_args: stop.set())
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    hf_session: RunnerSession | None = None
    chf_session: RunnerSession | None = None
    journal: AttemptJournalStore | None = None
    primary_error: BaseException | None = None
    try:
        with ProviderIdentityLock.acquire(
            IDENTITY_LOCK_PATH,
            credential.provider_ref,
        ):
            try:
                api = ProviderApiClient(
                    configured.endpoint.materialize(),
                    credential.credential_ref,
                    credential.private_key,
                )
                hf_session = open_runner_session(
                    HF_SOCKET_PATH,
                    frozen.runtime.hf.result_facts,
                    configured.runner,
                    frozen.runtime.hf.runner_codec,
                )
                chf_session = open_runner_session(
                    CHF_SOCKET_PATH,
                    frozen.runtime.chf.result_facts,
                    configured.runner,
                    frozen.runtime.chf.runner_codec,
                )
                journal = AttemptJournalStore(
                    JOURNAL_PATH,
                    maximum_records=configured.journal_maximum_records,
                    filesystem_reserve_bytes=(
                        configured.journal_filesystem_reserve_bytes
                    ),
                )
                run_provider_process(
                    runtime=frozen.runtime,
                    api=api,
                    journal=journal,
                    interpreter=interpreter,
                    hf_session=hf_session,
                    chf_session=chf_session,
                    hello=hello,
                    policy=configured.process,
                    stop=stop,
                    on_ready=readiness.publish,
                )
            except BaseException as error:
                primary_error = error
                raise
            finally:
                cleanup_errors: list[BaseException] = []
                if journal is not None:
                    try:
                        journal.close()
                    except BaseException as error:
                        cleanup_errors.append(error)
                for session in (hf_session, chf_session):
                    if session is not None and not session.retired:
                        try:
                            session.retire()
                        except BaseException as error:
                            cleanup_errors.append(error)
                if cleanup_errors:
                    if primary_error is not None:
                        primary_error.add_note(
                            "Provider cleanup could not confirm every local resource closure."
                        )
                    else:
                        raise cleanup_errors[0]
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def _prepare_hello(frozen: FrozenGeneration):
    files = {frozen_file.path: frozen_file.content for frozen_file in frozen.files}
    if set(files) != _FROZEN_FILES:
        raise ValueError("Frozen generation has an invalid public file inventory")
    try:
        descriptions = tuple(files[path].decode("utf-8") for path in _HELLO_FILES)
    except UnicodeDecodeError as error:
        raise ValueError("Frozen hello description is not UTF-8 text") from error
    return prepare_provider_hello(
        display_name=_DISPLAY_NAME,
        description=_DESCRIPTION,
        analysis_offerings=tuple(
            HelloOffering(lane.generation.analysis_kind_ref, description)
            for lane, description in zip(
                (frozen.runtime.hf, frozen.runtime.chf),
                descriptions,
                strict=True,
            )
        ),
    )


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"Cannot read provider startup input {path}") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > maximum_bytes:
            raise ValueError("Provider startup input must be a bounded regular file")
        content = os.read(descriptor, maximum_bytes + 1)
        if len(content) != status.st_size:
            raise ValueError("Provider startup input changed while it was read")
        return content
    finally:
        os.close(descriptor)


def main() -> int:
    try:
        run_provider()
    except Exception as error:
        print("Provider process stopped unexpectedly.", file=os.sys.stderr)
        traceback.print_exception(error, file=os.sys.stderr)
        print(
            "The provider is not ready; inspect the failure above and the runner "
            "status before restarting.",
            file=os.sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
