#!/usr/bin/env python3
"""Minimal build-only HTTP CONNECT proxy bound to one outbound interface."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import socket
import socketserver
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


def connect_bound(host: str, port: int, interface: str) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, address in socket.getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
    ):
        outbound = socket.socket(family, socktype, proto)
        try:
            outbound.setsockopt(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0"
            )
            outbound.settimeout(30)
            outbound.connect(address)
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

    if not (Path("/sys/class/net") / args.interface).is_dir():
        raise SystemExit(f"network interface does not exist: {args.interface}")

    with ProxyServer(("127.0.0.1", args.port), args.interface) as server:
        port = server.server_address[1]
        args.ready_file.write_text(f"{port}\n", encoding="ascii")
        os.chmod(args.ready_file, 0o600)
        server.serve_forever()


if __name__ == "__main__":
    main()
