"""Admit only thin endpoint settings and sibling credential references."""

import unittest

from nmrpeak_provider.interpreter_settings import parse_endpoint_settings


class InterpreterSettingsTests(unittest.TestCase):
    def test_malformed_configuration_does_not_retain_raw_decoder_input(self):
        with self.assertRaises(ValueError) as caught:
            parse_endpoint_settings(b"private-marker\xff")
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("private-marker", str(caught.exception))

    def test_thin_endpoint_allows_optional_ca(self):
        settings = parse_endpoint_settings(
            b'url = "https://model.example/v1/chat/completions"\nmodel = "model"\n'
            b'key_file = "model.key"\nca_file = "model-ca.pem"\n'
        )
        self.assertEqual(settings["key_file"], "model.key")
        self.assertEqual(settings["ca_file"], "model-ca.pem")

    def test_legacy_fields_and_escaping_file_references_are_rejected(self):
        for raw in (
            b'id="old"\nbase_url="https://example.test"\napi_key="key"\nmodel="m"\n',
            b'url="https://example.test"\nmodel="m"\nkey_file="../secret"\n',
            b'url="https://example.test"\nmodel="m"\nkey_file="/tmp/secret"\n',
            b'url="https://example.test"\nmodel="m"\nkey_file="other.toml"\n',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_endpoint_settings(raw)


if __name__ == "__main__":
    unittest.main()
