"""Separate operator policy from the frozen grant needed for API routing."""

import unittest
from pathlib import Path

from nmrpeak_provider import provider_config
from dataclasses import replace


TEMPLATE = b'''schema_id = "nmrpeak.provider.runtime_config.v2"
[binding]
definition_file = "/host/shared/delivery-bindings.toml"
name = "nmrpeak"
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
ROUTE = b'\n[api_route]\nproxy_socket = "/routes/selected/proxy.sock"\n'
FROZEN_ID = "sha256:" + "1" * 64
GENERATED_ID = f'frozen_generation_id = "{FROZEN_ID}"\n'.encode()
RUNTIME = GENERATED_ID + TEMPLATE.replace(b"/host/shared/delivery-bindings.toml",
                                         b"/run/config/nmrpeak-provider/bindings.toml") + ROUTE


class RuntimeConfigV2Tests(unittest.TestCase):
    def test_input_grants_have_fixed_leaves_inside_the_captured_tree(self):
        targets = [provider_config.api_input_target(path) for path in (
            "/host/private/credential.json", "/usr/local/lib/python3.12/site-packages/input.pem",
        )]
        self.assertTrue(all(target.parent == provider_config.API_INPUT_ROOT for target in targets))
        self.assertNotEqual(*targets)
        self.assertEqual(targets[0], provider_config.api_input_target("/host/private/credential.json"))

    def test_committed_operator_example_is_admitted_without_runtime_route(self):
        raw = (Path(__file__).parents[2] / "config/provider.toml.example").read_bytes()
        policy = provider_config.decode_provider_template(raw)
        self.assertEqual(policy.binding_name, "nmrpeak")
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(raw)

    def test_runtime_requires_the_generated_socket_and_keeps_selected_binding(self):
        configured = provider_config.decode_provider_runtime_config(RUNTIME)
        self.assertEqual(configured.frozen_generation_id, FROZEN_ID)
        self.assertEqual(configured.api_proxy_socket, "/routes/selected/proxy.sock")
        self.assertEqual(configured.policy.binding_name, "nmrpeak")
        self.assertEqual(str(configured.policy.binding_definition),
                         "/run/config/nmrpeak-provider/bindings.toml")
        self.assertEqual(configured.policy.process.observation.retry_initial_seconds, 5)
        self.assertEqual(configured.policy.process.observation.retry_max_seconds, 30)
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(TEMPLATE)
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(TEMPLATE + ROUTE)
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(GENERATED_ID + TEMPLATE)

    def test_operator_template_cannot_supply_a_route(self):
        policy = provider_config.decode_provider_template(TEMPLATE)
        self.assertEqual(policy.binding_name, "nmrpeak")
        with self.assertRaises(ValueError):
            provider_config.decode_provider_template(TEMPLATE + ROUTE)
        with self.assertRaises(ValueError):
            provider_config.decode_provider_template(GENERATED_ID + TEMPLATE)

    def test_generated_runtime_roundtrips_policy_and_owns_the_frozen_binding_path(self):
        policy = provider_config.decode_provider_template(TEMPLATE)
        raw = provider_config.render_provider_runtime_config(
            policy, frozen_generation_id=FROZEN_ID, api_proxy_socket="/routes/selected/proxy.sock",
        )
        configured = provider_config.decode_provider_runtime_config(raw)
        self.assertEqual(configured.policy, replace(policy, binding_definition=provider_config.BINDING_PATH))
        self.assertEqual(configured.frozen_generation_id, FROZEN_ID)
        self.assertEqual(configured.api_proxy_socket, "/routes/selected/proxy.sock")
        self.assertEqual(raw, provider_config.render_provider_runtime_config(
            policy, frozen_generation_id=FROZEN_ID, api_proxy_socket="/routes/selected/proxy.sock",
        ))

    def test_runtime_identity_covers_binding_bytes_and_both_generated_facts(self):
        policy = provider_config.decode_provider_template(TEMPLATE)
        render = provider_config.render_provider_runtime_config
        raw = render(policy, frozen_generation_id=FROZEN_ID, api_proxy_socket="/routes/selected/proxy.sock")
        files = {"provider.toml": raw, "bindings.toml": b"binding bytes", "grants/key": b"first key"}
        identity = provider_config.runtime_config_id(files)
        changed_id = render(policy, frozen_generation_id="sha256:" + "2" * 64,
                            api_proxy_socket="/routes/selected/proxy.sock")
        changed_route = render(policy, frozen_generation_id=FROZEN_ID, api_proxy_socket="/routes/other/proxy.sock")
        for config, definition in ((changed_id, b"binding bytes"), (changed_route, b"binding bytes"),
                                   (raw, b"different binding bytes")):
            self.assertNotEqual(identity, provider_config.runtime_config_id(
                files | {"provider.toml": config, "bindings.toml": definition},
            ))
        self.assertNotEqual(identity, provider_config.runtime_config_id(files | {"grants/key": b"rotated key"}))
        renamed = {key: value for key, value in files.items() if key != "grants/key"}
        self.assertNotEqual(identity, provider_config.runtime_config_id(renamed | {"grants/other": b"first key"}))

    def test_runtime_refuses_empty_relative_and_nonstring_socket_grants(self):
        for value in ('""', '"relative.sock"', '17'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                provider_config.decode_provider_runtime_config(
                    GENERATED_ID + TEMPLATE + b'\n[api_route]\nproxy_socket = ' + value.encode() + b'\n',
                )

    def test_runtime_uses_only_the_captured_binding_mount(self):
        with self.assertRaisesRegex(ValueError, "captured binding"):
            provider_config.decode_provider_runtime_config(RUNTIME.replace(
                b"/run/config/nmrpeak-provider/bindings.toml", b"/tmp/edited-bindings.toml",
            ))

    def test_runtime_has_no_v1_startup_compatibility(self):
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(
                RUNTIME.replace(b'runtime_config.v2', b'runtime_config.v1'),
            )

    def test_observation_retry_bounds_are_admitted_as_one_policy(self):
        with self.assertRaises(ValueError):
            provider_config.decode_provider_runtime_config(
                RUNTIME.replace(b'observation_retry_initial_seconds = 5',
                                b'observation_retry_initial_seconds = 45'),
            )
