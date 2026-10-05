"""Prove the build proxy keeps resolver failures transparent and recoverable."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import struct
import unittest
from unittest import mock


PROXY_PATH = Path(__file__).parents[2] / "docker/bound_http_proxy.py"
SPEC = importlib.util.spec_from_file_location("nmrpeak_bound_http_proxy", PROXY_PATH)
assert SPEC is not None and SPEC.loader is not None
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


class BoundHttpProxyTests(unittest.TestCase):
    def test_udp_dns_socket_is_bound_and_truncation_fails_closed(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.options = []

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def setsockopt(self, *option: object) -> None:
                self.options.append(option)

            def settimeout(self, _seconds: int) -> None:
                pass

            def connect(self, _address: object) -> None:
                pass

            def send(self, _request: bytes) -> None:
                pass

            def recv(self, _maximum: int) -> bytes:
                return struct.pack("!HHHHHH", 0x1234, 0x8200, 1, 0, 0, 0)

        fake = FakeSocket()
        with (
            mock.patch.object(PROXY.secrets, "randbits", return_value=0x1234),
            mock.patch.object(PROXY.socket, "socket", return_value=fake),
            self.assertRaisesRegex(OSError, "truncated UDP DNS response"),
        ):
            PROXY._query_ipv4("packages.example", "192.0.2.53", "wlan-test")
        self.assertIn(
            (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"wlan-test\0"),
            fake.options,
        )

    def test_outbound_tcp_socket_is_bound_before_connect(self) -> None:
        events = []

        class FakeSocket:
            def setsockopt(self, *option: object) -> None:
                events.append(("setsockopt", option))

            def settimeout(self, seconds: object) -> None:
                events.append(("settimeout", seconds))

            def connect(self, address: object) -> None:
                events.append(("connect", address))

            def close(self) -> None:
                events.append(("close",))

        fake = FakeSocket()
        with (
            mock.patch.object(PROXY, "resolve_ipv4_bound", return_value=("192.0.2.1",)),
            mock.patch.object(PROXY.socket, "socket", return_value=fake),
        ):
            connected = PROXY.connect_bound("packages.example", 443, "wlan-test")
        self.assertIs(connected, fake)
        self.assertEqual(
            events[:3],
            [
                ("setsockopt", (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"wlan-test\0")),
                ("settimeout", 30),
                ("connect", ("192.0.2.1", 443)),
            ],
        )
        self.assertEqual(events[-1], ("settimeout", None))

    def test_unicode_hostname_is_resolved_as_one_ascii_dns_name(self) -> None:
        calls = []

        def query(host: str, server: str, interface: str):
            calls.append((host, server, interface))
            return ({"xn--bcher-kva.example": ["192.0.2.2"]}, {})

        with (
            mock.patch.object(PROXY, "_dns_servers", return_value=("resolver",)),
            mock.patch.object(PROXY, "_query_ipv4", side_effect=query),
        ):
            addresses = PROXY.resolve_ipv4_bound("bücher.example", "wlan-test")

        self.assertEqual(addresses, ("192.0.2.2",))
        self.assertEqual(
            calls,
            [("xn--bcher-kva.example", "resolver", "wlan-test")],
        )

    def test_malformed_first_dns_response_falls_back_to_next_bound_server(self) -> None:
        calls = []

        def query(host: str, server: str, interface: str):
            calls.append((host, server, interface))
            if server == "malformed":
                raise ValueError("truncated DNS name")
            return ({host: ["192.0.2.1"]}, {})

        with (
            mock.patch.object(PROXY, "_dns_servers", return_value=("malformed", "working")),
            mock.patch.object(PROXY, "_query_ipv4", side_effect=query),
        ):
            addresses = PROXY.resolve_ipv4_bound("packages.example", "wlan-test")

        self.assertEqual(addresses, ("192.0.2.1",))
        self.assertEqual(
            calls,
            [
                ("packages.example", "malformed", "wlan-test"),
                ("packages.example", "working", "wlan-test"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
