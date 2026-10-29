"""Stateless journal records used by archive, inspection, and record tests."""

from base64 import b64encode
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from nmrpeak_provider.attempt_identity import derive_provider_attempt_key
from nmrpeak_provider.attempt_journal import (
    ActiveAttempt,
    LocalExecutionPhase,
    StartPending,
    TerminalOperation,
    TerminalPending,
)
from nmrpeak_provider.attempt_journal_store import AttemptJournalStore
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CODEC, CHF_RUNNER_CONTRACT_ID
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CODEC, HF_RUNNER_CONTRACT_ID
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.product_input import InputRejectionReason
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    RESULT_SCHEMA_ID,
    ProviderResultFacts,
)
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    run_generation_fingerprint,
)
from nmrpeak_provider.generation_runtime import GenerationLane, GenerationRuntime

FROZEN_GENERATION_ID = "sha256:" + "4" * 64
CHF_FACTS = ProviderResultFacts(
    identity=CHF_RESULT_IDENTITY,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "5" * 64,
    image_input_ref="sha256:" + "6" * 64,
)
HF_FACTS = ProviderResultFacts(
    identity=HF_RESULT_IDENTITY,
    runner_contract_id=HF_RUNNER_CONTRACT_ID,
    checkpoint_ref="sha256:" + "7" * 64,
    image_input_ref="sha256:" + "8" * 64,
)


def _generation(kind: str) -> RunGenerationIdentity:
    return RunGenerationIdentity(
        provider_ref="provider:nmrpeak",
        analysis_kind_ref=kind,
        generation_id="fixture-generation",
        scope=CreatedAtWindow(
            datetime(2026, 8, 24, tzinfo=UTC),
            datetime(2026, 8, 26, tzinfo=UTC),
        ),
    )


def generation_runtime() -> GenerationRuntime:
    return GenerationRuntime(
        frozen_generation_id=FROZEN_GENERATION_ID,
        hf=GenerationLane(
            lane=HF_LIFECYCLE_LANE,
            generation=_generation("mol_from_1h_peaks"),
            result_facts=HF_FACTS,
            runner_codec=HF_RUNNER_CODEC,
        ),
        chf=GenerationLane(
            lane=CHF_LIFECYCLE_LANE,
            generation=_generation("mol_from_1h_13c_formula"),
            result_facts=CHF_FACTS,
            runner_codec=CHF_RUNNER_CODEC,
        ),
    )


def attempt_snapshot(*, execution_attempt_ref: str, job_ref: str, state: str, job_state: str) -> dict[str, object]:
    return {
        "schema_id": "nmr.provider.execution_attempt_read_response.v1",
        "execution_attempt_ref": execution_attempt_ref,
        "job_ref": job_ref,
        "state": state,
        "job_state": job_state,
    }


@dataclass(frozen=True)
class HistoricalCommand:
    operation: TerminalOperation
    body: bytes


def complete_command(*, execution_attempt_ref, result_schema_id, canonical_result):
    return HistoricalCommand(TerminalOperation.COMPLETE, canonical_json_bytes({
        "schema_id": "nmr.provider.execution_attempt_complete_request.v1",
        "execution_attempt_ref": execution_attempt_ref, "result_schema_id": result_schema_id,
        "canonical_result_base64": b64encode(canonical_result).decode("ascii"),
    }))


def failure_command(*, execution_attempt_ref, failure_code, failure_message):
    return HistoricalCommand(TerminalOperation.FAIL, canonical_json_bytes({
        "schema_id": "nmr.provider.execution_attempt_fail_request.v1",
        "execution_attempt_ref": execution_attempt_ref, "failure_code": failure_code,
        "failure_message": failure_message,
    }))


def entered_record(active):
    return replace(active, local_phase=LocalExecutionPhase.EXECUTION_ENTERED)


def retained_terminal(active, command):
    return TerminalPending(
        job_ref=active.job_ref, provider_attempt_key=active.provider_attempt_key,
        input_fingerprint=active.input_fingerprint, frozen_generation_id=active.frozen_generation_id,
        execution_attempt_ref=active.execution_attempt_ref, terminal_operation=command.operation,
        terminal_request_body=command.body,
        terminal_request_fingerprint="sha256:" + sha256(command.body).hexdigest(),
        local_phase=active.local_phase, latest_diagnostic=active.latest_diagnostic,
    )


def terminal_pending(operation: TerminalOperation) -> TerminalPending:
    raw_input = b"retained journal fixture input"
    input_fingerprint = "sha256:" + sha256(raw_input).hexdigest()
    generation = _generation("mol_from_1h_13c_formula")
    active = ActiveAttempt(
        job_ref="job:selected",
        provider_attempt_key=derive_provider_attempt_key(
            provider_ref=generation.provider_ref,
            run_generation_fingerprint=run_generation_fingerprint(generation),
            job_ref="job:selected",
            input_fingerprint=input_fingerprint,
        ),
        input_fingerprint=input_fingerprint,
        frozen_generation_id=FROZEN_GENERATION_ID,
        execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
        local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
    )
    prepared = (
        complete_command(
            execution_attempt_ref=active.execution_attempt_ref,
            result_schema_id=RESULT_SCHEMA_ID,
            canonical_result=b'{"candidates":[{"generated_smiles":"CCO"}]}',
        )
        if operation is TerminalOperation.COMPLETE
        else failure_command(
            execution_attempt_ref=active.execution_attempt_ref,
            failure_code="input_rejected",
            failure_message=InputRejectionReason.INVALID_STRUCTURE.value,
        )
    )
    return retained_terminal(active, prepared)


def persist_terminal(journal: AttemptJournalStore, terminal: TerminalPending) -> None:
    pending = StartPending(
        job_ref=terminal.job_ref,
        provider_attempt_key=terminal.provider_attempt_key,
        input_fingerprint=terminal.input_fingerprint,
        frozen_generation_id=terminal.frozen_generation_id,
    )
    entered = ActiveAttempt(
        job_ref=terminal.job_ref,
        provider_attempt_key=terminal.provider_attempt_key,
        input_fingerprint=terminal.input_fingerprint,
        frozen_generation_id=terminal.frozen_generation_id,
        execution_attempt_ref=terminal.execution_attempt_ref,
        local_phase=LocalExecutionPhase.EXECUTION_ENTERED,
    )
    journal.admit(pending)
    journal.replace(pending, entered)
    journal.replace(entered, terminal)


@contextmanager
def journal_directory():
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "journal"
        path.mkdir(mode=0o700)
        yield path
