"""Check Peak's endpoint capture and SDK factory composition."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from provider_core.api_config import ApiConfigurationRejected

from nmrpeak_provider.openai_chat_interpreter import endpoint_factories, load_openai_chat_endpoint_specs


class EndpointCaptureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def endpoint(self, filename, *, key, url="https://example.test/v1/chat/completions"):
        (self.root / filename).write_text(f'url = "{url}"\nmodel = "fixture"\nkey_file = "{key}"\n')

    def test_filename_order_and_separate_keys_are_preserved(self):
        self.endpoint("20-second.toml", key="second.key")
        self.endpoint("10-first.toml", key="first.key")
        (self.root / "first.key").write_text("first-key\n")
        (self.root / "second.key").write_text("second-key\n")
        specs = load_openai_chat_endpoint_specs(self.root)
        self.assertEqual([s.configuration_id for s in specs], ["10-first", "20-second"])
        self.assertEqual([s.api_key for s in specs], ["first-key", "second-key"])
        factories = endpoint_factories(specs)
        self.assertEqual(len(factories), 2)
        self.assertTrue(all(0 < factory.context_bytes < factory.request_bytes for factory in factories))
        self.assertNotIn("first-key", repr(specs))

    def test_missing_or_symlinked_key_is_rejected(self):
        self.endpoint("first.toml", key="first.key")
        with self.assertRaises(ValueError):
            load_openai_chat_endpoint_specs(self.root)
        (self.root / "real.key").write_text("secret")
        (self.root / "first.key").symlink_to("real.key")
        with self.assertRaises(ValueError):
            load_openai_chat_endpoint_specs(self.root)

    def test_ambient_tls_keylog_is_rejected_without_creating_it(self):
        self.endpoint("first.toml", key="first.key")
        (self.root / "first.key").write_text("secret")
        keylog = self.root / "keylog"
        with patch.dict(os.environ, {"SSLKEYLOGFILE": str(keylog)}):
            with self.assertRaises(ApiConfigurationRejected):
                load_openai_chat_endpoint_specs(self.root)
        self.assertFalse(keylog.exists())

    def test_legacy_inline_credentials_are_rejected(self):
        (self.root / "first.toml").write_text(
            'id = "first"\nbase_url = "https://example.test/v1"\n'
            'model = "fixture"\napi_key = "private-marker"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_openai_chat_endpoint_specs(self.root)
        self.assertNotIn("private-marker", str(caught.exception))
