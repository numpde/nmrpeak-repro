"""Interpret one Attempt mutation send without hiding commit uncertainty."""

from __future__ import annotations

from dataclasses import dataclass

from .provider_https import (
    ProviderHttpResponse,
    ProviderHttpsOutcome,
    ProviderOperation,
    ProviderRequestUnavailable,
    ProviderResponseRejected,
    ProviderTlsRejected,
    RequestDelivery,
)
from .provider_problems import (
    ProviderProblem,
    ProviderProblemRejected,
    parse_provider_problem,
)
from .provider_requests import _PreparedProviderRequest
from .provider_success import (
    ExecutionAttemptCompleted,
    ExecutionAttemptFailed,
    ExecutionAttemptProgressed,
    ExecutionAttemptStarted,
    ProviderSuccessRejected,
    parse_execution_attempt_complete_success,
    parse_execution_attempt_fail_success,
    parse_execution_attempt_progress_success,
    parse_execution_attempt_start_success,
)


AttemptMutationReceipt = (
    ExecutionAttemptStarted
    | ExecutionAttemptProgressed
    | ExecutionAttemptCompleted
    | ExecutionAttemptFailed
)
AttemptMutationNotCommittedEvidence = (
    ProviderProblem | ProviderRequestUnavailable | ProviderTlsRejected
)
AttemptMutationCommitPossibleEvidence = (
    ProviderProblem
    | ProviderProblemRejected
    | ProviderSuccessRejected
    | ProviderRequestUnavailable
    | ProviderResponseRejected
)


@dataclass(frozen=True, slots=True)
class AttemptMutationNotCommitted:
    """This send is proved not to have committed its Attempt mutation."""

    evidence: AttemptMutationNotCommittedEvidence


@dataclass(frozen=True, slots=True)
class AttemptMutationCommitPossible:
    """This send may have committed and requires authoritative reconciliation."""

    evidence: AttemptMutationCommitPossibleEvidence


@dataclass(frozen=True, slots=True)
class AttemptMutationCommitted:
    """A command-bound receipt confirms this send's Attempt mutation."""

    receipt: AttemptMutationReceipt


AttemptMutationSendOutcome = (
    AttemptMutationNotCommitted
    | AttemptMutationCommitPossible
    | AttemptMutationCommitted
)


def interpret_execution_attempt_start(
    prepared: _PreparedProviderRequest,
    outcome: ProviderHttpsOutcome,
    *,
    expected_provider_ref: str,
    expected_analysis_kind_ref: str,
) -> AttemptMutationSendOutcome:
    """Interpret one start send and bind any success to its retained facts."""

    response = _success_response_or_send_outcome(
        ProviderOperation.EXECUTION_ATTEMPT_START,
        outcome,
    )
    if type(response) is not ProviderHttpResponse:
        return response
    receipt = parse_execution_attempt_start_success(
        prepared,
        response,
        expected_provider_ref=expected_provider_ref,
        expected_analysis_kind_ref=expected_analysis_kind_ref,
    )
    return _receipt_outcome(receipt)


def interpret_execution_attempt_progress(
    prepared: _PreparedProviderRequest,
    outcome: ProviderHttpsOutcome,
) -> AttemptMutationSendOutcome:
    """Interpret one progress send and bind any success to its exact command."""

    response = _success_response_or_send_outcome(
        ProviderOperation.EXECUTION_ATTEMPT_PROGRESS,
        outcome,
    )
    if type(response) is not ProviderHttpResponse:
        return response
    return _receipt_outcome(
        parse_execution_attempt_progress_success(prepared, response)
    )


def interpret_execution_attempt_complete(
    prepared: _PreparedProviderRequest,
    outcome: ProviderHttpsOutcome,
) -> AttemptMutationSendOutcome:
    """Interpret one completion send against the retained terminal command."""

    response = _success_response_or_send_outcome(
        ProviderOperation.EXECUTION_ATTEMPT_COMPLETE,
        outcome,
    )
    if type(response) is not ProviderHttpResponse:
        return response
    return _receipt_outcome(
        parse_execution_attempt_complete_success(prepared, response)
    )


def interpret_execution_attempt_fail(
    prepared: _PreparedProviderRequest,
    outcome: ProviderHttpsOutcome,
) -> AttemptMutationSendOutcome:
    """Interpret one failure send against the retained terminal command."""

    response = _success_response_or_send_outcome(
        ProviderOperation.EXECUTION_ATTEMPT_FAIL,
        outcome,
    )
    if type(response) is not ProviderHttpResponse:
        return response
    return _receipt_outcome(parse_execution_attempt_fail_success(prepared, response))


_PROBLEMS_PROVING_NO_COMMIT = frozenset(
    {400, 401, 403, 404, 408, 413, 414, 431}
)


def _success_response_or_send_outcome(
    operation: ProviderOperation,
    outcome: ProviderHttpsOutcome,
) -> ProviderHttpResponse | AttemptMutationSendOutcome:
    if type(outcome) is ProviderHttpResponse:
        if outcome.status == 200:
            return outcome
        problem = parse_provider_problem(operation, outcome)
        return (
            AttemptMutationNotCommitted(problem)
            if type(problem) is ProviderProblem
            and problem.status in _PROBLEMS_PROVING_NO_COMMIT
            else AttemptMutationCommitPossible(problem)
        )
    if type(outcome) is ProviderTlsRejected:
        return AttemptMutationNotCommitted(outcome)
    if type(outcome) is ProviderRequestUnavailable:
        return (
            AttemptMutationNotCommitted(outcome)
            if outcome.delivery is RequestDelivery.NOT_SENT
            else AttemptMutationCommitPossible(outcome)
        )
    if type(outcome) is ProviderResponseRejected:
        return AttemptMutationCommitPossible(outcome)
    raise TypeError("Attempt outcome interpretation requires HTTPS send evidence")


def _receipt_outcome(
    receipt: AttemptMutationReceipt | ProviderSuccessRejected,
) -> AttemptMutationSendOutcome:
    return (
        AttemptMutationCommitPossible(receipt)
        if type(receipt) is ProviderSuccessRejected
        else AttemptMutationCommitted(receipt)
    )
