"""Authenticate one retained two-lane generation and its named public files."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .attempt_journal import validate_frozen_generation_id
from .canonical_json import JsonValue, canonical_json_bytes, parse_canonical_json_bytes
from .chf_runner_protocol import CHF_RUNNER_CODEC, CHF_RUNNER_CONTRACT_ID
from .generation_runtime import GenerationLane, GenerationRuntime
from .hf_runner_protocol import HF_RUNNER_CODEC, HF_RUNNER_CONTRACT_ID
from .lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE, LifecycleLane
from .product_input import INPUT_SCHEMA_ID
from .product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    NMRPEAK_SOURCE_CLOSURE_REF,
    ProviderResultFacts,
    RESULT_SCHEMA_ID,
    ResultLaneIdentity,
)
from .provider_http_contract import (
    PROVIDER_HTTP_CONTRACT_ID,
    PROVIDER_HTTP_RELEASE_REF,
)
from .run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    parse_canonical_utc_timestamp,
    run_generation_fingerprint,
    run_generation_material,
)
from .runner_protocol import RunnerFrameCodec


_DOMAIN = b"nmrpeak.frozen_generation.v1\0"
_SCHEMA_ID = "nmrpeak.frozen_generation.v1"
_PRODUCT_ID = "nmrpeak_default"
_MANIFEST_NAME = "manifest.json"
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_MANIFEST_BYTES = 262_144
_MAX_NAMED_FILES = 16
_MAX_NAMED_FILE_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class FrozenFile:
    """One digest-bound public file retained beside the manifest."""

    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class FrozenGeneration:
    """The authenticated manifest identity and its existing runtime projection."""

    frozen_generation_id: str
    runtime: GenerationRuntime
    files: tuple[FrozenFile, ...]


@dataclass(frozen=True, slots=True)
class _LaneShape:
    implementation_ref: str
    lane: LifecycleLane
    result_identity: ResultLaneIdentity
    runner_contract_id: str
    runner_codec: RunnerFrameCodec


_LANES = (
    _LaneShape(
        "hf",
        HF_LIFECYCLE_LANE,
        HF_RESULT_IDENTITY,
        HF_RUNNER_CONTRACT_ID,
        HF_RUNNER_CODEC,
    ),
    _LaneShape(
        "chf",
        CHF_LIFECYCLE_LANE,
        CHF_RESULT_IDENTITY,
        CHF_RUNNER_CONTRACT_ID,
        CHF_RUNNER_CODEC,
    ),
)


def frozen_generation_id(manifest: bytes) -> str:
    """Derive the content identity from exact canonical manifest bytes."""

    parse_canonical_json_bytes(manifest)
    return "sha256:" + sha256(_DOMAIN + manifest).hexdigest()


def render_frozen_generation_manifest(
    runtime: GenerationRuntime,
    files: tuple[FrozenFile, ...] = (),
) -> bytes:
    """Render runtime facts and an exact retained-file inventory canonically."""

    if type(runtime) is not GenerationRuntime:
        raise TypeError("Frozen generation rendering requires one admitted runtime")
    file_entries = _render_files(files)
    implementations: list[JsonValue] = []
    for shape, generation_lane in zip(_LANES, (runtime.hf, runtime.chf), strict=True):
        implementations.append(_render_lane(shape, generation_lane))
    return canonical_json_bytes(
        {
            "schema_id": _SCHEMA_ID,
            "provider_http_contract_id": PROVIDER_HTTP_CONTRACT_ID,
            "provider_http_release": PROVIDER_HTTP_RELEASE_REF,
            "product_id": _PRODUCT_ID,
            "provider_ref": runtime.hf.generation.provider_ref,
            "implementations": implementations,
            "files": file_entries,
        }
    )


def load_frozen_generation(
    root: Path,
    *,
    expected_frozen_generation_id: str,
) -> FrozenGeneration:
    """Rehash a retained directory and construct its fixed semantic runtime."""

    validate_frozen_generation_id(expected_frozen_generation_id)
    directory = Path(root)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Frozen generation root must be a non-symlink directory")
    manifest = _read_regular_file(directory / _MANIFEST_NAME, _MAX_MANIFEST_BYTES)
    actual_id = frozen_generation_id(manifest)
    if actual_id != expected_frozen_generation_id:
        raise ValueError("Frozen generation manifest identity does not match")
    document = parse_canonical_json_bytes(manifest)
    if type(document) is not dict or set(document) != {
        "files",
        "implementations",
        "product_id",
        "provider_http_contract_id",
        "provider_http_release",
        "provider_ref",
        "schema_id",
    }:
        raise ValueError("Frozen generation manifest fields are invalid")
    if (
        document["schema_id"] != _SCHEMA_ID
        or document["product_id"] != _PRODUCT_ID
        or document["provider_http_contract_id"] != PROVIDER_HTTP_CONTRACT_ID
        or document["provider_http_release"] != PROVIDER_HTTP_RELEASE_REF
    ):
        raise ValueError("Frozen generation static product facts have drifted")
    files = _load_files(directory, document["files"])
    runtime = _load_runtime(actual_id, document)
    _require_exact_inventory(directory, files)
    return FrozenGeneration(actual_id, runtime, files)


def _render_lane(shape: _LaneShape, lane: GenerationLane) -> dict[str, JsonValue]:
    if lane.lane is not shape.lane:
        raise ValueError("Frozen generation runtime lane order has drifted")
    facts = lane.result_facts
    return {
        "implementation_ref": shape.implementation_ref,
        "analysis_kind_ref": shape.lane.offering.analysis_kind_ref,
        "run_generation": run_generation_material(lane.generation),
        "run_generation_fingerprint": run_generation_fingerprint(lane.generation),
        "input_schema_id": INPUT_SCHEMA_ID,
        "result_schema_id": RESULT_SCHEMA_ID,
        "runner_ref": shape.result_identity.runner_ref,
        "runner_contract_id": shape.runner_contract_id,
        "decode_policy_id": shape.result_identity.decode_policy.decode_policy_id,
        "target": "cpu-x86_64",
        "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
        "checkpoint_sha256": facts.checkpoint_ref,
        "runner_image_input_id": facts.image_input_ref,
    }


def _render_files(files: tuple[FrozenFile, ...]) -> list[JsonValue]:
    if type(files) is not tuple or len(files) > _MAX_NAMED_FILES:
        raise ValueError("Frozen generation files must be a bounded tuple")
    rendered: list[JsonValue] = []
    paths: set[str] = set()
    for frozen_file in files:
        if type(frozen_file) is not FrozenFile:
            raise TypeError("Frozen generation files must be exact FrozenFile values")
        _require_relative_path(frozen_file.path)
        if frozen_file.path == _MANIFEST_NAME or frozen_file.path in paths:
            raise ValueError("Frozen generation file paths must be unique")
        if type(frozen_file.content) is not bytes or len(frozen_file.content) > _MAX_NAMED_FILE_BYTES:
            raise ValueError("Frozen generation file exceeds its byte limit")
        paths.add(frozen_file.path)
        rendered.append(
            {
                "path": frozen_file.path,
                "byte_length": len(frozen_file.content),
                "sha256": "sha256:" + sha256(frozen_file.content).hexdigest(),
            }
        )
    return rendered


def _load_runtime(frozen_id: str, document: dict[str, JsonValue]) -> GenerationRuntime:
    implementations = document["implementations"]
    if type(implementations) is not list or len(implementations) != len(_LANES):
        raise ValueError("Frozen generation must contain exactly HF and CHF")
    loaded = tuple(
        _load_lane(shape, value, document["provider_ref"])
        for shape, value in zip(_LANES, implementations, strict=True)
    )
    return GenerationRuntime(frozen_id, loaded[0], loaded[1])


def _load_lane(
    shape: _LaneShape,
    value: JsonValue,
    provider_ref: JsonValue,
) -> GenerationLane:
    if type(value) is not dict or set(value) != {
        "analysis_kind_ref",
        "checkpoint_sha256",
        "decode_policy_id",
        "implementation_ref",
        "input_schema_id",
        "result_schema_id",
        "run_generation",
        "run_generation_fingerprint",
        "runner_contract_id",
        "runner_image_input_id",
        "runner_ref",
        "source_closure_sha256",
        "target",
    }:
        raise ValueError("Frozen generation implementation fields are invalid")
    expected = {
        "implementation_ref": shape.implementation_ref,
        "analysis_kind_ref": shape.lane.offering.analysis_kind_ref,
        "input_schema_id": INPUT_SCHEMA_ID,
        "result_schema_id": RESULT_SCHEMA_ID,
        "runner_ref": shape.result_identity.runner_ref,
        "runner_contract_id": shape.runner_contract_id,
        "decode_policy_id": shape.result_identity.decode_policy.decode_policy_id,
        "target": "cpu-x86_64",
        "source_closure_sha256": NMRPEAK_SOURCE_CLOSURE_REF,
    }
    if any(value[name] != expected_value for name, expected_value in expected.items()):
        raise ValueError("Frozen generation implementation facts have drifted")
    generation = _load_run_generation(value["run_generation"])
    if generation.provider_ref != provider_ref:
        raise ValueError("Frozen generation provider identity is inconsistent")
    if run_generation_fingerprint(generation) != value["run_generation_fingerprint"]:
        raise ValueError("Frozen generation run fingerprint is inconsistent")
    checkpoint_ref = value["checkpoint_sha256"]
    image_input_ref = value["runner_image_input_id"]
    if not _is_sha256_ref(checkpoint_ref) or not _is_sha256_ref(image_input_ref):
        raise ValueError("Frozen generation runner digest is invalid")
    facts = ProviderResultFacts(
        shape.result_identity,
        shape.runner_contract_id,
        checkpoint_ref,
        image_input_ref,
    )
    return GenerationLane(shape.lane, generation, facts, shape.runner_codec)


def _load_run_generation(value: JsonValue) -> RunGenerationIdentity:
    if type(value) is not dict or set(value) != {
        "analysis_kind_ref",
        "generation_id",
        "provider_ref",
        "scope",
        "v",
    }:
        raise ValueError("Frozen generation run policy fields are invalid")
    scope = value["scope"]
    if type(scope) is not dict or set(scope) != {"kind", "not_after", "not_before"}:
        raise ValueError("Frozen generation run scope fields are invalid")
    if value["v"] != 1 or scope["kind"] != "created_at_window":
        raise ValueError("Frozen generation run policy version is invalid")
    not_after = scope["not_after"]
    return RunGenerationIdentity(
        provider_ref=value["provider_ref"],
        analysis_kind_ref=value["analysis_kind_ref"],
        generation_id=value["generation_id"],
        scope=CreatedAtWindow(
            parse_canonical_utc_timestamp(scope["not_before"]),
            None if not_after is None else parse_canonical_utc_timestamp(not_after),
        ),
    )


def _load_files(root: Path, value: JsonValue) -> tuple[FrozenFile, ...]:
    if type(value) is not list or len(value) > _MAX_NAMED_FILES:
        raise ValueError("Frozen generation file inventory is invalid")
    files: list[FrozenFile] = []
    paths: set[str] = set()
    for entry in value:
        if type(entry) is not dict or set(entry) != {"byte_length", "path", "sha256"}:
            raise ValueError("Frozen generation file entry is invalid")
        path = entry["path"]
        length = entry["byte_length"]
        digest = entry["sha256"]
        _require_relative_path(path)
        if path == _MANIFEST_NAME or path in paths:
            raise ValueError("Frozen generation file paths must be unique")
        if type(length) is not int or not 0 <= length <= _MAX_NAMED_FILE_BYTES:
            raise ValueError("Frozen generation file length is invalid")
        if not _is_sha256_ref(digest):
            raise ValueError("Frozen generation file digest is invalid")
        try:
            content = _read_retained_file(root, path, _MAX_NAMED_FILE_BYTES)
        except OSError:
            raise ValueError(
                "Frozen generation named file path is not a retained regular path"
            ) from None
        if len(content) != length or "sha256:" + sha256(content).hexdigest() != digest:
            raise ValueError("Frozen generation named file does not match its manifest")
        paths.add(path)
        files.append(FrozenFile(path, content))
    return tuple(files)


def _require_exact_inventory(root: Path, files: tuple[FrozenFile, ...]) -> None:
    expected_files = {_MANIFEST_NAME, *(frozen_file.path for frozen_file in files)}
    expected_directories = {
        PurePosixPath(*PurePosixPath(frozen_file.path).parts[:depth]).as_posix()
        for frozen_file in files
        for depth in range(1, len(PurePosixPath(frozen_file.path).parts))
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise ValueError("Frozen generation directory must not contain symlinks")
        if candidate.is_file():
            actual_files.add(relative)
        elif candidate.is_dir():
            actual_directories.add(relative)
        else:
            raise ValueError("Frozen generation directory contains a special file")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("Frozen generation directory inventory does not match")


def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > maximum_bytes:
            raise ValueError("Frozen generation input must be a bounded regular file")
        content = os.read(descriptor, maximum_bytes + 1)
        if len(content) != status.st_size:
            raise ValueError("Frozen generation input changed while it was read")
        return content
    finally:
        os.close(descriptor)


def _read_retained_file(root: Path, relative_path: str, maximum_bytes: int) -> bytes:
    """Open every retained path component without following a symlink."""

    parts = PurePosixPath(relative_path).parts
    directory_fd = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for part in parts[:-1]:
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > maximum_bytes:
                raise ValueError("Frozen generation input must be a bounded regular file")
            content = os.read(descriptor, maximum_bytes + 1)
            if len(content) != status.st_size:
                raise ValueError("Frozen generation input changed while it was read")
            return content
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _require_relative_path(value: object) -> None:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 255:
        raise ValueError("Frozen generation file path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Frozen generation file path must be normalized and relative")


def _is_sha256_ref(value: object) -> bool:
    return type(value) is str and _SHA256_REF.fullmatch(value) is not None
