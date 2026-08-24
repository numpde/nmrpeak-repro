"""Authenticate the immutable NMR API provider contract consumed here."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1, sha256
import os
from pathlib import Path, PurePosixPath
import stat

from .canonical_json import (
    JsonValue,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


PROVIDER_HTTP_CONTRACT_ID = "nmr.provider.http.v1"
PROVIDER_HTTP_RELEASE_REF = (
    "sha256:ad01fc7c8a070ae752ab1bffb8abe954f3629e21b262cd0e9f0a554a3b07684c"
)
PROVIDER_HTTP_RELEASE_SOURCE_REVISION = (
    "c385071ab0958ce0ab2803424060e80f9b884658"
)

_MANIFEST_SCHEMA_ID = "nmr.provider.http_contract_release_manifest.v1"
_ROUTES = {
    "execution_attempt_complete": ("POST", "/provider/v1/execution-attempts/complete"),
    "execution_attempt_fail": ("POST", "/provider/v1/execution-attempts/fail"),
    "execution_attempt_progress": (
        "PUT",
        "/provider/v1/execution-attempts/{execution_attempt_ref}/progress",
    ),
    "execution_attempt_read": (
        "GET",
        "/provider/v1/execution-attempts/{execution_attempt_ref}",
    ),
    "execution_attempt_start": ("POST", "/provider/v1/execution-attempts/start"),
    "execution_attempts_list": ("GET", "/provider/v1/execution-attempts"),
    "job_input_read": ("GET", "/provider/v1/jobs/{job_ref}/input"),
    "jobs_list": ("GET", "/provider/v1/jobs"),
    "provider_hello": ("POST", "/provider/v1/hello"),
}
_SIGNING_VECTOR_NAMES = {
    "execution-attempt-start-post",
    "provider-jobs-list-get",
}


@dataclass(frozen=True, slots=True)
class ProviderHttpContractRelease:
    """One authenticated release and its provider-consumed public facts."""

    release_ref: str
    source_revision: str
    openapi: dict[str, JsonValue]
    schemas: dict[str, dict[str, JsonValue]]
    routes: dict[str, tuple[str, str]]
    signing_profile: dict[str, JsonValue]
    signing_vectors: tuple[dict[str, JsonValue], ...]
    attempt_recovery: dict[str, JsonValue]


def load_provider_http_contract_release(
    release_root: Path,
) -> ProviderHttpContractRelease:
    """Authenticate and load the exact provider HTTP release at ``release_root``."""

    root = Path(release_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("provider contract release root must be a directory")
    manifest = _read_canonical_object(root / "manifest.json")
    if set(manifest) != {
        "artifacts",
        "contract_id",
        "release_ref",
        "schema_id",
        "source_revision",
    }:
        raise ValueError("provider contract release manifest fields have drifted")
    if manifest.get("schema_id") != _MANIFEST_SCHEMA_ID:
        raise ValueError("provider contract release manifest schema has drifted")
    if manifest.get("contract_id") != PROVIDER_HTTP_CONTRACT_ID:
        raise ValueError("provider HTTP contract identity has drifted")
    if manifest.get("release_ref") != PROVIDER_HTTP_RELEASE_REF:
        raise ValueError("provider HTTP release identity has drifted")
    if manifest.get("source_revision") != PROVIDER_HTTP_RELEASE_SOURCE_REVISION:
        raise ValueError("provider HTTP release provenance has drifted")

    artifacts = _artifact_inventory(manifest.get("artifacts"))
    release_material: dict[str, JsonValue] = {
        "artifacts": artifacts,
        "contract_id": PROVIDER_HTTP_CONTRACT_ID,
    }
    derived_ref = "sha256:" + sha256(canonical_json_bytes(release_material)).hexdigest()
    if derived_ref != PROVIDER_HTTP_RELEASE_REF:
        raise ValueError("provider HTTP release digest does not match its contents")
    artifact_bytes = _authenticate_artifacts(root, artifacts)

    openapi = _decode_canonical_object(
        artifact_bytes["openapi/openapi.v1.json"],
        name="provider release OpenAPI",
    )
    if openapi.get("x-nmr-contract-id") != PROVIDER_HTTP_CONTRACT_ID:
        raise ValueError("provider release OpenAPI identity has drifted")
    schemas = _load_schemas(artifact_bytes)
    routes = _load_routes(openapi)
    signing_profile, signing_vectors = _load_signing_contract(openapi)
    attempt_recovery = _load_attempt_recovery(openapi)
    return ProviderHttpContractRelease(
        release_ref=PROVIDER_HTTP_RELEASE_REF,
        source_revision=PROVIDER_HTTP_RELEASE_SOURCE_REVISION,
        openapi=openapi,
        schemas=schemas,
        routes=routes,
        signing_profile=signing_profile,
        signing_vectors=signing_vectors,
        attempt_recovery=attempt_recovery,
    )


def _artifact_inventory(value: object) -> list[dict[str, JsonValue]]:
    if type(value) is not list or not value:
        raise ValueError("provider contract release artifacts must be a non-empty list")
    artifacts: list[dict[str, JsonValue]] = []
    paths: set[str] = set()
    for entry in value:
        if type(entry) is not dict or set(entry) != {
            "byte_length",
            "git_blob_sha1",
            "path",
        }:
            raise ValueError("provider contract release artifact entry is malformed")
        path = entry.get("path")
        length = entry.get("byte_length")
        blob = entry.get("git_blob_sha1")
        if (
            type(path) is not str
            or not path
            or path.startswith("/")
            or "\\" in path
            or PurePosixPath(path).as_posix() != path
            or ".." in Path(path).parts
            or path in paths
        ):
            raise ValueError("provider contract release artifact path is invalid")
        if type(length) is not int or length <= 0:
            raise ValueError("provider contract release artifact length is invalid")
        if (
            type(blob) is not str
            or len(blob) != 40
            or any(character not in "0123456789abcdef" for character in blob)
        ):
            raise ValueError("provider contract release artifact hash is invalid")
        paths.add(path)
        artifacts.append(
            {"byte_length": length, "git_blob_sha1": blob, "path": path}
        )
    return sorted(artifacts, key=lambda artifact: str(artifact["path"]))


def _authenticate_artifacts(
    root: Path,
    artifacts: list[dict[str, JsonValue]],
) -> dict[str, bytes]:
    expected = {"manifest.json", *(str(artifact["path"]) for artifact in artifacts)}
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("provider contract release contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise ValueError("provider contract release contains a special file")
    if actual != expected:
        raise ValueError("provider contract release file inventory has drifted")

    authenticated: dict[str, bytes] = {}
    for artifact in artifacts:
        relative_path = str(artifact["path"])
        raw = _read_regular_file(
            root / relative_path,
            label=f"provider release artifact {relative_path}",
        )
        if len(raw) != artifact["byte_length"]:
            raise ValueError(f"provider release artifact length drift: {relative_path}")
        if _git_blob_sha1(raw) != artifact["git_blob_sha1"]:
            raise ValueError(
                f"provider release artifact content drift: {relative_path}"
            )
        authenticated[relative_path] = raw
    return authenticated


def _load_schemas(
    artifacts: dict[str, bytes],
) -> dict[str, dict[str, JsonValue]]:
    schemas: dict[str, dict[str, JsonValue]] = {}
    for path, raw in artifacts.items():
        if not path.startswith("schemas/"):
            continue
        schema = _decode_canonical_object(raw, name=path)
        schema_id = schema.get("$id")
        if type(schema_id) is not str or not schema_id or schema_id in schemas:
            raise ValueError("provider release schema identity has drifted")
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError(f"provider release schema draft drift: {path}")
        schemas[schema_id] = schema
    if len(schemas) != 14:
        raise ValueError("provider release schema inventory has drifted")
    return schemas


def _load_routes(openapi: dict[str, JsonValue]) -> dict[str, tuple[str, str]]:
    paths = openapi.get("paths")
    if type(paths) is not dict:
        raise ValueError("provider release route inventory is malformed")
    routes: dict[str, tuple[str, str]] = {}
    for path, path_item in paths.items():
        if type(path) is not str or type(path_item) is not dict:
            raise ValueError("provider release route inventory is malformed")
        for method, operation in path_item.items():
            if type(method) is not str or type(operation) is not dict:
                raise ValueError("provider release route operation is malformed")
            operation_id = operation.get("operationId")
            if operation_id in _ROUTES:
                if operation_id in routes:
                    raise ValueError("provider release repeats a route identity")
                routes[operation_id] = (method.upper(), path)
    if routes != _ROUTES:
        raise ValueError("provider release route inventory has drifted")
    return routes


def _load_signing_contract(
    openapi: dict[str, JsonValue],
) -> tuple[dict[str, JsonValue], tuple[dict[str, JsonValue], ...]]:
    profile = openapi.get("x-nmr-request-signature-profile")
    vectors = openapi.get("x-nmr-signature-conformance-vectors")
    if type(profile) is not dict:
        raise ValueError("provider release signing profile is malformed")
    if type(vectors) is not list or any(type(vector) is not dict for vector in vectors):
        raise ValueError("provider release signing vectors are malformed")
    names = {vector.get("name") for vector in vectors}
    if names != _SIGNING_VECTOR_NAMES or len(vectors) != len(names):
        raise ValueError("provider release signing vector inventory has drifted")
    return profile, tuple(vectors)


def _load_attempt_recovery(openapi: dict[str, JsonValue]) -> dict[str, JsonValue]:
    recovery = openapi.get("x-nmr-attempt-recovery")
    if type(recovery) is not dict:
        raise ValueError("provider release Attempt recovery contract is malformed")
    return recovery


def _git_blob_sha1(raw: bytes) -> str:
    digest = sha1(usedforsecurity=False)
    digest.update(f"blob {len(raw)}\0".encode("ascii"))
    digest.update(raw)
    return digest.hexdigest()


def _read_canonical_object(path: Path) -> dict[str, JsonValue]:
    raw = _read_regular_file(path, label="provider contract release manifest")
    return _decode_canonical_object(raw, name="provider release manifest")


def _read_regular_file(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{label} must be a readable regular file") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_canonical_object(raw: bytes, *, name: str) -> dict[str, JsonValue]:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise ValueError(f"{name} must end with exactly one LF")
    try:
        value = parse_canonical_json_bytes(raw[:-1])
    except ValueError as error:
        raise ValueError(f"{name} is not canonical JSON") from error
    if type(value) is not dict:
        raise ValueError(f"{name} must contain a JSON object")
    return value
