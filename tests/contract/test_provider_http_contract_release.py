"""Prove the committed NMR API provider release is exact and usable offline."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile
import unittest

from nmrpeak_provider.provider_http_contract import (
    PROVIDER_HTTP_CONTRACT_ID,
    PROVIDER_HTTP_RELEASE_REF,
    PROVIDER_HTTP_RELEASE_SOURCE_REVISION,
    load_provider_http_contract_release,
)


RELEASE_ROOT = Path(__file__).parents[2] / "contracts/upstream/nmr_api_v1"


@contextmanager
def copied_release() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary) / "release"
        shutil.copytree(RELEASE_ROOT, destination)
        yield destination


class ProviderHttpContractReleaseTests(unittest.TestCase):
    def test_committed_release_authenticates_all_consumed_contract_facts(self) -> None:
        release = load_provider_http_contract_release(RELEASE_ROOT)

        self.assertEqual(PROVIDER_HTTP_RELEASE_REF, release.release_ref)
        self.assertEqual(
            PROVIDER_HTTP_RELEASE_SOURCE_REVISION,
            release.source_revision,
        )
        self.assertEqual(
            PROVIDER_HTTP_CONTRACT_ID,
            release.openapi["x-nmr-contract-id"],
        )

    def test_artifact_byte_drift_is_rejected(self) -> None:
        with copied_release() as release_root:
            schema = release_root / "schemas/hello_response.v1.schema.json"
            schema.write_bytes(schema.read_bytes() + b" ")

            with self.assertRaisesRegex(ValueError, "length drift"):
                load_provider_http_contract_release(release_root)

    def test_same_length_artifact_drift_is_rejected(self) -> None:
        with copied_release() as release_root:
            schema = release_root / "schemas/hello_response.v1.schema.json"
            raw = schema.read_bytes()
            schema.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])

            with self.assertRaisesRegex(ValueError, "content drift"):
                load_provider_http_contract_release(release_root)

    def test_uninventoried_file_is_rejected(self) -> None:
        with copied_release() as release_root:
            (release_root / "unexpected.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "file inventory"):
                load_provider_http_contract_release(release_root)

    def test_manifest_provenance_drift_is_rejected(self) -> None:
        with copied_release() as release_root:
            manifest_path = release_root / "manifest.json"
            manifest_path.write_bytes(
                manifest_path.read_bytes().replace(
                    PROVIDER_HTTP_RELEASE_SOURCE_REVISION.encode("ascii"),
                    ("0" * 40).encode("ascii"),
                )
            )

            with self.assertRaisesRegex(ValueError, "provenance"):
                load_provider_http_contract_release(release_root)

    def test_symlinked_artifact_is_rejected(self) -> None:
        with copied_release() as release_root:
            schema = release_root / "schemas/hello_response.v1.schema.json"
            target = release_root / "schemas/hello_response.target.json"
            schema.rename(target)
            schema.symlink_to(target.name)

            with self.assertRaisesRegex(ValueError, "symlink"):
                load_provider_http_contract_release(release_root)

if __name__ == "__main__":
    unittest.main()
