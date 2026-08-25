"""CHF identity over the shared checkpoint-volume operation."""

from __future__ import annotations

from pathlib import Path

from repository_checks.checkpoint import (
    DOCKER_BINARY,
    CheckpointImportSpec,
    CheckpointOperationRejected,
    CheckpointVolume,
    checkpoint_volume_name as _checkpoint_volume_name,
    import_checkpoint,
    recover_checkpoint,
    run_checkpoint_cli,
    verify_checkpoint_volume,
)
from repository_checks.chf_release import CHF_RELEASE_SPEC, ChfCheckpointRelease


VOLUME_SCHEMA_ID = "nmrpeak.checkpoint_volume.chf.v1"
VOLUME_PREFIX = "nmrpeak-chf-checkpoint-"
CHF_CHECKPOINT_SPEC = CheckpointImportSpec(
    lane_name="CHF",
    release_directory=Path("models/nmrpeak_chf_v1/releases"),
    volume_schema_id=VOLUME_SCHEMA_ID,
    volume_prefix=VOLUME_PREFIX,
    release=CHF_RELEASE_SPEC,
)
ChfCheckpointOperationRejected = CheckpointOperationRejected


def import_chf_checkpoint(
    repository_root: Path,
    archive: Path,
    release_name: str,
    *,
    docker_binary: Path = DOCKER_BINARY,
    runtime_directory: Path | None = None,
) -> CheckpointVolume:
    return import_checkpoint(
        CHF_CHECKPOINT_SPEC,
        repository_root,
        archive,
        release_name,
        docker_binary=docker_binary,
        runtime_directory=runtime_directory,
    )


def recover_chf_checkpoint(
    volume_name: str,
    confirmation: str,
    *,
    repository_root: Path | None = None,
    docker_binary: Path = DOCKER_BINARY,
    runtime_directory: Path | None = None,
) -> None:
    recover_checkpoint(
        CHF_CHECKPOINT_SPEC,
        volume_name,
        confirmation,
        repository_root=repository_root,
        docker_binary=docker_binary,
        runtime_directory=runtime_directory,
    )


def verify_chf_checkpoint(
    repository_root: Path,
    release: ChfCheckpointRelease,
    *,
    docker_binary: Path = DOCKER_BINARY,
    runtime_directory: Path | None = None,
) -> CheckpointVolume:
    return verify_checkpoint_volume(
        CHF_CHECKPOINT_SPEC,
        repository_root,
        release,
        docker_binary=docker_binary,
        runtime_directory=runtime_directory,
    )


def checkpoint_volume_name(checkpoint_sha256: str) -> str:
    return _checkpoint_volume_name(CHF_CHECKPOINT_SPEC, checkpoint_sha256)


def main(argv: list[str] | None = None) -> int:
    return run_checkpoint_cli(CHF_CHECKPOINT_SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
