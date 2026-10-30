"""Admit captured ACL inputs through the actual installed SDK parsers."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from provider_core.api_config import ApiConfigurationRejected
from provider_core.diagnostics import failure_evidence
from provider_core.startup_file import StartupFileRejected

from nmrpeak_provider import provider_config, provider_startup
from nmrpeak_provider.local_input import LocalInputSnapshotError, read_bounded_regular_file
from tests.unit.test_provider_startup_inputs import credential_bytes
from tests.unit.test_runtime_config_v2 import RUNTIME


class ProviderStartupTests(unittest.TestCase):
    def test_captured_file_error_names_the_purpose_and_preserves_native_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StartupFileRejected) as caught:
                provider_startup.read_startup_input(Path(directory) / "missing", maximum_bytes=100,
                                                    label="API signing credential", secret=True)
        self.assertIn("API signing credential", str(caught.exception))
        self.assertIsInstance(caught.exception.cause, LocalInputSnapshotError)
        self.assertIsInstance(caught.exception.cause.__cause__, FileNotFoundError)
        evidence = failure_evidence(caught.exception, secrets=())
        self.assertIn("API signing credential", evidence["message"])

    def test_control_interruption_and_independent_failure_groups_keep_their_identity(self):
        for failure in (SystemExit(0), ExceptionGroup("Read and cleanup failed", [OSError("read"), OSError("close")])):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(provider_startup, "read_bounded_regular_file", side_effect=failure):
                    with self.assertRaises(type(failure)) as caught:
                        provider_startup.read_startup_input("/unused", maximum_bytes=100, label="API input")
                self.assertIs(caught.exception, failure)

    def fixture(self, directory, *, credential=None):
        root = Path(directory)
        config = root / "provider.toml"
        original = Path("/run/config/nmrpeak-provider/grants") / provider_config.api_input_target(
            "/run/credentials/first.json").name
        config.write_bytes(RUNTIME.replace(str(original).encode(), str(provider_config.api_input_target(
            "/run/credentials/first.json")).encode()))
        key = Ed25519PrivateKey.generate()
        captured = provider_config.api_input_target("/run/credentials/first.json")
        captured.parent.mkdir()
        captured.write_bytes(credential_bytes(key) if credential is None else credential)
        captured.chmod(0o440)
        return config, captured, key

    def test_captured_credential_is_read_once_and_retained_for_publication(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider_config, "API_INPUT_ROOT", Path(directory) / "grants"):
            config, captured, key = self.fixture(directory)
            observed = []

            def read(path, *, maximum_bytes):
                observed.append(path)
                return read_bounded_regular_file(path, maximum_bytes=maximum_bytes)

            with patch.object(provider_startup, "read_bounded_regular_file", side_effect=read):
                startup = provider_startup.load_provider_startup(config)
            self.assertEqual(observed, [config, captured])
            self.assertEqual(startup.provider_ref, "provider:nmrpeak")
            self.assertEqual(startup.publication.credential_bytes, captured.read_bytes())
            self.assertEqual(startup.settings.expected_topology, "web")
            self.assertEqual(startup.settings.http.proxy_socket, "/routes/selected/proxy.sock")
            self.assertEqual(startup.settings.identity.private_key.public_key().public_bytes_raw(),
                             key.public_key().public_bytes_raw())
            self.assertEqual(captured.stat().st_mode & 0o777, 0o440)

    def test_original_path_is_never_used_when_capture_is_missing(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider_config, "API_INPUT_ROOT", Path(directory) / "grants"):
            path = str(Path(directory) / "original-credential.json")
            Path(path).write_bytes(b"original is available but not a grant")
            with self.assertRaises(StartupFileRejected):
                provider_startup.read_captured_api_input(path, maximum_bytes=1024, label="API credential", secret=True)
            self.assertEqual(Path(path).read_bytes(), b"original is available but not a grant")

    def test_sdk_rejects_credential_principal_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider_config, "API_INPUT_ROOT", Path(directory) / "grants"):
            wrong = credential_bytes(Ed25519PrivateKey.generate()).replace(b'provider:nmrpeak', b'provider:other')
            config, _captured, _key = self.fixture(directory, credential=wrong)
            with self.assertRaises(ApiConfigurationRejected):
                provider_startup.load_provider_startup(config)

    def test_capture_refuses_symlink_instead_of_opening_its_target(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider_config, "API_INPUT_ROOT", Path(directory) / "grants"):
            original = Path(directory) / "original"
            original.write_bytes(b"private")
            target = provider_config.api_input_target(str(original))
            target.parent.mkdir()
            target.symlink_to(original)
            with self.assertRaises(StartupFileRejected):
                provider_startup.read_captured_api_input(str(original), maximum_bytes=1024, label="API credential", secret=True)
