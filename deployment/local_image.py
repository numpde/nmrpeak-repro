"""Resolve one locally built image by its exact committed input identity."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess


_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_INSPECT_BYTES = 1_048_576


class LocalImageRejected(RuntimeError):
    """A local image does not prove the selected build-input identity."""


@dataclass(frozen=True, slots=True)
class LocalImageSpec:
    """Code-owned repository and runtime labels for one fixed image role."""

    repository: str
    input_id: str
    labels: tuple[tuple[str, str], ...]
    entrypoint: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalImage:
    """One immutable daemon image admitted for Compose rendering."""

    image_id: str
    input_id: str


def resolve_local_image(docker: Path, spec: LocalImageSpec) -> LocalImage:
    """Inspect the input-addressed local tag and admit its immutable image ID."""

    if type(spec) is not LocalImageSpec or _SHA256_REF.fullmatch(spec.input_id) is None:
        raise LocalImageRejected("Local image requires an exact input identity")
    tag = f"{spec.repository}:{spec.input_id.removeprefix('sha256:')}"
    try:
        result = subprocess.run(
            (str(docker), "image", "inspect", tag),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalImageRejected(
            f"Docker could not inspect the required local image: {tag}"
        ) from error
    if result.returncode != 0 or not result.stdout or len(result.stdout) > _MAX_INSPECT_BYTES:
        raise LocalImageRejected(f"Required local image is unavailable: {tag}")
    try:
        documents = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LocalImageRejected("Docker returned invalid image inspection JSON") from error
    if type(documents) is not list or len(documents) != 1:
        raise LocalImageRejected("Docker did not inspect exactly one local image")
    document = documents[0]
    if type(document) is not dict:
        raise LocalImageRejected("Docker image inspection has an invalid shape")
    image_id = document.get("Id")
    config = document.get("Config")
    if (
        type(image_id) is not str
        or _SHA256_REF.fullmatch(image_id) is None
        or document.get("Os") != "linux"
        or document.get("Architecture") != "amd64"
        or type(config) is not dict
        or config.get("User") != "65532:65532"
        or config.get("Entrypoint") != list(spec.entrypoint)
    ):
        raise LocalImageRejected("Local image runtime identity has drifted")
    labels = config.get("Labels")
    expected_labels = dict(spec.labels) | {
        "io.numpde.nmrpeak.image.input-id": spec.input_id
    }
    if type(labels) is not dict or any(
        labels.get(name) != value for name, value in expected_labels.items()
    ):
        raise LocalImageRejected("Local image labels do not match its selected role")
    return LocalImage(image_id, spec.input_id)
