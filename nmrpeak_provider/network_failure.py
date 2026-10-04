"""Describe provider HTTPS transport causes without exception-owned text.

Adapted from secs-repro's network_errors.py. Request URLs, peer data, and
certificate messages can appear in exception strings, so this projection uses
only known exception classes and operating-system error numbers.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl


def network_failure_reason(error: BaseException) -> str:
    if isinstance(error, socket.gaierror):
        return "the API network address could not be resolved"
    if isinstance(error, ssl.SSLCertVerificationError):
        code = getattr(error, "verify_code", None)
        suffix = f" (verification code {code})" if type(code) is int else ""
        return "the API TLS certificate could not be verified" + suffix
    if isinstance(error, ssl.SSLError):
        return "the encrypted API connection failed"
    if isinstance(error, TimeoutError):
        return "the API connection timed out"
    if isinstance(error, http.client.RemoteDisconnected):
        return "the API closed the connection without a reply"
    if isinstance(error, http.client.IncompleteRead):
        return "the API reply ended before all declared bytes arrived"
    if isinstance(error, EOFError):
        return "the API reply ended before completion"
    if isinstance(error, (http.client.BadStatusLine, http.client.LineTooLong)):
        return "the API returned an unreadable HTTP response"
    if isinstance(error, http.client.HTTPException):
        return "the API HTTP exchange could not be completed"
    if isinstance(error, ConnectionRefusedError):
        return "the API connection was refused"
    if isinstance(error, ConnectionResetError):
        return "the API connection was reset"
    if isinstance(error, ConnectionAbortedError):
        return "the API connection was aborted"
    if isinstance(error, BrokenPipeError):
        return "the API connection closed during send"
    if isinstance(error, OSError):
        code = getattr(error, "errno", None)
        return (
            f"operating-system error {code}: {os.strerror(code)}"
            if type(code) is int and code > 0
            else "an operating-system error interrupted the API connection"
        )
    return "the API connection failed without a classified transport cause"
