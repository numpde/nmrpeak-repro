#!/usr/bin/env python3
"""Route acquisition HTTP/CONNECT traffic through one explicit interface.

DNS and TCP sockets bind the selected device without an unbound resolver
fallback. This IPv4-only loopback proxy supplies cooperative routing; the
acquisition launcher owns process lifetime and container resources.
"""

# Adapted from the SECS interface-bound proxy so acquisition uses the same
# SO_BINDTODEVICE transport; this repository runs the proxy inside a container.
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tools/bound_http_proxy.py

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import argparse
import ipaddress
import os
import re
import secrets
import select
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

# Header admission owns this fixed byte ceiling independently of coalesced body
# bytes; changing it would alter the bounded acquisition protocol.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
MAX_HEADER_BYTES = 64 * 1024
# Relay owns one bounded forwarding chunk rather than an accumulated body.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
BUFFER_BYTES = 128 * 1024
# Header reads share one absolute admission budget across all fragments.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
HEADER_SECONDS = 30
# DNS and destination addresses share the connection owner's finite budget.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
CONNECT_SECONDS = 30
# Relay renews this inactivity budget only after a completed bounded write.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
RELAY_IDLE_SECONDS = 120
# The listener reserves this fixed worker capacity before allocating threads.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L31-L36
# repo-check: allow-module-constant
MAX_CONNECTIONS = 32


class ProxyResolutionFailed(OSError):
    """Safe owned DNS reason; native query failures remain private evidence."""

    # silent-defaults: allow-contract -- Missing resolver configuration has no native query failures; failed queries supply their original exceptions.
    def __init__(self, message, failures=()):
        super().__init__(message)
        self.failures = tuple(failures)


class ProxyResolutionDeadline(ProxyResolutionFailed, TimeoutError):
    """DNS aggregate expiry retains earlier causes and the timeout contract."""


class DNSResponseRejected(OSError):
    """Code-owned protocol text; only the parsed numeric rcode is interpolated."""


class DNSNameRejected(ValueError):
    """Code-owned name-decoder text, preserving its ValueError contract."""


def _remaining(deadline: float, operation: str) -> float:
    """Return seconds left in one absolute budget, rejecting completed deadlines."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Cannot complete proxy {operation}: its deadline elapsed.")
    return remaining


def receive_headers(client: socket.socket) -> tuple[bytes, bytes]:
    """Return the complete header including its terminator and any received body.

    One absolute 30-second budget covers every fragment. The byte limit covers
    only the header. Body bytes received with its final fragment remain
    available to the forwarding caller. EOF before the header
    completes raises ConnectionError; an oversized header raises ValueError.
    """
    deadline = time.monotonic() + HEADER_SECONDS
    data = bytearray()
    while True:
        # A fragment arriving after the deadline cannot complete admission.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L54-L56
        client.settimeout(_remaining(deadline, "header read"))
        chunk = client.recv(8192)
        _remaining(deadline, "header read")
        if not chunk:
            raise ConnectionError("client closed before sending headers")
        data.extend(chunk)
        terminator = data.find(b"\r\n\r\n")
        # Coalesced body bytes belong to forwarding, not header admission.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L88
        header_bytes = terminator + 4 if terminator >= 0 else len(data)
        if not (header_bytes <= MAX_HEADER_BYTES):
            raise ValueError("request headers are too large")
        if terminator >= 0:
            return bytes(data[:header_bytes]), bytes(data[header_bytes:])


def parse_destination(header: bytes) -> tuple[str, str, int, bytes]:
    """Admit a destination without transport and return its forwarding header.

    CONNECT retains its header; absolute HTTP targets become origin-form paths.
    Invalid destinations, including ports outside 1-65535, raise ValueError.
    """
    lines = header.split(b"\r\n")
    method, target, version = lines[0].decode("ascii").split(" ", 2)
    method = method.upper()

    if method == "CONNECT":
        host, separator, port_text = target.rpartition(":")
        if not separator or not host:
            raise ValueError("CONNECT target must be host:port")
    else:
        parsed = urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("proxy request must use an absolute HTTP URL")
        host = parsed.hostname

    try:
        port = int(port_text) if method == "CONNECT" else parsed.port
    except ValueError:
        raise ValueError("Cannot admit proxy destination: the port must be an integer from 1 to 65535.") from None
    if port is None:
        # RFC 9110 sections 4.2.1 and 4.2.2 define omitted HTTP/HTTPS ports:
        # https://www.rfc-editor.org/rfc/rfc9110.html#section-4.2.1
        # https://www.rfc-editor.org/rfc/rfc9110.html#section-4.2.2
        # silent-defaults: allow-contract -- Omitted URI ports mean HTTP 80 or HTTPS 443; proof=packages/provider-core/tests/unit/normative/test_bound_http_proxy.py::BoundProxyTests.test_destination_port_boundaries_and_scheme_defaults_are_preserved
        port = 443 if parsed.scheme == "https" else 80
    # Admit the destination before DNS or socket use; explicit HTTP port zero
    # must not silently choose the scheme default.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L41
    if not 1 <= port <= 65535:
        raise ValueError("Cannot admit proxy destination: the port must be an integer from 1 to 65535.")
    if method == "CONNECT":
        return method, host, port, header

    # RFC 9112 section 3.2.1: https://www.rfc-editor.org/rfc/rfc9112.html#section-3.2.1
    # silent-defaults: allow-contract -- Origin-form requires / for an empty URI path; proof=packages/provider-core/tests/unit/normative/test_bound_http_proxy.py::BoundProxyTests.test_empty_absolute_path_becomes_origin_slash
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    lines[0] = f"{method} {path} {version}".encode("ascii")
    return method, host, port, b"\r\n".join(lines)


# DNS queries and destination connections both use the selected device.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L96-L298
def _dns_name(name: str) -> bytes:
    """Encode bounded IDNA labels in DNS wire form."""

    labels = name.rstrip(".").encode("idna").split(b".")
    if len(name.encode("idna")) > 253 or not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError(
            "Cannot resolve proxy destination: require a DNS name of at most 253 bytes with labels of 1 through 63 bytes."
        )
    return b"".join(bytes((len(label),)) + label for label in labels) + b"\0"


# Each top-level name starts with no followed pointers; recursive calls below
# supply depth + 1. Sixteen hops is this decoder's existing local bound.
# silent-defaults: allow-contract -- A new DNS name starts its pointer recursion count at zero; proof=packages/provider-core/tests/unit/normative/test_bound_http_proxy.py::BoundProxyTests.test_dns_name_depth_starts_at_zero_and_bounds_compression
def _read_dns_name(message: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    """Decode a DNS name with at most sixteen compression-pointer hops.

    Return the decoded text and the byte after its original encoded representation.
    """

    if not (depth <= 16):
        raise DNSNameRejected("DNS compression pointer recursion is too deep")
    labels: list[bytes] = []
    next_offset: int | None = None
    while True:
        if not (offset < len(message)):
            raise DNSNameRejected("truncated DNS name")
        size = message[offset]
        if size & 0xC0 == 0xC0:
            if not (offset + 1 < len(message)):
                raise DNSNameRejected("truncated DNS compression pointer")
            pointer = ((size & 0x3F) << 8) | message[offset + 1]
            suffix, _ = _read_dns_name(message, pointer, depth + 1)
            if suffix:
                labels.extend(part.encode("ascii") for part in suffix.split("."))
            next_offset = next_offset or offset + 2
            break
        if size & 0xC0:
            raise DNSNameRejected("invalid DNS label encoding")
        offset += 1
        if size == 0:
            next_offset = next_offset or offset
            break
        if not (offset + size <= len(message)):
            raise DNSNameRejected("truncated DNS label")
        labels.append(message[offset : offset + size])
        offset += size
    try:
        return b".".join(labels).decode("ascii").lower(), next_offset
    except UnicodeDecodeError as cause:
        raise DNSNameRejected("DNS name contains a non-ASCII label") from cause


def _dns_servers() -> tuple[str, ...]:
    """Choose configured non-loopback IPv4 resolvers instead of a local DNS stub."""

    paths = (Path("/run/systemd/resolve/resolv.conf"), Path("/etc/resolv.conf"))
    for path in paths:
        try:
            lines = path.read_text(encoding="ascii").splitlines()
        except OSError:
            continue
        servers = []
        for line in lines:
            fields = line.split()
            if len(fields) == 2 and fields[0] == "nameserver":
                try:
                    address = ipaddress.ip_address(fields[1])
                except ValueError:
                    continue
                if address.version == 4 and not address.is_loopback:
                    servers.append(str(address))
        if servers:
            return tuple(dict.fromkeys(servers))
    raise ProxyResolutionFailed("Cannot resolve proxy destination: no non-loopback IPv4 DNS server is configured.")


def _query_ipv4(
    host: str,
    server: str,
    interface: str,
    *,
    deadline: float,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Query IPv4 DNS on the chosen device within the shared connection budget."""

    query_deadline = min(deadline, time.monotonic() + 3)
    _remaining(query_deadline, "DNS query")
    transaction = secrets.randbits(16)
    question = _dns_name(host) + struct.pack("!HH", 1, 1)
    request = struct.pack("!HHHHHH", transaction, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver:
        if interface != "system":
            resolver.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
        resolver.settimeout(_remaining(query_deadline, "DNS query"))
        resolver.connect((server, 53))
        resolver.settimeout(_remaining(query_deadline, "DNS query"))
        resolver.send(request)
        resolver.settimeout(_remaining(query_deadline, "DNS query"))
        response = resolver.recv(65535)
        # Native timeouts bound the wait, but a late successful return must
        # also fail admission. This closes the inherited upstream deadline gap.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L183-L184
        _remaining(query_deadline, "DNS query")
    if len(response) < 12:
        raise DNSResponseRejected("truncated DNS response")
    response_id, flags, questions, answers, authorities, additional = struct.unpack("!HHHHHH", response[:12])
    if response_id != transaction or not flags & 0x8000:
        raise DNSResponseRejected("mismatched DNS response")
    if flags & 0x0200:
        raise DNSResponseRejected("truncated UDP DNS response")
    if flags & 0x000F:
        raise DNSResponseRejected(f"DNS server returned rcode {flags & 0x000F}")
    if questions != 1:
        raise DNSResponseRejected("DNS response has the wrong question count")
    offset = 12
    question_name, offset = _read_dns_name(response, offset)
    if offset + 4 > len(response):
        raise DNSResponseRejected("truncated DNS question")
    question_type, question_class = struct.unpack("!HH", response[offset : offset + 4])
    offset += 4
    expected_name = host.rstrip(".").encode("idna").decode("ascii").lower()
    if question_name != expected_name or (question_type, question_class) != (1, 1):
        raise DNSResponseRejected("DNS response question does not match the request")
    addresses: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    for _ in range(answers + authorities + additional):
        owner, offset = _read_dns_name(response, offset)
        if offset + 10 > len(response):
            raise DNSResponseRejected("truncated DNS record")
        record_type, record_class, _ttl, size = struct.unpack("!HHIH", response[offset : offset + 10])
        offset += 10
        end = offset + size
        if end > len(response):
            raise DNSResponseRejected("truncated DNS record data")
        if record_class == 1 and record_type == 1 and size == 4:
            # silent-defaults: allow-contract -- Each response-local owner bucket contains only its received A records; proof=repository_checks/test_proxy_posture.py::ImageBuildProxyTransportTests.test_dns_groups_only_received_a_records_by_owner
            addresses.setdefault(owner, []).append(socket.inet_ntoa(response[offset:end]))
        elif record_class == 1 and record_type == 5:
            aliases[owner], _ = _read_dns_name(response, offset)
        offset = end
    return addresses, aliases


def resolve_ipv4_bound(host: str, interface: str, *, deadline: float) -> tuple[str, ...]:
    """Follow bounded IPv4 aliases without using an unbound system resolver."""
    try:
        return (str(ipaddress.IPv4Address(host)),)
    # silent-defaults: allow-contract -- A destination that is not a literal IPv4 address proceeds to interface-bound DNS; proof=repository_checks/test_proxy_posture.py::ImageBuildProxyTransportTests.test_expired_dns_budget_does_not_start_the_next_server
    except ipaddress.AddressValueError:
        pass
    normalized_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    errors = []
    failures = []
    for server in _dns_servers():
        try:
            _remaining(deadline, "DNS resolution")
        except TimeoutError as expired:
            raise ProxyResolutionDeadline(
                "Cannot resolve proxy destination: the shared DNS deadline elapsed.", (*failures, expired)
            ) from expired
        try:
            current = normalized_host
            visited = set()
            for _ in range(16):
                if current in visited:
                    raise DNSResponseRejected("DNS response contains an alias cycle")
                visited.add(current)
                addresses, aliases = _query_ipv4(current, server, interface, deadline=deadline)
                if current in addresses:
                    return tuple(addresses[current])
                if current not in aliases:
                    break
                current = aliases[current]
            errors.append(f"{server}: response contains no IPv4 address for {host}")
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{server}: {error}")
            failures.append(error)
    # silent-defaults: allow-contract -- Distinguish failed DNS queries from completed replies containing no IPv4 address; neither outcome selects another route.
    reason = "the configured DNS queries failed" if failures else "the configured resolvers did not return an IPv4 address"
    failure = ProxyResolutionFailed(f"Cannot resolve proxy destination on the selected interface: {reason}.", failures)
    # Preserve source-bearing evidence privately; service logs use only the
    # owned reason above and bounded native exception classes/errno.
    failure.add_note("; ".join(errors))
    raise failure


def connect_bound(host: str, port: int, interface: str) -> socket.socket:
    """Return one device-bound connection within a shared DNS/address budget."""

    deadline = time.monotonic() + CONNECT_SECONDS
    last_error: OSError | None = None
    for address in resolve_ipv4_bound(host, interface, deadline=deadline):
        _remaining(deadline, "connection")
        outbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if interface != "system":
                outbound.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
            outbound.settimeout(_remaining(deadline, "connection"))
            outbound.connect((address, port))
            # A late successful connect still owns an open socket; reject it
            # inside this try so the existing failure path closes the socket.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L267-L272
            _remaining(deadline, "connection")
            return outbound
        except OSError as error:
            last_error = error
            outbound.close()
    raise last_error or OSError("Cannot connect proxy destination: DNS returned no IPv4 address.")


def relay(left: socket.socket, right: socket.socket) -> None:
    """Preserve TCP half-closes within the 120-second forwarding inactivity budget."""

    readers = [left, right]
    deadline = time.monotonic() + RELAY_IDLE_SECONDS
    while readers:
        readable, _, _ = select.select(readers, (), (), _remaining(deadline, "relay"))
        if not readable:
            raise TimeoutError("Cannot continue proxy relay: no forwarding completed within 120 seconds.")
        for source in readable:
            destination = right if source is left else left
            source.settimeout(_remaining(deadline, "relay read"))
            data = source.recv(BUFFER_BYTES)
            if not data:
                # EOF closes only this read direction. The opposite direction
                # may still carry the response to a completed client upload.
                readers.remove(source)
                destination.shutdown(socket.SHUT_WR)
                continue
            # A writable peer may still stall after select. Bound sendall too,
            # and reset inactivity only when actual forwarding completes.
            destination.settimeout(_remaining(deadline, "relay write"))
            destination.sendall(data)
            # Only a write completed within the budget can renew inactivity.
            # Upstream resets immediately after sendall, accepting late success.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L296-L298
            _remaining(deadline, "relay write")
            deadline = time.monotonic() + RELAY_IDLE_SECONDS


class ProxyHandler(socketserver.BaseRequestHandler):
    def destination(self, header):
        """Acquisition accepts CONNECT and absolute HTTP destinations."""
        return parse_destination(header)

    def connect(self, host, port):
        """Acquire the admitted destination through this listener's device."""
        return connect_bound(host, port, self.server.outbound_interface)

    def admission_reply(self, _error):
        """Keep the acquisition listener's existing fixed failure response."""
        return b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"

    def report_failure(self, phase, error):
        """Native causes may contain private request values; expose phase/type."""
        sys.stderr.write(f"proxy {phase} failed: {type(error).__name__}\n")

    def handle(self) -> None:
        """Forward one request, reporting failures without private request values.

        A fixed HTTP error is emitted only before forwarding is attempted. Once
        a write starts, its completion may be uncertain and a second HTTP status
        would corrupt the forwarded response or CONNECT tunnel.
        """
        phase = "request admission"
        try:
            header, remainder = receive_headers(self.request)
            method, host, port, forwarded_header = self.destination(header)
            with self.connect(host, port) as outbound:
                # sendall may fail after writing a prefix; mark the phase before
                # the first HTTP header or CONNECT-success write is attempted.
                # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L296
                phase = "forwarding"
                # Header admission leaves its remaining timeout on the client;
                # forwarding owns a fresh bounded write budget for both peers.
                # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L312-L315
                self.request.settimeout(RELAY_IDLE_SECONDS)
                outbound.settimeout(RELAY_IDLE_SECONDS)
                if method == "CONNECT":
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    outbound.sendall(forwarded_header)
                if remainder:
                    outbound.sendall(remainder)
                relay(self.request, outbound)
        except (ConnectionError, OSError, UnicodeError, ValueError) as error:
            if phase == "request admission":
                try:
                    self.request.settimeout(1)
                    self.request.sendall(self.admission_reply(error))
                # silent-defaults: allow-contract -- A failed fixed 502 reply must still reach the original safe request-admission diagnostic; proof=packages/provider-core/tests/unit/normative/test_bound_http_proxy.py::BoundProxyTests.test_failed_admission_reply_preserves_original_safe_cause_and_unexpected_errors
                except OSError:
                    pass
            # Native causes can contain request URLs, headers or other private
            # values. The operator receives only the owned phase and class.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L277
            self.report_failure(phase, error)


class ConnectionAdmission:
    """Bound active connections while allowing the owner to change the limit."""

    def __init__(self, limit):
        self._lock = threading.Lock()
        self._active = 0
        self.set_limit(limit)

    def set_limit(self, limit):
        if not (type(limit) is int and 0 < limit):
            raise ValueError("Proxy connection capacity must be a positive integer.")
        with self._lock:
            self._limit = limit

    def acquire(self):
        with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    def load(self):
        with self._lock:
            return self._active, self._limit

    def release(self):
        with self._lock:
            if self._active == 0:
                raise RuntimeError("Proxy connection capacity was released without an owner.")
            self._active -= 1


class ProxyServer(socketserver.ThreadingTCPServer):
    """Reserve finite worker capacity before allocating a request thread."""

    allow_reuse_address = True
    daemon_threads = True
    request_handler = ProxyHandler

    def __init__(self, address: tuple[str, int], interface: str):
        """Own the selected device and the bounded worker slots for this listener."""
        self.outbound_interface = interface
        self._slots = ConnectionAdmission(MAX_CONNECTIONS)
        super().__init__(address, self.request_handler)

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        """Close overload immediately; transfer each reserved slot to its worker."""
        # Admission precedes ThreadingMixIn's allocation. A failed thread start
        # keeps ownership here, while a started worker releases in its finally.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L343-L365
        if not self._slots.acquire():
            self.shutdown_request(request)
            return
        self.start_reserved_request(request, client_address)

    def start_reserved_request(self, request, client_address):
        """Transfer an already reserved slot to a worker, or release it on failure."""
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        """Release the worker's slot after success, request failure or cleanup."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, _request: socket.socket, _client_address: tuple[str, int]) -> None:
        """Replace socketserver's request-bearing traceback with a fixed diagnostic."""
        # A worker exception must not bypass the handler's request privacy rule.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L367-L370
        sys.stderr.write("Cannot serve proxy request: its worker failed; request contents are not logged.\n")


def require_interface_name(interface: str) -> None:
    """Admit the Linux interface grammar without claiming a host device exists."""
    # Native lookup admits the same device name consumed by SO_BINDTODEVICE.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L373-L381
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", interface):
        raise ValueError(
            "Cannot start proxy: require an ASCII interface name of 1 through 15 bytes, without paths or whitespace."
        )


def require_interface(interface: str) -> None:
    """Admit one existing native Linux interface without filesystem lookup."""
    require_interface_name(interface)
    try:
        socket.if_nametoindex(interface)
    except OSError:
        raise ValueError("Cannot start proxy: the selected network interface does not exist.") from None


def publish_readiness(ready_file, content):
    """Publish complete private readiness without replacing any prior receipt."""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".proxy-ready.", dir=ready_file.parent)
    except OSError:
        raise SystemExit("Cannot start proxy: a private readiness staging file could not be created.") from None

    published = False
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as ready:
            ready.write(content)
        # mkstemp creates private bytes. Linking after close publishes
        # only complete content and cannot overwrite a file or symlink.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L398-L410
        os.link(temporary, ready_file, follow_symlinks=False)
        published = True
    except OSError as error:
        raise SystemExit(f"Cannot start proxy: readiness publication failed ({type(error).__name__}).") from None
    finally:
        try:
            Path(temporary).unlink()
        except OSError:
            # Upstream can report creation failure after link succeeded.
            # The complete receipt remains usable; only private staging-alias
            # cleanup is unconfirmed. Never roll back by a replaceable path.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L406-L410
            # silent-defaults: allow-contract -- Report the observed publication outcome independently of failed temporary-alias cleanup; proof=repository_checks/test_proxy_posture.py::ImageBuildProxyWorkerTests.test_readiness_publication_failure_survives_temporary_cleanup_failure
            outcome = (
                "Readiness was published; serving will continue." if published else "Readiness publication did not complete."
            )
            sys.stderr.write(f"{outcome} Cannot confirm temporary readiness cleanup at {temporary}.\n")


def main() -> None:
    """Publish exclusive private readiness after listener and device admission.

    The acquisition launcher explicitly supplies port zero for kernel selection.
    Direct CLI callers must supply --port (use --port 0 for the same policy);
    omission is a parse error before interface lookup or listener allocation.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", required=True)
    parser.add_argument("--port", type=int, required=True, help="Listener port; 0 asks the kernel to choose a free port.")
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.interface != "system":
            require_interface(args.interface)
    except ValueError as error:
        raise SystemExit(str(error)) from None

    # Prove interface binding before publishing readiness, without any transfer.
    if args.interface != "system":
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0")

    with ProxyServer(("127.0.0.1", args.port), args.interface) as server:
        publish_readiness(args.ready_file, f"{server.server_address[1]}\n")
        server.serve_forever()


if __name__ == "__main__":
    main()
