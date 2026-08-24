"""Render and verify the one checkpoint release shape owned by the CHF runner."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

from nmrpeak_provider.canonical_json import (
    CanonicalJsonError,
    JsonValue,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CONTRACT_ID
from nmrpeak_provider.product import CHF_OFFERING
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    RESULT_SCHEMA_ID,
)
from repository_checks.nmrpeak_source import read_nmrpeak_source_revision


SCHEMA_ID = "nmrpeak.checkpoint_release.chf.v1"
RUNNER_REF = CHF_RESULT_IDENTITY.runner_ref
ARCHIVE_MEMBER = (
    "weights/generation/all_weights/"
    "NMRexp_lr3e-4_bs16_gpu8_spec_trans_mol_bart_base_spec_trans_mol_60000_1000000/"
    "CHF/checkpoint_best.pt"
)
_RELEASE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_COPY_CHUNK_BYTES = 8 * 1024 * 1024


class ChfReleaseRejected(ValueError):
    """The archive or declaration cannot authorize a CHF checkpoint release."""


@dataclass(frozen=True, slots=True)
class ChfCheckpointRelease:
    """Immutable identities required before the CHF checkpoint may be imported."""

    release_name: str
    checkpoint_bytes: int
    checkpoint_sha256: str


def candidate_release_bytes(
    archive: Path,
    release_name: str,
    *,
    source_revision: str,
) -> bytes:
    """Stream the selected member and render one canonical candidate declaration."""

    _validate_release_name(release_name)
    _validate_source_revision(source_revision)
    size, digest = measure_checkpoint_member(archive)
    return canonical_json_bytes(
        _release_document(release_name, size, digest, source_revision)
    )


def verify_release_bytes(
    raw: bytes,
    archive: Path,
    *,
    expected_release_name: str,
    expected_source_revision: str,
) -> ChfCheckpointRelease:
    """Bind one canonical declaration to the selected archive member bytes."""

    declaration = parse_release_bytes(
        raw,
        expected_release_name=expected_release_name,
        expected_source_revision=expected_source_revision,
    )
    size, digest = measure_checkpoint_member(archive)
    if (
        size != declaration.checkpoint_bytes
        or digest != declaration.checkpoint_sha256
    ):
        raise ChfReleaseRejected(
            "CHF checkpoint bytes do not match the release declaration"
        )
    return declaration


def parse_release_bytes(
    raw: bytes,
    *,
    expected_release_name: str,
    expected_source_revision: str,
) -> ChfCheckpointRelease:
    """Admit the exact closed CHF declaration without consulting an archive."""

    _validate_release_name(expected_release_name)
    _validate_source_revision(expected_source_revision)
    try:
        document = parse_canonical_json_bytes(raw)
    except CanonicalJsonError as error:
        raise ChfReleaseRejected("CHF release declaration is not canonical JSON") from error
    if type(document) is not dict or set(document) != {
        "schema_id",
        "release_name",
        "runner_ref",
        "runner_contract_id",
        "analysis_kind_ref",
        "result_schema_id",
        "decode_policy_id",
        "upstream_revision",
        "source_closure_sha256",
        "checkpoint",
    }:
        raise ChfReleaseRejected("CHF release declaration has an invalid shape")
    expected_facts = {
        "schema_id": SCHEMA_ID,
        "release_name": expected_release_name,
        "runner_ref": RUNNER_REF,
        "runner_contract_id": CHF_RUNNER_CONTRACT_ID,
        "analysis_kind_ref": CHF_OFFERING.analysis_kind_ref,
        "result_schema_id": RESULT_SCHEMA_ID,
        "decode_policy_id": CHF_RESULT_IDENTITY.decode_policy.decode_policy_id,
        "upstream_revision": expected_source_revision,
        "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
    }
    if any(document[name] != value for name, value in expected_facts.items()):
        raise ChfReleaseRejected("CHF release declaration identity has drifted")
    checkpoint = document["checkpoint"]
    if type(checkpoint) is not dict or set(checkpoint) != {
        "archive_member",
        "byte_length",
        "sha256",
    }:
        raise ChfReleaseRejected("CHF release checkpoint facts have an invalid shape")
    byte_length = checkpoint["byte_length"]
    digest = checkpoint["sha256"]
    if (
        checkpoint["archive_member"] != ARCHIVE_MEMBER
        or type(byte_length) is not int
        or type(byte_length) is bool
        or byte_length <= 0
        or type(digest) is not str
        or _SHA256_REF.fullmatch(digest) is None
    ):
        raise ChfReleaseRejected("CHF release checkpoint facts are invalid")
    if canonical_json_bytes(document) != raw:
        raise ChfReleaseRejected("CHF release declaration rendering has drifted")
    return ChfCheckpointRelease(expected_release_name, byte_length, digest)


def measure_checkpoint_member(archive: Path) -> tuple[int, str]:
    """Inventory the ZIP and hash only the one selected regular CHF member."""

    _require_archive_path(archive)
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            by_name: dict[str, zipfile.ZipInfo] = {}
            for member in members:
                _validate_archive_member_name(member.filename)
                if member.filename in by_name:
                    raise ChfReleaseRejected(
                        "CHF acquisition archive contains duplicate member names"
                    )
                by_name[member.filename] = member
            selected = by_name.get(ARCHIVE_MEMBER)
            if selected is None:
                raise ChfReleaseRejected(
                    "CHF acquisition archive does not contain the selected checkpoint"
                )
            _require_regular_checkpoint(selected)
            digest = sha256()
            count = 0
            with bundle.open(selected, "r") as source:
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    count += len(chunk)
                    if count > selected.file_size:
                        raise ChfReleaseRejected(
                            "CHF checkpoint produced more bytes than declared"
                        )
                    digest.update(chunk)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ChfReleaseRejected("CHF acquisition archive is not readable") from error
    if count != selected.file_size or count <= 0:
        raise ChfReleaseRejected("CHF checkpoint byte length is incomplete")
    return count, "sha256:" + digest.hexdigest()


def _release_document(
    release_name: str,
    checkpoint_bytes: int,
    checkpoint_sha256: str,
    source_revision: str,
) -> dict[str, JsonValue]:
    return {
        "schema_id": SCHEMA_ID,
        "release_name": release_name,
        "runner_ref": RUNNER_REF,
        "runner_contract_id": CHF_RUNNER_CONTRACT_ID,
        "analysis_kind_ref": CHF_OFFERING.analysis_kind_ref,
        "result_schema_id": RESULT_SCHEMA_ID,
        "decode_policy_id": CHF_RESULT_IDENTITY.decode_policy.decode_policy_id,
        "upstream_revision": source_revision,
        "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
        "checkpoint": {
            "archive_member": ARCHIVE_MEMBER,
            "byte_length": checkpoint_bytes,
            "sha256": checkpoint_sha256,
        },
    }


def _require_archive_path(archive: Path) -> None:
    if not isinstance(archive, Path) or not archive.is_absolute():
        raise ChfReleaseRejected("CHF acquisition archive path must be absolute")
    try:
        resolved = archive.resolve(strict=True)
        metadata = archive.lstat()
    except OSError as error:
        raise ChfReleaseRejected("CHF acquisition archive does not exist") from error
    if archive != resolved or not stat.S_ISREG(metadata.st_mode):
        raise ChfReleaseRejected(
            "CHF acquisition archive must be one resolved non-symlink regular file"
        )


def _validate_archive_member_name(name: str) -> None:
    if type(name) is not str or not name or "\\" in name or "\0" in name:
        raise ChfReleaseRejected("CHF acquisition archive has an invalid member name")
    path = PurePosixPath(name)
    raw_parts = name.split("/")
    content_parts = raw_parts[:-1] if raw_parts[-1] == "" else raw_parts
    if (
        path.is_absolute()
        or not content_parts
        or any(part in {"", ".", ".."} for part in content_parts)
        or path.parts[0] not in {"weights", "__MACOSX"}
    ):
        raise ChfReleaseRejected("CHF acquisition archive has an invalid member path")


def _require_regular_checkpoint(member: zipfile.ZipInfo) -> None:
    mode = member.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if (
        member.is_dir()
        or file_type not in {0, stat.S_IFREG}
        or member.flag_bits & 1
        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        or member.file_size <= 0
    ):
        raise ChfReleaseRejected(
            "Selected CHF checkpoint is not an admitted regular ZIP member"
        )


def _validate_release_name(value: object) -> None:
    if type(value) is not str or _RELEASE_NAME.fullmatch(value) is None:
        raise ChfReleaseRejected("CHF release name has an invalid format")


def _validate_source_revision(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ChfReleaseRejected("CHF release source revision is invalid")


def _read_declaration_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ChfReleaseRejected("CHF release declaration does not exist") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ChfReleaseRejected(
            "CHF release declaration must be a regular non-symlink file"
        )
    return path.read_bytes()


def _current_source_revision(repository_root: Path) -> str:
    try:
        return read_nmrpeak_source_revision(
            repository_root / "families/nmrpeak/source-closure.paths"
        )
    except (OSError, ValueError) as error:
        raise ChfReleaseRejected(
            "NMRPeak source-closure declaration is invalid"
        ) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("write", "check"))
    parser.add_argument("--runner", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--declaration", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.runner != RUNNER_REF:
        parser.error(f"--runner must be {RUNNER_REF}")
    try:
        repository_root = Path(__file__).resolve().parents[1]
        source_revision = _current_source_revision(repository_root)
        if arguments.operation == "write":
            if arguments.declaration is not None:
                parser.error("--declaration is valid only for check")
            sys.stdout.buffer.write(
                candidate_release_bytes(
                    arguments.archive,
                    arguments.release,
                    source_revision=source_revision,
                )
            )
            return 0
        if arguments.declaration is None:
            parser.error("--declaration is required for check")
        declaration = _read_declaration_file(arguments.declaration)
        verify_release_bytes(
            declaration,
            arguments.archive,
            expected_release_name=arguments.release,
            expected_source_revision=source_revision,
        )
    except (ChfReleaseRejected, OSError) as error:
        parser.exit(2, f"CHF release rejected: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
