"""Prove the committed provider image context is the provider runtime alone."""

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest

from repository_checks.nmrpeak_image_inputs import ImageInputRejected, materialize_image_context


REPOSITORY_ROOT = Path(__file__).parents[2]


class NmrpeakProviderImageTests(unittest.TestCase):
    def test_committed_context_contains_exactly_the_declared_provider_inputs(self) -> None:
        revision = subprocess.run(
            ("git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD"),
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary)
            identity = materialize_image_context(
                REPOSITORY_ROOT,
                revision,
                context,
                "provider",
            )
            files = {
                path.relative_to(context).as_posix()
                for path in context.rglob("*")
                if path.is_file()
            }

        declared = set(
            (REPOSITORY_ROOT / "containers/provider/image-inputs.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(files, declared)
        self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")

    def test_stream_allowlist_describes_the_same_captured_context(self) -> None:
        revision = subprocess.run(
            ("git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD"),
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            allowlist = root / "source.json"
            identity = materialize_image_context(
                REPOSITORY_ROOT, revision, context, "provider",
                source_allowlist=allowlist, source_prefix=PurePosixPath("/source"),
            )
            entries = json.loads(allowlist.read_text())
            self.assertEqual(
                {entry["destination"] for entry in entries},
                {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()},
            )
            captured = sha256()
            for entry in entries:
                path = context / entry["destination"]
                content = path.read_bytes()
                self.assertEqual(entry["path"], f"/source/{entry['destination']}")
                self.assertEqual(entry["sha256"], sha256(content).hexdigest())
                self.assertEqual(entry["executable"], bool(path.stat().st_mode & 0o100))
                mode = "100755" if entry["executable"] else "100644"
                captured.update(f"{mode} {entry['sha256']} {len(content)} {entry['destination']}\n".encode())
            self.assertEqual(identity, "sha256:" + captured.hexdigest())

    def test_stream_allowlist_cannot_be_part_of_the_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary)
            with self.assertRaisesRegex(ImageInputRejected, "outside"):
                materialize_image_context(
                    REPOSITORY_ROOT, "0" * 40, context, "provider",
                    source_allowlist=context / "source.json", source_prefix=PurePosixPath("/source"),
                )

    def test_stream_allowlist_does_not_replace_an_existing_file(self) -> None:
        revision = subprocess.run(
            ("git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD"),
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            allowlist = root / "source.json"
            allowlist.write_text("preserve")
            with self.assertRaises(FileExistsError):
                materialize_image_context(
                    REPOSITORY_ROOT, revision, context, "provider",
                    source_allowlist=allowlist, source_prefix=PurePosixPath("/source"),
                )
            self.assertEqual(allowlist.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
