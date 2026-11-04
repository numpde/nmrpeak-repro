"""Prove the build proxy keeps resolver failures transparent and recoverable."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import unittest
from unittest import mock


RESOLVER_FILE = Path("/test/resolv.conf")
PROXY_PATH = Path(__file__).parents[2] / "docker/bound_http_proxy.py"
SPEC = importlib.util.spec_from_file_location("nmrpeak_bound_http_proxy", PROXY_PATH)
assert SPEC is not None and SPEC.loader is not None
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


class BoundHttpProxyTests(unittest.TestCase):
    def test_unconfirmed_build_retains_proxy_resolver_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "resolv.conf").write_text("nameserver 192.0.2.53\n")
            (root / "context").mkdir()
            script = Path(__file__).parents[2] / "scripts/image-operation-lock.sh"
            subprocess.run(("bash", "-c", 'source "$1"; trim_streamed_image_capture "$2"',
                            "test", str(script), str(root)), check=True)
            self.assertTrue((root / "resolv.conf").is_file())
            self.assertFalse((root / "context").exists())

    def test_captured_resolvers_are_used_without_global_dns(self):
        with tempfile.TemporaryDirectory() as directory:
            captured = Path(directory) / "resolv.conf"
            captured.write_text("nameserver 192.0.2.53\nnameserver 192.0.2.53\n")
            self.assertEqual(PROXY._dns_servers(captured), ("192.0.2.53",))
            captured.write_text("nameserver 127.0.0.1\n")
            with self.assertRaises(OSError):
                PROXY._dns_servers(captured)

    def test_dns_timeout_retries_same_bound_server_then_succeeds(self) -> None:
        with (
            mock.patch.object(PROXY, "_dns_servers", return_value=("resolver",)),
            mock.patch.object(PROXY, "_query_ipv4", side_effect=[TimeoutError("timed out"),
                ({"packages.example": ["192.0.2.1"]}, {})]) as query,
        ):
            self.assertEqual(PROXY.resolve_ipv4_bound("packages.example", "wlan-test", RESOLVER_FILE), ("192.0.2.1",))
        self.assertEqual(query.call_args_list, [mock.call("packages.example", "resolver", "wlan-test")] * 2)

    def test_dns_timeout_retry_is_bounded(self) -> None:
        with (
            mock.patch.object(PROXY, "_dns_servers", return_value=("resolver",)),
            mock.patch.object(PROXY, "_query_ipv4", side_effect=TimeoutError("timed out")) as query,
            self.assertRaisesRegex(OSError, "resolver: timed out"),
        ):
            PROXY.resolve_ipv4_bound("packages.example", "wlan-test", RESOLVER_FILE)
        self.assertEqual(query.call_count, 2)

    def test_exhausted_dns_timeout_uses_next_bound_server(self) -> None:
        with (
            mock.patch.object(PROXY, "_dns_servers", return_value=("first", "second")),
            mock.patch.object(PROXY, "_query_ipv4", side_effect=[TimeoutError("timed out"),
                TimeoutError("timed out"), ({"packages.example": ["192.0.2.1"]}, {})]) as query,
        ):
            self.assertEqual(PROXY.resolve_ipv4_bound("packages.example", "wlan-test", RESOLVER_FILE), ("192.0.2.1",))
        self.assertEqual(query.call_args_list, [mock.call("packages.example", "first", "wlan-test")] * 2 +
                         [mock.call("packages.example", "second", "wlan-test")])

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
            connected = PROXY.connect_bound("packages.example", 443, "wlan-test", RESOLVER_FILE)
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
            addresses = PROXY.resolve_ipv4_bound("bücher.example", "wlan-test", RESOLVER_FILE)

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
            addresses = PROXY.resolve_ipv4_bound("packages.example", "wlan-test", RESOLVER_FILE)

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
