"""Low-level release mechanics for unusable streamed HTTP responses."""

from __future__ import annotations

import asyncio

import httpx


async def best_effort_release_response(
    response: httpx.Response,
    *,
    deadline: float,
) -> None:
    """Try to release a stream without replacing the primary operation outcome.

    ``Response.aclose()`` marks a response closed before awaiting its stream.
    If that await is interrupted, retrying the response method can therefore
    become a no-op while the transport still owns the connection. Failure-path
    cleanup must address the stream directly and stay within its caller's
    existing deadline; it never creates a second resource-policy budget.
    """

    try:
        async with asyncio.timeout_at(deadline):
            await response.stream.aclose()
    except Exception:
        # The primary delivery or cancellation outcome remains authoritative.
        pass
