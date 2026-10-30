"""Separate provider API inputs from host-owned shared socket settings."""

from dataclasses import replace
from pathlib import Path
import unittest

from nmrpeak_provider import provider_config

TEMPLATE = b'''schema_id = "nmrpeak.provider.runtime_config.v3"
[api]
origin = "https://api.example.test"
profile = "run"
topology = "web"
provider_ref = "provider:nmrpeak"
credential_file = "/run/credentials/first.json"
exchange_seconds = 5
retirement_seconds = 5
[shared]
project = "fixture"
gid = 1234
[process]
feed_interval_seconds = 5
hello_interval_seconds = 3600
shutdown_drain_seconds = 330
forced_join_seconds = 10
inventory_maximum_pages = 20
observation_interval_seconds = 1
observation_retry_initial_seconds = 5
observation_retry_max_seconds = 30
[runner]
connect_seconds = 30
ready_seconds = 600
validate_seconds = 30
generate_seconds = 600
retire_seconds = 20
[interpreter]
request_timeout_seconds = 25
turn_timeout_seconds = 60
interpretation_timeout_seconds = 240
'''
FROZEN_ID = "sha256:" + "1" * 64
POLICY = provider_config.decode_provider_template(TEMPLATE)
RUNTIME = provider_config.render_provider_runtime_config(
    POLICY, frozen_generation_id=FROZEN_ID, api_proxy_socket="/routes/selected/proxy.sock",
    outbox_socket="/outbox/http.sock",
)


class RuntimeConfigTests(unittest.TestCase):
    def test_operator_example_and_generated_runtime(self):
        raw = (Path(__file__).parents[2] / "config/provider.toml.example").read_bytes()
        self.assertEqual(provider_config.decode_provider_template(raw).shared_project, "replace-me")
        self.assertEqual(provider_config.decode_provider_template(raw).shared_gid, 1000)
        configured = provider_config.decode_provider_runtime_config(RUNTIME)
        self.assertEqual(configured.policy, replace(
            POLICY, shared_project=None, shared_gid=None, api=provider_config.captured_api_fields(POLICY.api)))
        self.assertEqual(configured.frozen_generation_id, FROZEN_ID)
        self.assertEqual(configured.api_proxy_socket, "/routes/selected/proxy.sock")
        self.assertEqual(configured.outbox_socket, "/outbox/http.sock")
        self.assertNotIn(b"[shared]", RUNTIME)
        self.assertNotIn(b"/run/credentials/first.json", RUNTIME)
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(TEMPLATE)
        with self.assertRaises(ValueError):
            provider_config.decode_provider_template(RUNTIME)

    def test_identity_covers_settings_and_captured_credential(self):
        files = {"provider.toml": RUNTIME, "grants/key": b"first key"}
        identity = provider_config.runtime_config_id(files)
        changed = provider_config.render_provider_runtime_config(
            POLICY, frozen_generation_id=FROZEN_ID, api_proxy_socket="/other/proxy.sock",
            outbox_socket="/outbox/http.sock",
        )
        self.assertNotEqual(identity, provider_config.runtime_config_id(files | {"provider.toml": changed}))
        self.assertNotEqual(identity, provider_config.runtime_config_id(files | {"grants/key": b"rotated key"}))

    def test_api_and_socket_paths_are_absolute(self):
        raw = TEMPLATE.replace(b"/run/credentials/first.json", b"relative.json")
        with self.assertRaises(ValueError):
            provider_config.decode_provider_template(raw)
        for raw in (RUNTIME.replace(b"/routes/selected/proxy.sock", b"relative.sock"),
                    RUNTIME.replace(b"/outbox/http.sock", b"relative.sock")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                provider_config.decode_provider_runtime_config(raw)

    def test_shared_grant_identity_is_admitted(self):
        for raw in (TEMPLATE.replace(b'project = "fixture"', b'project = "Invalid Project"'),
                    TEMPLATE.replace(b"gid = 1234", b"gid = 0")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                provider_config.decode_provider_template(raw)

    def test_no_static_binding_compatibility_and_bounded_observation(self):
        with self.assertRaises(ValueError):
            provider_config.decode_provider_template(TEMPLATE.replace(b"runtime_config.v3", b"runtime_config.v2"))
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(
                RUNTIME.replace(b"observation_retry_initial_seconds = 5", b"observation_retry_initial_seconds = 45"),
            )

    def test_input_targets_are_stable_and_separate(self):
        first = provider_config.api_input_target("/host/private/credential.json")
        second = provider_config.api_input_target("/host/private/ca.pem")
        self.assertEqual(first.parent, provider_config.API_INPUT_ROOT)
        self.assertNotEqual(first, second)
        self.assertEqual(first, provider_config.api_input_target("/host/private/credential.json"))
