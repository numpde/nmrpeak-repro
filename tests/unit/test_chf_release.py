"""Prove CHF release identity without loading or extracting a checkpoint."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import warnings
import zipfile

from nmrpeak_provider.canonical_json import canonical_json_bytes, parse_canonical_json_bytes
from repository_checks.chf_release import (
    ARCHIVE_MEMBER,
    ChfReleaseRejected,
    candidate_release_bytes,
    parse_release_bytes,
    verify_release_bytes,
)


_CHECKPOINT = b"checkpoint fixture bytes"
_SOURCE_REVISION = "1" * 40


class ChfReleaseTests(unittest.TestCase):
    def test_valid_member_renders_and_verifies_one_canonical_declaration(self) -> None:
        with ReleaseArchive() as archive:
            raw = candidate_release_bytes(
                archive,
                "chf-test-v1",
                source_revision=_SOURCE_REVISION,
            )
            release = verify_release_bytes(
                raw,
                archive,
                expected_release_name="chf-test-v1",
                expected_source_revision=_SOURCE_REVISION,
            )
        self.assertEqual(release.release_name, "chf-test-v1")
        self.assertEqual(release.checkpoint_bytes, len(_CHECKPOINT))
        self.assertTrue(release.checkpoint_sha256.startswith("sha256:"))
        self.assertEqual(
            parse_release_bytes(
                raw,
                expected_release_name="chf-test-v1",
                expected_source_revision=_SOURCE_REVISION,
            ),
            release,
        )

    def test_malformed_missing_and_duplicate_archives_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "malformed.zip"
            malformed.write_bytes(b"PK\x03\x04incomplete")
            missing = root / "missing.zip"
            with zipfile.ZipFile(missing, "w") as bundle:
                bundle.writestr("weights/other.pt", _CHECKPOINT)
            duplicate = root / "duplicate.zip"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "w") as bundle:
                    bundle.writestr(ARCHIVE_MEMBER, _CHECKPOINT)
                    bundle.writestr(ARCHIVE_MEMBER, _CHECKPOINT)
            for archive in (malformed, missing, duplicate):
                with self.subTest(archive=archive.name):
                    with self.assertRaises(ChfReleaseRejected):
                        candidate_release_bytes(
                            archive,
                            "chf-test-v1",
                            source_revision=_SOURCE_REVISION,
                        )

    def test_every_archive_name_is_inventoried_before_selected_bytes(self) -> None:
        invalid_names = (
            "../outside",
            "/absolute",
            "weights//ambiguous.pt",
            "weights/./ambiguous.pt",
            "weights\\ambiguous.pt",
            "other/unrelated.pt",
        )
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=invalid_name), ReleaseArchive(
                extra_name=invalid_name
            ) as archive:
                with self.assertRaises(ChfReleaseRejected):
                    candidate_release_bytes(
                        archive,
                        "chf-test-v1",
                        source_revision=_SOURCE_REVISION,
                    )

    def test_selected_symlink_and_declaration_drift_are_rejected(self) -> None:
        with ReleaseArchive(selected_mode=stat_mode_symlink()) as archive:
            with self.assertRaises(ChfReleaseRejected):
                candidate_release_bytes(
                    archive,
                    "chf-test-v1",
                    source_revision=_SOURCE_REVISION,
                )
        with ReleaseArchive() as archive:
            raw = candidate_release_bytes(
                archive,
                "chf-test-v1",
                source_revision=_SOURCE_REVISION,
            )
            document = parse_canonical_json_bytes(raw)
            for field, value in (
                ("byte_length", len(_CHECKPOINT) + 1),
                ("sha256", "sha256:" + "0" * 64),
                ("archive_member", "weights/other.pt"),
            ):
                with self.subTest(field=field):
                    changed = dict(document)
                    changed["checkpoint"] = dict(document["checkpoint"]) | {field: value}
                    with self.assertRaises(ChfReleaseRejected):
                        verify_release_bytes(
                            canonical_json_bytes(changed),
                            archive,
                            expected_release_name="chf-test-v1",
                            expected_source_revision=_SOURCE_REVISION,
                        )

    def test_selected_member_corruption_and_unsupported_flags_are_rejected(self) -> None:
        for mutation in ("crc", "encrypted", "compression", "length"):
            with self.subTest(mutation=mutation), ReleaseArchive() as archive:
                mutate_zip(archive, mutation)
                with self.assertRaises(ChfReleaseRejected):
                    candidate_release_bytes(
                        archive,
                        "chf-test-v1",
                        source_revision=_SOURCE_REVISION,
                    )

    def test_archive_path_and_declaration_shape_fail_closed(self) -> None:
        with ReleaseArchive() as archive:
            raw = candidate_release_bytes(
                archive,
                "chf-test-v1",
                source_revision=_SOURCE_REVISION,
            )
            with self.assertRaises(ChfReleaseRejected):
                candidate_release_bytes(
                    Path(archive.name),
                    "chf-test-v1",
                    source_revision=_SOURCE_REVISION,
                )
            alias = archive.parent / "alias.zip"
            alias.symlink_to(archive)
            with self.assertRaises(ChfReleaseRejected):
                candidate_release_bytes(
                    alias,
                    "chf-test-v1",
                    source_revision=_SOURCE_REVISION,
                )
            document = parse_canonical_json_bytes(raw)
            changed = dict(document) | {"extra": True}
            with self.assertRaises(ChfReleaseRejected):
                parse_release_bytes(
                    canonical_json_bytes(changed),
                    expected_release_name="chf-test-v1",
                    expected_source_revision=_SOURCE_REVISION,
                )


class ReleaseArchive:
    def __init__(
        self,
        *,
        extra_name: str | None = None,
        selected_mode: int | None = None,
    ) -> None:
        self.extra_name = extra_name
        self.selected_mode = selected_mode
        self.temporary: TemporaryDirectory | None = None

    def __enter__(self) -> Path:
        self.temporary = TemporaryDirectory()
        archive = Path(self.temporary.name).resolve() / "weights.zip"
        selected = zipfile.ZipInfo(ARCHIVE_MEMBER)
        if self.selected_mode is not None:
            selected.create_system = 3
            selected.external_attr = self.selected_mode << 16
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(selected, _CHECKPOINT)
            if self.extra_name is not None:
                bundle.writestr(self.extra_name, b"unrelated")
        return archive

    def __exit__(self, *_error: object) -> None:
        assert self.temporary is not None
        self.temporary.cleanup()


def stat_mode_symlink() -> int:
    return 0o120777


def mutate_zip(archive: Path, mutation: str) -> None:
    raw = bytearray(archive.read_bytes())
    local = raw.index(b"PK\x03\x04")
    central = raw.index(b"PK\x01\x02")
    if mutation == "crc":
        payload = raw.index(_CHECKPOINT)
        raw[payload] ^= 1
    elif mutation == "encrypted":
        _set_u16(raw, local + 6, _u16(raw, local + 6) | 1)
        _set_u16(raw, central + 8, _u16(raw, central + 8) | 1)
    elif mutation == "compression":
        _set_u16(raw, local + 8, 99)
        _set_u16(raw, central + 10, 99)
    elif mutation == "length":
        _set_u32(raw, central + 24, _u32(raw, central + 24) + 1)
    else:
        raise AssertionError("unsupported ZIP test mutation")
    archive.write_bytes(raw)


def _u16(raw: bytearray, offset: int) -> int:
    return int.from_bytes(raw[offset : offset + 2], "little")


def _set_u16(raw: bytearray, offset: int, value: int) -> None:
    raw[offset : offset + 2] = value.to_bytes(2, "little")


def _u32(raw: bytearray, offset: int) -> int:
    return int.from_bytes(raw[offset : offset + 4], "little")


def _set_u32(raw: bytearray, offset: int, value: int) -> None:
    raw[offset : offset + 4] = value.to_bytes(4, "little")


if __name__ == "__main__":
    unittest.main()
