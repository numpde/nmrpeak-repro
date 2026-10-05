#!/usr/bin/env python3
"""Minimal build-only HTTP CONNECT proxy bound to one outbound interface."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import secrets
import select
import socket
import socketserver
import struct
import sys
from urllib.parse import urlsplit


MAX_HEADER_BYTES = 64 * 1024
BUFFER_BYTES = 128 * 1024


def receive_headers(client: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = client.recv(8192)
        if not chunk:
            raise ConnectionError("client closed before sending headers")
        data.extend(chunk)
        if len(data) > MAX_HEADER_BYTES:
            raise ValueError("request headers are too large")
    header_end = data.index(b"\r\n\r\n") + 4
    return bytes(data[:header_end]), bytes(data[header_end:])


def parse_destination(header: bytes) -> tuple[str, str, int, bytes]:
    lines = header.split(b"\r\n")
    method, target, version = lines[0].decode("ascii").split(" ", 2)
    method = method.upper()

    if method == "CONNECT":
        host, separator, port_text = target.rpartition(":")
        if not separator or not host:
            raise ValueError("CONNECT target must be host:port")
        return method, host, int(port_text), header

    parsed = urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("proxy request must use an absolute HTTP URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    lines[0] = f"{method} {path} {version}".encode("ascii")
    return method, parsed.hostname, port, b"\r\n".join(lines)


def _dns_name(name: str) -> bytes:
    labels = name.rstrip(".").encode("idna").split(b".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError(f"invalid DNS name: {name!r}")
    return b"".join(bytes((len(label),)) + label for label in labels) + b"\0"


def _read_dns_name(message: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    if depth > 16:
        raise ValueError("DNS compression pointer recursion is too deep")
    labels: list[bytes] = []
    next_offset: int | None = None
    while True:
        if offset >= len(message):
            raise ValueError("truncated DNS name")
        size = message[offset]
        if size & 0xC0 == 0xC0:
            if offset + 1 >= len(message):
                raise ValueError("truncated DNS compression pointer")
            pointer = ((size & 0x3F) << 8) | message[offset + 1]
            suffix, _ = _read_dns_name(message, pointer, depth + 1)
            if suffix:
                labels.extend(part.encode("ascii") for part in suffix.split("."))
            next_offset = next_offset or offset + 2
            break
        if size & 0xC0:
            raise ValueError("invalid DNS label encoding")
        offset += 1
        if size == 0:
            next_offset = next_offset or offset
            break
        if offset + size > len(message):
            raise ValueError("truncated DNS label")
        labels.append(message[offset:offset + size])
        offset += size
    return b".".join(labels).decode("ascii").lower(), next_offset


def _dns_servers() -> tuple[str, ...]:
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
    raise OSError("no non-loopback IPv4 DNS servers are configured")


def _query_ipv4(host: str, server: str, interface: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    transaction = secrets.randbits(16)
    question = _dns_name(host) + struct.pack("!HH", 1, 1)
    request = struct.pack("!HHHHHH", transaction, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver:
        resolver.setsockopt(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0"
        )
        resolver.settimeout(3)
        resolver.connect((server, 53))
        resolver.send(request)
        response = resolver.recv(65535)
    if len(response) < 12:
        raise OSError("truncated DNS response")
    response_id, flags, questions, answers, authorities, additional = struct.unpack(
        "!HHHHHH", response[:12]
    )
    if response_id != transaction or not flags & 0x8000:
        raise OSError("mismatched DNS response")
    if flags & 0x0200:
        raise OSError("truncated UDP DNS response")
    if flags & 0x000F:
        raise OSError(f"DNS server returned rcode {flags & 0x000F}")
    if questions != 1:
        raise OSError("DNS response has the wrong question count")
    offset = 12
    question_name, offset = _read_dns_name(response, offset)
    if offset + 4 > len(response):
        raise OSError("truncated DNS question")
    question_type, question_class = struct.unpack("!HH", response[offset:offset + 4])
    offset += 4
    expected_name = host.rstrip(".").encode("idna").decode("ascii").lower()
    if question_name != expected_name or (question_type, question_class) != (1, 1):
        raise OSError("DNS response question does not match the request")
    addresses: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    for _ in range(answers + authorities + additional):
        owner, offset = _read_dns_name(response, offset)
        if offset + 10 > len(response):
            raise OSError("truncated DNS record")
        record_type, record_class, _ttl, size = struct.unpack("!HHIH", response[offset:offset + 10])
        offset += 10
        end = offset + size
        if end > len(response):
            raise OSError("truncated DNS record data")
        if record_class == 1 and record_type == 1 and size == 4:
            addresses.setdefault(owner, []).append(socket.inet_ntoa(response[offset:end]))
        elif record_class == 1 and record_type == 5:
            aliases[owner], _ = _read_dns_name(response, offset)
        offset = end
    return addresses, aliases


def resolve_ipv4_bound(host: str, interface: str) -> tuple[str, ...]:
    try:
        return (str(ipaddress.IPv4Address(host)),)
    except ipaddress.AddressValueError:
        pass
    normalized_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    errors = []
    for server in _dns_servers():
        try:
            current = normalized_host
            visited = set()
            for _ in range(16):
                if current in visited:
                    raise OSError(f"DNS alias cycle while resolving {host}")
                visited.add(current)
                addresses, aliases = _query_ipv4(current, server, interface)
                if current in addresses:
                    return tuple(addresses[current])
                if current not in aliases:
                    break
                current = aliases[current]
            errors.append(f"{server}: response contains no IPv4 address for {host}")
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{server}: {error}")
    raise OSError("Wi-Fi-bound DNS resolution failed: " + "; ".join(errors))


def connect_bound(host: str, port: int, interface: str) -> socket.socket:
    last_error: OSError | None = None
    for address in resolve_ipv4_bound(host, interface):
        outbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            outbound.setsockopt(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0"
            )
            outbound.settimeout(30)
            outbound.connect((address, port))
            outbound.settimeout(None)
            return outbound
        except OSError as error:
            last_error = error
            outbound.close()
    raise last_error or OSError(f"could not resolve {host}")


def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = (left, right)
    while True:
        readable, _, _ = select.select(sockets, (), (), 60)
        if not readable:
            continue
        for source in readable:
            data = source.recv(BUFFER_BYTES)
            if not data:
                return
            destination = right if source is left else left
            destination.sendall(data)


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            header, remainder = receive_headers(self.request)
            method, host, port, forwarded_header = parse_destination(header)
            with connect_bound(host, port, self.server.outbound_interface) as outbound:
                if method == "CONNECT":
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    outbound.sendall(forwarded_header)
                if remainder:
                    outbound.sendall(remainder)
                relay(self.request, outbound)
        except (ConnectionError, OSError, UnicodeError, ValueError) as error:
            try:
                self.request.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            print(f"proxy request failed: {error}", file=sys.stderr)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], interface: str):
        self.outbound_interface = interface
        super().__init__(address, ProxyHandler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    interface = Path("/sys/class/net") / args.interface
    if not interface.is_dir():
        raise SystemExit(f"network interface does not exist: {args.interface}")
    if not (interface / "wireless").is_dir():
        raise SystemExit(f"network interface is not wireless: {args.interface}")

    with ProxyServer(("127.0.0.1", args.port), args.interface) as server:
        port = server.server_address[1]
        args.ready_file.write_text(f"{port}\n", encoding="ascii")
        os.chmod(args.ready_file, 0o600)
        server.serve_forever()


if __name__ == "__main__":
    main()
