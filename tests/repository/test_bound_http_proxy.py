"""Prove the build proxy keeps resolver failures transparent and recoverable."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


PROXY_PATH = Path(__file__).parents[2] / "docker/bound_http_proxy.py"
SPEC = importlib.util.spec_from_file_location("nmrpeak_bound_http_proxy", PROXY_PATH)
assert SPEC is not None and SPEC.loader is not None
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


class BoundHttpProxyTests(unittest.TestCase):
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
