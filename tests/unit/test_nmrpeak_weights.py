from __future__ import annotations

from hashlib import md5
import json
from pathlib import Path
import tempfile
import threading
import unittest

from repository_checks.nmrpeak_weights import (
    ARCHIVE_PATH,
    DECLARATION_PATH,
    PARTIAL_PATH,
    WeightsAcquisitionRejected,
    check_weights,
    download_weights,
)


class NmrpeakWeightsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.payload = b"declared public weights bytes"
        declaration = {
            "schema_id": "nmrpeak-weights-acquisition-v1",
            "doi": "10.5281/zenodo.19122815",
            "url": "https://zenodo.org/records/19122815/files/weights.zip?download=1",
            "file_name": "weights.zip",
            "byte_length": len(self.payload),
            "md5": md5(self.payload, usedforsecurity=False).hexdigest(),
        }
        path = self.root / DECLARATION_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(declaration), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_download_publishes_only_the_authenticated_partial(self) -> None:
        observed: list[str] = []

        def fake_curl(arguments: list[str]) -> None:
            observed.extend(arguments)
            output = Path(arguments[arguments.index("--output") + 1])
            output.write_bytes(self.payload)

        download_weights(self.root, "wlp1s0", run_curl=fake_curl)

        self.assertEqual((self.root / ARCHIVE_PATH).read_bytes(), self.payload)
        self.assertFalse((self.root / PARTIAL_PATH).exists())
        self.assertEqual(observed[-1], self.declared_url)
        self.assertEqual(observed[observed.index("--interface") + 1], "wlp1s0")
        self.assertIn("--continue-at", observed)

    def test_download_uses_normal_routing_when_no_interface_is_selected(self) -> None:
        observed: list[str] = []

        def fake_curl(arguments: list[str]) -> None:
            observed.extend(arguments)
            Path(arguments[arguments.index("--output") + 1]).write_bytes(self.payload)

        download_weights(self.root, None, run_curl=fake_curl)

        self.assertNotIn("--interface", observed)

    def test_existing_authenticated_archive_makes_download_idempotent(self) -> None:
        archive = self.root / ARCHIVE_PATH
        archive.parent.mkdir()
        archive.write_bytes(self.payload)

        def unexpected_curl(_arguments: list[str]) -> None:
            self.fail("curl must not run for an authenticated archive")

        download_weights(self.root, None, run_curl=unexpected_curl)
        check_weights(self.root)

    def test_concurrent_downloaders_share_one_authenticated_publication(self) -> None:
        runner_started = threading.Event()
        second_started = threading.Event()
        release_runner = threading.Event()
        runner_calls: list[list[str]] = []
        failures: list[BaseException] = []

        def controlled_curl(arguments: list[str]) -> None:
            runner_calls.append(arguments)
            runner_started.set()
            if not release_runner.wait(timeout=2):
                raise AssertionError("test did not release the download runner")
            Path(arguments[arguments.index("--output") + 1]).write_bytes(self.payload)

        def download(started: threading.Event | None = None) -> None:
            if started is not None:
                started.set()
            try:
                download_weights(self.root, None, run_curl=controlled_curl)
            except BaseException as error:
                failures.append(error)

        first = threading.Thread(target=download)
        second = threading.Thread(target=download, args=(second_started,))
        first.start()
        self.assertTrue(runner_started.wait(timeout=2))
        second.start()
        self.assertTrue(second_started.wait(timeout=2))
        self.assertTrue(second.is_alive())
        release_runner.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(runner_calls), 1)
        check_weights(self.root)

    def test_failed_authentication_does_not_replace_an_archive(self) -> None:
        partial = self.root / PARTIAL_PATH
        partial.parent.mkdir()
        partial.write_bytes(b"old partial")

        def fake_curl(arguments: list[str]) -> None:
            Path(arguments[arguments.index("--output") + 1]).write_bytes(
                b"wrong bytes of same length!!!"
            )

        with self.assertRaisesRegex(
            WeightsAcquisitionRejected, "MD5 differs|has .* bytes"
        ):
            download_weights(self.root, None, run_curl=fake_curl)

        self.assertFalse((self.root / ARCHIVE_PATH).exists())
        self.assertTrue(partial.exists())

    def test_oversized_partial_is_rejected_before_network_effect(self) -> None:
        partial = self.root / PARTIAL_PATH
        partial.parent.mkdir()
        partial.write_bytes(self.payload + b"x")

        def unexpected_curl(_arguments: list[str]) -> None:
            self.fail("curl must not run for an oversized partial")

        with self.assertRaisesRegex(WeightsAcquisitionRejected, "larger"):
            download_weights(self.root, None, run_curl=unexpected_curl)

    def test_check_rejects_symlink_and_wrong_checksum(self) -> None:
        archive = self.root / ARCHIVE_PATH
        archive.parent.mkdir()
        source = self.root / "source"
        source.write_bytes(self.payload)
        archive.symlink_to(source)
        with self.assertRaisesRegex(WeightsAcquisitionRejected, "non-symlink"):
            check_weights(self.root)

        archive.unlink()
        archive.write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(WeightsAcquisitionRejected, "MD5 differs"):
            check_weights(self.root)

    def test_declaration_url_must_match_its_versioned_doi(self) -> None:
        path = self.root / DECLARATION_PATH
        document = json.loads(path.read_text(encoding="utf-8"))
        document["url"] = (
            "https://zenodo.org/records/19098285/files/weights.zip?download=1"
        )
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaisesRegex(WeightsAcquisitionRejected, "does not name"):
            check_weights(self.root)

    @property
    def declared_url(self) -> str:
        return "https://zenodo.org/records/19122815/files/weights.zip?download=1"


if __name__ == "__main__":
    unittest.main()
