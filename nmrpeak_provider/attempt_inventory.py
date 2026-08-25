"""Read and validate Server A's complete in-progress Attempt inventory."""

from __future__ import annotations

from dataclasses import dataclass, field

from .attempt_journal import (
    ActiveAttempt,
    AttemptJournalRecord,
    StartPending,
    TerminalPending,
)
from .generation_runtime import GenerationLane, GenerationRuntime
from .provider_api import ProviderApiClient
from .provider_https import (
    ProviderHttpResponse,
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
from .provider_requests import prepare_execution_attempts_list
from .provider_success import (
    InProgressAttempt,
    ProviderSuccessRejected,
    parse_execution_attempts_list_success,
)


_PAGE_LIMIT = 50

InventoryReadEvidence = (
    ProviderProblem
    | ProviderProblemRejected
    | ProviderRequestUnavailable
    | ProviderResponseRejected
    | ProviderTlsRejected
    | ProviderSuccessRejected
)


class AttemptInventoryRejected(RuntimeError):
    """The complete live inventory cannot authorize fresh Job admission."""


@dataclass(frozen=True, slots=True)
class AttemptInventory:
    """One complete bounded snapshot of the provider's in-progress Attempts."""

    attempts: tuple[InProgressAttempt, ...]


@dataclass(frozen=True, slots=True)
class AttemptInventoryReadFailed:
    """Server A did not yield one complete admitted inventory snapshot."""

    evidence: InventoryReadEvidence = field(repr=False)


def read_attempt_inventory(
    *,
    api: ProviderApiClient,
    maximum_pages: int,
) -> AttemptInventory | AttemptInventoryReadFailed:
    """Traverse every bounded page without returning a partial inventory."""

    if type(maximum_pages) is not int or maximum_pages < 1:
        raise ValueError("Attempt inventory page bound must be positive")
    attempts: list[InProgressAttempt] = []
    attempt_refs: set[str] = set()
    attempt_keys: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None

    for _ in range(maximum_pages):
        prepared = prepare_execution_attempts_list(
            limit=_PAGE_LIMIT,
            cursor=cursor,
        )
        response = api.send(prepared)
        if type(response) is not ProviderHttpResponse or response.status != 200:
            return AttemptInventoryReadFailed(
                _read_failure(prepared.operation, response)
            )
        page = parse_execution_attempts_list_success(prepared, response)
        if type(page) is ProviderSuccessRejected:
            return AttemptInventoryReadFailed(page)
        for attempt in page.attempts:
            if (
                attempt.execution_attempt_ref in attempt_refs
                or attempt.provider_attempt_key in attempt_keys
            ):
                raise AttemptInventoryRejected(
                    "Attempt inventory repeats an Attempt identity"
                )
            attempt_refs.add(attempt.execution_attempt_ref)
            attempt_keys.add(attempt.provider_attempt_key)
            attempts.append(attempt)
        if page.next_cursor is None:
            return AttemptInventory(tuple(attempts))
        if page.next_cursor in seen_cursors:
            raise AttemptInventoryRejected("Attempt inventory repeats a page cursor")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor

    raise AttemptInventoryRejected("Attempt inventory exceeds its configured page bound")


def validate_startup_inventory(
    *,
    runtime: GenerationRuntime,
    records: tuple[AttemptJournalRecord, ...],
    inventory: AttemptInventory,
) -> None:
    """Require every live Server A Attempt to have its exact durable owner."""

    if type(records) is not tuple or any(
        type(record) not in {StartPending, ActiveAttempt, TerminalPending}
        for record in records
    ):
        raise TypeError("Startup inventory validation requires exact journal records")
    if type(inventory) is not AttemptInventory:
        raise TypeError("Startup inventory validation requires a complete inventory")

    owned: dict[str, tuple[AttemptJournalRecord, GenerationLane | None]] = {}
    for record in records:
        # A record from another generation may be observed and terminalized,
        # but only a current-generation record may recover into execution.
        resolved = (
            runtime.resolve(record)
            if record.frozen_generation_id == runtime.frozen_generation_id
            else None
        )
        if record.provider_attempt_key in owned:
            raise AttemptInventoryRejected(
                "Attempt journal repeats a provider Attempt key"
            )
        owned[record.provider_attempt_key] = (record, resolved)

    seen_refs: set[str] = set()
    seen_keys: set[str] = set()
    for attempt in inventory.attempts:
        if type(attempt) is not InProgressAttempt:
            raise TypeError("Startup inventory contains an invalid Attempt")
        if (
            attempt.execution_attempt_ref in seen_refs
            or attempt.provider_attempt_key in seen_keys
        ):
            raise AttemptInventoryRejected(
                "Attempt inventory repeats an Attempt identity"
            )
        seen_refs.add(attempt.execution_attempt_ref)
        seen_keys.add(attempt.provider_attempt_key)
        owner = owned.get(attempt.provider_attempt_key)
        if owner is None:
            raise AttemptInventoryRejected(
                "Server A has an in-progress Attempt without a journal owner"
            )
        record, resolved = owner
        if attempt.job_ref != record.job_ref:
            raise AttemptInventoryRejected(
                "Server A Attempt identity differs from its journal owner"
            )
        if (
            resolved is not None
            and attempt.analysis_kind_ref
            != resolved.lane.offering.analysis_kind_ref
        ):
            raise AttemptInventoryRejected(
                "Server A Attempt analysis kind differs from its journal owner"
            )
        if (
            type(record) in {ActiveAttempt, TerminalPending}
            and attempt.execution_attempt_ref != record.execution_attempt_ref
        ):
            raise AttemptInventoryRejected(
                "Server A Attempt reference differs from its journal owner"
            )


def _read_failure(
    operation: ProviderOperation,
    outcome: object,
) -> InventoryReadEvidence:
    if type(outcome) is ProviderHttpResponse:
        return parse_provider_problem(operation, outcome)
    if type(outcome) in {
        ProviderRequestUnavailable,
        ProviderResponseRejected,
        ProviderTlsRejected,
    }:
        return outcome
    raise TypeError("Attempt inventory read returned unsupported transport evidence")
