"""Bind startup identity and exclusion before entering provider operations."""

from __future__ import annotations

from pathlib import Path
from threading import Event

from .attempt_journal_store import AttemptJournalStore
from .generation_runtime import GenerationRuntime
from .provider_api import ProviderApiClient
from .provider_credential import parse_provider_signing_credential
from .provider_https import ProviderHttpsEndpoint
from .provider_identity_lock import ProviderIdentityLock
from .provider_process import ProviderProcessPolicy, run_provider_process
from .provider_requests import _PreparedProviderRequest
from .runner_session import RunnerSession


def run_locked_provider(
    *,
    credential_bytes: bytes,
    endpoint: ProviderHttpsEndpoint,
    runtime: GenerationRuntime,
    identity_lock_path: Path,
    journal: AttemptJournalStore,
    hf_session: RunnerSession,
    chf_session: RunnerSession,
    hello: _PreparedProviderRequest,
    policy: ProviderProcessPolicy,
    stop: Event,
) -> None:
    """Hold the credential's provider identity across all hello and Job work."""

    credential = parse_provider_signing_credential(credential_bytes)
    provider_ref = runtime.hf.generation.provider_ref
    if credential.provider_ref != provider_ref:
        raise ValueError(
            "Provider signing credential belongs to another frozen provider"
        )
    with ProviderIdentityLock.acquire(identity_lock_path, provider_ref):
        api = ProviderApiClient(
            endpoint,
            credential.credential_ref,
            credential.private_key,
        )
        run_provider_process(
            runtime=runtime,
            api=api,
            journal=journal,
            hf_session=hf_session,
            chf_session=chf_session,
            hello=hello,
            policy=policy,
            stop=stop,
        )
