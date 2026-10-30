"""Bind SDK API reads to stopped journal maintenance and settle their owner."""

from contextlib import asynccontextmanager

from provider_core.api import ProviderAPI

from .provider_diagnostics import describe_nmrpeak_failure

from .attempt_journal_store import AttemptJournalConflict
from .journal_identity import journal_api_authority_id
from .provider_startup import load_maintenance_api


def load_journal_api(*, proxy_socket, expected_provider_ref, expected_authority_id):
    """Construct one API owner from captured inputs, before any HTTP effect."""
    settings, provider_ref, _publication = load_maintenance_api(proxy_socket)
    if provider_ref != expected_provider_ref:
        raise AttemptJournalConflict("Selected API binding belongs to another provider; preserve the retained journal")
    authority = journal_api_authority_id(origin=settings.identity.origin, topology=settings.expected_topology)
    if authority != expected_authority_id:
        raise AttemptJournalConflict("Selected API namespace differs from the retained journal; inspect the matching deployment binding")
    return ProviderAPI(settings.identity, settings.http.new_transport(), expected_topology=settings.expected_topology)


@asynccontextmanager
async def settled_journal_api(api):
    """Retire the same original API owner while preserving independent errors."""
    primary = None
    try:
        yield api
    except BaseException as cause:
        primary = cause
        raise
    finally:
        try:
            await api.retire()
        except BaseException as retirement:
            if primary is not None and retirement is not primary:
                raise BaseExceptionGroup(
                    "Journal operation failed and API retirement raised another error", [primary, retirement],
                ) from None
            raise


async def require_empty_live_inventory(api, *, maximum_pages):
    """Require every bounded page to be empty before permitting journal deletion."""
    if type(maximum_pages) is not int or maximum_pages < 1:
        raise ValueError("Attempt inventory page bound must be positive")
    cursor = None
    seen_cursors = set()
    for _ in range(maximum_pages):
        page = await api.execution_attempts(state="in_progress", limit=50, cursor=cursor)
        if page.attempts:
            raise AttemptJournalConflict("Provider journal retirement found in-progress Attempts; preserve the journal")
        if page.next_cursor is None:
            return
        if page.next_cursor in seen_cursors:
            raise AttemptJournalConflict("Attempt inventory repeats a page cursor; preserve the journal")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    raise AttemptJournalConflict("Attempt inventory exceeds its configured page bound; preserve the journal")


def journal_api_failure(error, api):
    """Keep caller failure and the SDK's canonical API observation available."""
    from provider_core.diagnostics import failure_evidence

    evidence = {"failure": failure_evidence(error, secrets=(), describe_owner=describe_nmrpeak_failure)}
    if api is not None and api.last_failure is not None:
        evidence["api_failure"] = failure_evidence(api.last_failure, secrets=(), describe_owner=describe_nmrpeak_failure)
    return evidence


async def _inventory(api, maximum_pages):
    async with settled_journal_api(api):
        await require_empty_live_inventory(api, maximum_pages=maximum_pages)


def main(arguments=None):
    """The host holds identity exclusion through this read and exact volume removal."""
    import argparse
    import asyncio
    import json
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy-socket', required=True)
    parser.add_argument('--expected-provider-ref', required=True)
    parser.add_argument('--expected-authority-id', required=True)
    parser.add_argument('--maximum-pages', required=True, type=int)
    args = parser.parse_args(arguments)
    api = None
    try:
        api = load_journal_api(proxy_socket=args.proxy_socket,
                               expected_provider_ref=args.expected_provider_ref,
                               expected_authority_id=args.expected_authority_id)
        asyncio.run(_inventory(api, args.maximum_pages))
        print('complete-empty')
        return 0
    except BaseException as error:
        print(json.dumps({'operation': 'prove empty live Attempt inventory',
                          **journal_api_failure(error, api)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
