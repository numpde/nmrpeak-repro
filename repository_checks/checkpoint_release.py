"""Render and verify one lane-owned NMRPeak checkpoint release."""

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
from nmrpeak_provider.product_result import NMRPEAK_SOURCE_CLOSURE_REF, RESULT_SCHEMA_ID
from repository_checks.nmrpeak_source import read_nmrpeak_source_revision


_RELEASE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_COPY_CHUNK_BYTES = 8 * 1024 * 1024


class CheckpointReleaseRejected(ValueError):
    """The archive or declaration cannot authorize a checkpoint release."""


@dataclass(frozen=True, slots=True)
class CheckpointRelease:
    """Immutable identities required before one checkpoint may be imported."""

    release_name: str
    checkpoint_bytes: int
    checkpoint_sha256: str


@dataclass(frozen=True, slots=True)
class CheckpointReleaseSpec:
    """Code-owned product identity for one checkpoint lane."""

    lane_name: str
    schema_id: str
    runner_ref: str
    runner_contract_id: str
    analysis_kind_ref: str
    decode_policy_id: str
    archive_member: str


def candidate_release_bytes(
    spec: CheckpointReleaseSpec,
    archive: Path,
    release_name: str,
    *,
    source_revision: str,
) -> bytes:
    """Stream the selected member and render one canonical candidate declaration."""

    _validate_release_name(spec, release_name)
    _validate_source_revision(spec, source_revision)
    size, digest = measure_checkpoint_member(spec, archive)
    return canonical_json_bytes(
        _release_document(spec, release_name, size, digest, source_revision)
    )


def verify_release_bytes(
    spec: CheckpointReleaseSpec,
    raw: bytes,
    archive: Path,
    *,
    expected_release_name: str,
    expected_source_revision: str,
) -> CheckpointRelease:
    """Bind one canonical declaration to the selected archive member bytes."""

    declaration = parse_release_bytes(
        spec,
        raw,
        expected_release_name=expected_release_name,
        expected_source_revision=expected_source_revision,
    )
    size, digest = measure_checkpoint_member(spec, archive)
    if (
        size != declaration.checkpoint_bytes
        or digest != declaration.checkpoint_sha256
    ):
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} checkpoint bytes do not match the release declaration"
        )
    return declaration


def parse_release_bytes(
    spec: CheckpointReleaseSpec,
    raw: bytes,
    *,
    expected_release_name: str,
    expected_source_revision: str,
) -> CheckpointRelease:
    """Admit one exact closed declaration without consulting an archive."""

    _validate_release_name(spec, expected_release_name)
    _validate_source_revision(spec, expected_source_revision)
    try:
        document = parse_canonical_json_bytes(raw)
    except CanonicalJsonError as error:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release declaration is not canonical JSON"
        ) from error
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
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release declaration has an invalid shape"
        )
    expected_facts = {
        "schema_id": spec.schema_id,
        "release_name": expected_release_name,
        "runner_ref": spec.runner_ref,
        "runner_contract_id": spec.runner_contract_id,
        "analysis_kind_ref": spec.analysis_kind_ref,
        "result_schema_id": RESULT_SCHEMA_ID,
        "decode_policy_id": spec.decode_policy_id,
        "upstream_revision": expected_source_revision,
        "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
    }
    if any(document[name] != value for name, value in expected_facts.items()):
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release declaration identity has drifted"
        )
    checkpoint = document["checkpoint"]
    if type(checkpoint) is not dict or set(checkpoint) != {
        "archive_member",
        "byte_length",
        "sha256",
    }:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release checkpoint facts have an invalid shape"
        )
    byte_length = checkpoint["byte_length"]
    digest = checkpoint["sha256"]
    if (
        checkpoint["archive_member"] != spec.archive_member
        or type(byte_length) is not int
        or type(byte_length) is bool
        or byte_length <= 0
        or type(digest) is not str
        or _SHA256_REF.fullmatch(digest) is None
    ):
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release checkpoint facts are invalid"
        )
    if canonical_json_bytes(document) != raw:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release declaration rendering has drifted"
        )
    return CheckpointRelease(expected_release_name, byte_length, digest)


def measure_checkpoint_member(
    spec: CheckpointReleaseSpec,
    archive: Path,
) -> tuple[int, str]:
    """Inventory the ZIP and hash only one selected regular member."""

    _require_archive_path(spec, archive)
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            by_name: dict[str, zipfile.ZipInfo] = {}
            for member in members:
                _validate_archive_member_name(spec, member.filename)
                if member.filename in by_name:
                    raise CheckpointReleaseRejected(
                        f"{spec.lane_name} acquisition archive contains duplicate member names"
                    )
                by_name[member.filename] = member
            selected = by_name.get(spec.archive_member)
            if selected is None:
                raise CheckpointReleaseRejected(
                    f"{spec.lane_name} acquisition archive does not contain the selected checkpoint"
                )
            _require_regular_checkpoint(spec, selected)
            digest = sha256()
            count = 0
            with bundle.open(selected, "r") as source:
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    count += len(chunk)
                    if count > selected.file_size:
                        raise CheckpointReleaseRejected(
                            f"{spec.lane_name} checkpoint produced more bytes than declared"
                        )
                    digest.update(chunk)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} acquisition archive is not readable"
        ) from error
    if count != selected.file_size or count <= 0:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} checkpoint byte length is incomplete"
        )
    return count, "sha256:" + digest.hexdigest()


def _release_document(
    spec: CheckpointReleaseSpec,
    release_name: str,
    checkpoint_bytes: int,
    checkpoint_sha256: str,
    source_revision: str,
) -> dict[str, JsonValue]:
    return {
        "schema_id": spec.schema_id,
        "release_name": release_name,
        "runner_ref": spec.runner_ref,
        "runner_contract_id": spec.runner_contract_id,
        "analysis_kind_ref": spec.analysis_kind_ref,
        "result_schema_id": RESULT_SCHEMA_ID,
        "decode_policy_id": spec.decode_policy_id,
        "upstream_revision": source_revision,
        "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
        "checkpoint": {
            "archive_member": spec.archive_member,
            "byte_length": checkpoint_bytes,
            "sha256": checkpoint_sha256,
        },
    }


def _require_archive_path(spec: CheckpointReleaseSpec, archive: Path) -> None:
    if not isinstance(archive, Path) or not archive.is_absolute():
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} acquisition archive path must be absolute"
        )
    try:
        resolved = archive.resolve(strict=True)
        metadata = archive.lstat()
    except OSError as error:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} acquisition archive does not exist"
        ) from error
    if archive != resolved or not stat.S_ISREG(metadata.st_mode):
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} acquisition archive must be one resolved non-symlink regular file"
        )


def _validate_archive_member_name(spec: CheckpointReleaseSpec, name: str) -> None:
    if type(name) is not str or not name or "\\" in name or "\0" in name:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} acquisition archive has an invalid member name"
        )
    path = PurePosixPath(name)
    raw_parts = name.split("/")
    content_parts = raw_parts[:-1] if raw_parts[-1] == "" else raw_parts
    if (
        path.is_absolute()
        or not content_parts
        or any(part in {"", ".", ".."} for part in content_parts)
        or path.parts[0] not in {"weights", "__MACOSX"}
    ):
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} acquisition archive has an invalid member path"
        )


def _require_regular_checkpoint(
    spec: CheckpointReleaseSpec,
    member: zipfile.ZipInfo,
) -> None:
    mode = member.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if (
        member.is_dir()
        or file_type not in {0, stat.S_IFREG}
        or member.flag_bits & 1
        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        or member.file_size <= 0
    ):
        raise CheckpointReleaseRejected(
            f"Selected {spec.lane_name} checkpoint is not an admitted regular ZIP member"
        )


def _validate_release_name(spec: CheckpointReleaseSpec, value: object) -> None:
    if type(value) is not str or _RELEASE_NAME.fullmatch(value) is None:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release name has an invalid format"
        )


def _validate_source_revision(spec: CheckpointReleaseSpec, value: object) -> None:
    if (
        type(value) is not str
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release source revision is invalid"
        )


def _read_declaration_file(spec: CheckpointReleaseSpec, path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release declaration does not exist"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise CheckpointReleaseRejected(
            f"{spec.lane_name} release declaration must be a regular non-symlink file"
        )
    return path.read_bytes()


def _current_source_revision(
    spec: CheckpointReleaseSpec,
    repository_root: Path,
) -> str:
    try:
        return read_nmrpeak_source_revision(
            repository_root / "families/nmrpeak/source-closure.paths"
        )
    except (OSError, ValueError) as error:
        raise CheckpointReleaseRejected(
            "NMRPeak source-closure declaration is invalid"
        ) from error


def run_release_cli(
    spec: CheckpointReleaseSpec,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("write", "check"))
    parser.add_argument("--runner", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--declaration", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.runner != spec.runner_ref:
        parser.error(f"--runner must be {spec.runner_ref}")
    try:
        repository_root = Path(__file__).resolve().parents[1]
        source_revision = _current_source_revision(spec, repository_root)
        if arguments.operation == "write":
            if arguments.declaration is not None:
                parser.error("--declaration is valid only for check")
            sys.stdout.buffer.write(
                candidate_release_bytes(
                    spec,
                    arguments.archive,
                    arguments.release,
                    source_revision=source_revision,
                )
            )
            return 0
        if arguments.declaration is None:
            parser.error("--declaration is required for check")
        declaration = _read_declaration_file(spec, arguments.declaration)
        verify_release_bytes(
            spec,
            declaration,
            arguments.archive,
            expected_release_name=arguments.release,
            expected_source_revision=source_revision,
        )
    except (CheckpointReleaseRejected, OSError) as error:
        parser.exit(2, f"{spec.lane_name} release rejected: {error}\n")
    return 0
