"""CHF identity over the shared safe checkpoint-release mechanics."""

from __future__ import annotations

from pathlib import Path

from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CONTRACT_ID
from nmrpeak_provider.product import CHF_OFFERING
from nmrpeak_provider.product_result import CHF_RESULT_IDENTITY
from repository_checks.checkpoint_release import (
    CheckpointRelease,
    CheckpointReleaseRejected,
    CheckpointReleaseSpec,
    candidate_release_bytes as _candidate_release_bytes,
    measure_checkpoint_member as _measure_checkpoint_member,
    parse_release_bytes as _parse_release_bytes,
    run_release_cli,
    verify_release_bytes as _verify_release_bytes,
)


SCHEMA_ID = "nmrpeak.checkpoint_release.chf.v1"
RUNNER_REF = CHF_RESULT_IDENTITY.runner_ref
ARCHIVE_MEMBER = (
    "weights/generation/all_weights/"
    "NMRexp_lr3e-4_bs16_gpu8_spec_trans_mol_bart_base_spec_trans_mol_60000_1000000/"
    "CHF/checkpoint_best.pt"
)
CHF_RELEASE_SPEC = CheckpointReleaseSpec(
    lane_name="CHF",
    schema_id=SCHEMA_ID,
    runner_ref=RUNNER_REF,
    runner_contract_id=CHF_RUNNER_CONTRACT_ID,
    analysis_kind_ref=CHF_OFFERING.analysis_kind_ref,
    decode_policy_id=CHF_RESULT_IDENTITY.decode_policy.decode_policy_id,
    archive_member=ARCHIVE_MEMBER,
)
ChfReleaseRejected = CheckpointReleaseRejected
ChfCheckpointRelease = CheckpointRelease


def candidate_release_bytes(
    archive: Path,
    release_name: str,
    *,
    source_revision: str,
) -> bytes:
    return _candidate_release_bytes(
        CHF_RELEASE_SPEC,
        archive,
        release_name,
        source_revision=source_revision,
    )


def verify_release_bytes(
    raw: bytes,
    archive: Path,
    *,
    expected_release_name: str,
    expected_source_revision: str,
) -> ChfCheckpointRelease:
    return _verify_release_bytes(
        CHF_RELEASE_SPEC,
        raw,
        archive,
        expected_release_name=expected_release_name,
        expected_source_revision=expected_source_revision,
    )


def parse_release_bytes(
    raw: bytes,
    *,
    expected_release_name: str,
    expected_source_revision: str,
) -> ChfCheckpointRelease:
    return _parse_release_bytes(
        CHF_RELEASE_SPEC,
        raw,
        expected_release_name=expected_release_name,
        expected_source_revision=expected_source_revision,
    )


def measure_checkpoint_member(archive: Path) -> tuple[int, str]:
    return _measure_checkpoint_member(CHF_RELEASE_SPEC, archive)


def main(argv: list[str] | None = None) -> int:
    return run_release_cli(CHF_RELEASE_SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
