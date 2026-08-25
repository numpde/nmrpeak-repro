"""Prove runtime TOML carries only mutable transport and bounded timing facts."""

from __future__ import annotations

import unittest

from nmrpeak_provider.provider_config import (
    CA_PATH,
    CHF_SOCKET_PATH,
    CREDENTIAL_PATH,
    FROZEN_ROOT,
    HF_SOCKET_PATH,
    IDENTITY_LOCK_PATH,
    JOURNAL_PATH,
    decode_provider_runtime_config,
)


CONFIG = b'''schema_id = "nmrpeak.provider.runtime_config.v1"
frozen_generation_id = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

[server_a]
origin = "https://api.example.test"
topology = "web"
connect_timeout_seconds = 5
io_deadline_seconds = 30

[journal]
maximum_records = 2
filesystem_reserve_bytes = 1048576

[process]
feed_interval_seconds = 5
hello_interval_seconds = 3600
shutdown_drain_seconds = 330
forced_join_seconds = 10
inventory_maximum_pages = 20
maximum_consecutive_unavailable = 5
observation_interval_seconds = 1
observation_maximum_gap_seconds = 45

[runner]
connect_seconds = 30
ready_seconds = 300
validate_seconds = 30
generate_seconds = 300
retire_seconds = 10
'''


class ProviderConfigTests(unittest.TestCase):
    def test_closed_document_constructs_existing_runtime_values(self) -> None:
        configured = decode_provider_runtime_config(CONFIG)

        self.assertEqual(configured.endpoint.origin, "https://api.example.test")
        self.assertEqual(configured.endpoint.expected_topology, "web")
        self.assertEqual(configured.journal_maximum_records, 2)
        self.assertEqual(configured.process.hello_interval_seconds, 3600)
        self.assertEqual(configured.runner.generate_seconds, 300)

    def test_private_ca_is_selected_without_loading_runtime_trust(self) -> None:
        configured = decode_provider_runtime_config(
            CONFIG.replace(
                b'[server_a]\n',
                b'[server_a]\nuse_private_ca = true\n',
            )
        )

        self.assertEqual(configured.endpoint.ca_file, CA_PATH)

    def test_unknown_missing_and_invalid_values_are_rejected(self) -> None:
        invalid = (
            CONFIG + b"unknown = true\n",
            CONFIG.replace(b"maximum_records = 2\n", b""),
            CONFIG.replace(b"maximum_records = 2", b"maximum_records = 0"),
            CONFIG.replace(b"maximum_records = 2", b"maximum_records = 10001"),
            CONFIG.replace(b'topology = "web"', b'topology = "other"'),
            CONFIG.replace(
                b"feed_interval_seconds = 5",
                b"feed_interval_seconds = 0",
            ),
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises((TypeError, ValueError)):
                decode_provider_runtime_config(raw)

    def test_container_paths_are_fixed_code_owned_mount_contracts(self) -> None:
        self.assertEqual(FROZEN_ROOT.as_posix(), "/run/nmrpeak-provider/frozen")
        self.assertEqual(
            CREDENTIAL_PATH.as_posix(),
            "/run/secrets/nmrpeak-provider/signing.private.json",
        )
        self.assertEqual(
            IDENTITY_LOCK_PATH.as_posix(),
            "/run/nmrpeak-provider-lock/provider.lock",
        )
        self.assertEqual(JOURNAL_PATH.as_posix(), "/var/lib/nmrpeak-provider/journal")
        self.assertEqual(HF_SOCKET_PATH, "/run/nmrpeak-provider/hf/session.sock")
        self.assertEqual(CHF_SOCKET_PATH, "/run/nmrpeak-provider/chf/session.sock")


if __name__ == "__main__":
    unittest.main()
