"""Join one literal two-lane deployment declaration to existing authorities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CODEC,
    CHF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.frozen_generation import (
    FrozenFile,
    frozen_generation_id,
    render_frozen_generation_manifest,
)
from nmrpeak_provider.generation_runtime import GenerationLane, GenerationRuntime
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CODEC, HF_RUNNER_CONTRACT_ID
from nmrpeak_provider.lifecycle_lane import CHF_LIFECYCLE_LANE, HF_LIFECYCLE_LANE
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    ProviderResultFacts,
)
from nmrpeak_provider.provider_config import decode_provider_runtime_config
from nmrpeak_provider.run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    parse_canonical_utc_timestamp,
)
from repository_checks.chf_release import (
    ChfCheckpointRelease,
    parse_release_bytes as parse_chf_release_bytes,
)
from repository_checks.hf_release import (
    HfCheckpointRelease,
    parse_release_bytes as parse_hf_release_bytes,
)


SCHEMA_ID = "nmrpeak.named_deployment.v1"
GENERATED_FROZEN_ID = "sha256:" + "0" * 64
_MAX_INPUT_BYTES = 65_536
_TARGET = "cpu-x86_64"


class NamedDeploymentRejected(ValueError):
    """One named deployment input cannot select the fixed product safely."""


@dataclass(frozen=True, slots=True)
class LaneSelection:
    """Operator-owned release and Job-admission policy for one fixed lane."""

    release_name: str
    generation: RunGenerationIdentity


@dataclass(frozen=True, slots=True)
class NamedDeployment:
    """The mutable semantic selections for the fixed two-lane product."""

    provider_ref: str
    hf: LaneSelection
    chf: LaneSelection


@dataclass(frozen=True, slots=True)
class RenderedGeneration:
    """Exact provider inputs derived from one admitted deployment selection."""

    frozen_generation_id: str
    manifest: bytes
    files: tuple[FrozenFile, ...]
    provider_config: bytes


@dataclass(frozen=True, slots=True)
class DeploymentReleases:
    """The two declarations admitted once for one deployment render."""

    hf: HfCheckpointRelease
    chf: ChfCheckpointRelease


def load_named_deployment(path: Path) -> NamedDeployment:
    """Decode one closed deployment TOML without inferring a lane or target."""

    document = _read_toml(path)
    _fields("deployment", document, {"schema_id", "provider_ref", "implementations"})
    if document["schema_id"] != SCHEMA_ID:
        raise NamedDeploymentRejected("Named deployment schema is unsupported")
    implementations = _table(document, "implementations", {"hf", "chf"})
    provider_ref = document["provider_ref"]
    hf = _lane(implementations, "hf", provider_ref, "mol_from_1h_peaks")
    chf = _lane(implementations, "chf", provider_ref, "mol_from_1h_13c_formula")
    return NamedDeployment(provider_ref, hf, chf)


def admit_deployment_releases(
    selection: NamedDeployment,
    *,
    hf_release_declaration: bytes,
    chf_release_declaration: bytes,
    upstream_revision: str,
) -> DeploymentReleases:
    """Parse both selected canonical release declarations exactly once."""

    if type(selection) is not NamedDeployment:
        raise TypeError("Release admission requires one named deployment")
    return DeploymentReleases(
        parse_hf_release_bytes(
            hf_release_declaration,
            expected_release_name=selection.hf.release_name,
            expected_source_revision=upstream_revision,
        ),
        parse_chf_release_bytes(
            chf_release_declaration,
            expected_release_name=selection.chf.release_name,
            expected_source_revision=upstream_revision,
        ),
    )


def render_generation(
    selection: NamedDeployment,
    releases: DeploymentReleases,
    *,
    provider_config_template: bytes,
    hf_image_input_id: str,
    chf_image_input_id: str,
    hf_hello: bytes,
    chf_hello: bytes,
    topology: bytes,
) -> RenderedGeneration:
    """Render one generation through the existing release and runtime owners."""

    if type(selection) is not NamedDeployment:
        raise TypeError("Generation rendering requires one named deployment")
    if type(releases) is not DeploymentReleases:
        raise TypeError("Generation rendering requires admitted releases")
    topology_document = parse_canonical_json_bytes(topology)
    if (
        type(topology_document) is not dict
        or topology_document.get("schema_id")
        != "nmrpeak.deployment_topology.v1"
    ):
        raise NamedDeploymentRejected("Deployment topology projection is invalid")
    _require_topology_generation(
        topology_document,
        hf_checkpoint=releases.hf.checkpoint_sha256,
        chf_checkpoint=releases.chf.checkpoint_sha256,
        hf_image_input=hf_image_input_id,
        chf_image_input=chf_image_input_id,
    )
    files = (
        FrozenFile("hello/hf.txt", hf_hello),
        FrozenFile("hello/chf.txt", chf_hello),
        FrozenFile("deployment/topology.json", topology),
    )
    runtime = GenerationRuntime(
        GENERATED_FROZEN_ID,
        GenerationLane(
            HF_LIFECYCLE_LANE,
            selection.hf.generation,
            ProviderResultFacts(
                HF_RESULT_IDENTITY,
                HF_RUNNER_CONTRACT_ID,
                releases.hf.checkpoint_sha256,
                hf_image_input_id,
            ),
            HF_RUNNER_CODEC,
        ),
        GenerationLane(
            CHF_LIFECYCLE_LANE,
            selection.chf.generation,
            ProviderResultFacts(
                CHF_RESULT_IDENTITY,
                CHF_RUNNER_CONTRACT_ID,
                releases.chf.checkpoint_sha256,
                chf_image_input_id,
            ),
            CHF_RUNNER_CODEC,
        ),
    )
    manifest = render_frozen_generation_manifest(runtime, files)
    identity = frozen_generation_id(manifest)
    provider_config = render_provider_config(provider_config_template, identity)
    return RenderedGeneration(identity, manifest, files, provider_config)


def render_provider_config(template: bytes, identity: str) -> bytes:
    """Replace the sole generated field and prove the provider consumes it."""

    if type(template) is not bytes:
        raise TypeError("Provider config template must be exact bytes")
    marker = f'frozen_generation_id = "{GENERATED_FROZEN_ID}"'.encode()
    if template.count(marker) != 1:
        raise NamedDeploymentRejected(
            "Provider config must contain the single generated frozen identity"
        )
    rendered = template.replace(
        marker,
        f'frozen_generation_id = "{identity}"'.encode(),
    )
    decode_provider_runtime_config(rendered)
    return rendered


def _require_topology_generation(
    topology: dict[str, object],
    *,
    hf_checkpoint: str,
    chf_checkpoint: str,
    hf_image_input: str,
    chf_image_input: str,
) -> None:
    if topology.get("checkpoint_releases") != {
        "hf": hf_checkpoint,
        "chf": chf_checkpoint,
    }:
        raise NamedDeploymentRejected(
            "Deployment topology names another checkpoint generation"
        )
    services = topology.get("services")
    if type(services) is not list:
        raise NamedDeploymentRejected("Deployment topology services are invalid")
    runner_inputs = {
        service.get("role"): service.get("image_input_id")
        for service in services
        if type(service) is dict and service.get("role") in {"hf", "chf"}
    }
    if runner_inputs != {"hf": hf_image_input, "chf": chf_image_input}:
        raise NamedDeploymentRejected(
            "Deployment topology names another runner generation"
        )


def _lane(
    implementations: dict[str, object],
    name: str,
    provider_ref: object,
    analysis_kind_ref: str,
) -> LaneSelection:
    lane = _table(implementations, name, {"release", "run_generation", "target"})
    if lane["target"] != _TARGET:
        raise NamedDeploymentRejected(f"Named deployment {name} target is unsupported")
    generation = _table(
        lane,
        "run_generation",
        {"generation_id", "not_before"},
        {"not_after"},
    )
    not_after = generation.get("not_after")
    try:
        identity = RunGenerationIdentity(
            provider_ref=provider_ref,
            analysis_kind_ref=analysis_kind_ref,
            generation_id=generation["generation_id"],
            scope=CreatedAtWindow(
                parse_canonical_utc_timestamp(generation["not_before"]),
                None
                if not_after is None
                else parse_canonical_utc_timestamp(not_after),
            ),
        )
    except (TypeError, ValueError) as error:
        raise NamedDeploymentRejected(
            f"Named deployment {name} run generation is invalid"
        ) from error
    release = lane["release"]
    if type(release) is not str:
        raise NamedDeploymentRejected(
            f"Named deployment {name} release must be a string"
        )
    return LaneSelection(release, identity)


def _read_toml(path: Path) -> dict[str, object]:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > _MAX_INPUT_BYTES
        ):
            raise NamedDeploymentRejected(
                "Named deployment input must be a bounded non-symlink regular file"
            )
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise NamedDeploymentRejected(
            "Named deployment input is not valid TOML"
        ) from error
    return document


def _table(
    document: dict[str, object],
    name: str,
    required: set[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    value = document.get(name)
    if type(value) is not dict:
        raise NamedDeploymentRejected(f"Named deployment {name} must be a table")
    _fields(name, value, required, optional)
    return value


def _fields(
    name: str,
    value: dict[str, object],
    required: set[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    actual = set(value)
    if not required <= actual or actual - required - optional:
        raise NamedDeploymentRejected(f"Named deployment {name} fields are invalid")
