"""Fetch the pinned shared tool through an already-owned isolated relay."""

from __future__ import annotations

import errno
import http.client
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from repository_checks.wheelhouse_tool_asset import (
    MAX_BYTES, TOOL_URL, ToolInterrupted, TransientToolDownload, _retry_after, ensure_tool,
)


class _RequiredProxy(urllib.request.ProxyHandler):
    """Never consult environment bypass rules for the explicitly granted relay."""

    def proxy_open(self, request, proxy, _scheme):
        request.set_proxy(urlsplit(proxy).netloc, 'http')
        return None


class _HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise ValueError('Cannot acquire the shared tool: redirect must use HTTPS.')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _network_failure(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLError):
        raise ValueError('Cannot acquire the shared tool: TLS verification failed.') from None
    if isinstance(reason, socket.gaierror):
        raise ValueError('Cannot acquire the shared tool: name resolution failed.') from None
    tunnel = re.fullmatch(r'Tunnel connection failed: ([0-9]{3})(?: .*)?', str(reason))
    if tunnel:
        status = int(tunnel[1])
        if status in (429, 503):
            raise ValueError(f'Cannot acquire the shared tool: proxy tunnel HTTP {status}; Retry-After is unavailable.') from None
        if status in (408, 500, 502, 504):
            raise TransientToolDownload(f'HTTP {status}') from None
        raise ValueError(f'Cannot acquire the shared tool: proxy tunnel HTTP {status}.') from None
    if isinstance(reason, (TimeoutError, ConnectionError)) or (
        isinstance(reason, OSError) and reason.errno in (errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE)
    ):
        raise TransientToolDownload('the connection or transfer failed') from None
    raise ValueError('Cannot acquire the shared tool: the network request failed.') from None


def download(output, _directory, _interface):
    proxy = os.environ.get('HTTPS_PROXY', '')
    match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})', proxy)
    if match is None or int(match[1]) > 65535:
        raise ValueError('Cannot acquire the shared tool: the isolated relay address is invalid.')
    opener = urllib.request.build_opener(_RequiredProxy({'https': proxy}), _HTTPSRedirects())

    def expired(_signum, _frame):
        raise TransientToolDownload('the transfer timed out')

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 300)
    try:
        try:
            response = opener.open(TOOL_URL, timeout=30)
        except urllib.error.HTTPError as error:
            try:
                status = error.code
                if status in (408, 429, 500, 502, 503, 504):
                    delay = _retry_after(error.headers.get('Retry-After', '')) if status in (429, 503) else 0
                    raise TransientToolDownload(f'HTTP {status}', delay) from None
                raise ValueError(f'Cannot acquire the shared tool: HTTP {status}.') from None
            finally:
                error.close()
        except (urllib.error.URLError, OSError) as error:
            _network_failure(error)
        with response, os.fdopen(os.dup(output), 'wb') as destination:
            size = 0
            while True:
                try:
                    chunk = response.read(64 * 1024)
                except http.client.IncompleteRead:
                    raise TransientToolDownload('the connection or transfer failed') from None
                except (urllib.error.URLError, OSError) as error:
                    _network_failure(error)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('The downloaded shared tool exceeds 16 MiB.')
                destination.write(chunk)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] or previous_timer[1]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pool', type=Path)
    args = parser.parse_args()

    def interrupted(_signum, _frame):
        for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(number, signal.SIG_IGN)
        raise ToolInterrupted('The shared wheelhouse tool operation was interrupted.')

    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
    try:
        if not ensure_tool(args.pool, 'system', download=download):
            raise ValueError('The shared wheelhouse tool could not be acquired.')
    except (OSError, ValueError, ToolInterrupted) as error:
        parser.exit(1, f'{error}\n')
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    main()
