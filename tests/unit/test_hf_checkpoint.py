"""Prove HF identity at the shared checkpoint-volume boundary."""

import json
import unittest

from nmrpeak_provider.product_result import HF_RESULT_IDENTITY
from repository_checks.hf_checkpoint import (
    HfCheckpointOperationRejected,
    VOLUME_SCHEMA_ID,
    checkpoint_volume_name,
    recover_hf_checkpoint,
    verify_hf_checkpoint,
)
from repository_checks.hf_release import ARCHIVE_MEMBER, parse_release_bytes
from tests.unit.test_chf_checkpoint import CheckpointOperationFixture


class HfCheckpointOperationTests(unittest.TestCase):
    def test_import_projects_hf_identity_into_volume_name_labels_and_marker(self) -> None:
        with CheckpointOperationFixture("hf") as fixture:
            volume = fixture.import_checkpoint()
            metadata = json.loads(
                (fixture.metadata / f"{volume.name}.json").read_text(encoding="utf-8")
            )
            marker = json.loads(
                (fixture.volume_directory(volume.name) / ".nmrpeak-checkpoint.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(
            volume.name,
            checkpoint_volume_name(volume.checkpoint_sha256),
        )
        self.assertEqual(
            metadata["Labels"]["io.github.numpde.nmrpeak.schema"],
            VOLUME_SCHEMA_ID,
        )
        self.assertEqual(
            metadata["Labels"]["io.github.numpde.nmrpeak.runner"],
            HF_RESULT_IDENTITY.runner_ref,
        )
        self.assertEqual(marker["archive_member"], ARCHIVE_MEMBER)
        self.assertEqual(marker["runner_ref"], HF_RESULT_IDENTITY.runner_ref)

    def test_hf_recovery_rejects_the_chf_volume_namespace_before_engine_io(self) -> None:
        chf_volume = "nmrpeak-chf-checkpoint-" + "a" * 64
        with self.assertRaises(HfCheckpointOperationRejected):
            recover_hf_checkpoint(chf_volume, chf_volume)

    def test_verification_rejects_checkpoint_volume_byte_drift(self) -> None:
        with CheckpointOperationFixture("hf") as fixture:
            volume = fixture.import_checkpoint()
            declaration = (
                fixture.repository
                / f"models/nmrpeak_hf_v1/releases/{fixture.release_name}.json"
            ).read_bytes()
            release = parse_release_bytes(
                declaration,
                expected_release_name=fixture.release_name,
                expected_source_revision="1" * 40,
            )
            self.assertEqual(
                verify_hf_checkpoint(
                    fixture.repository,
                    release,
                    docker_binary=fixture.docker,
                    runtime_directory=fixture.runtime,
                ),
                volume,
            )
            checkpoint = fixture.volume_directory(volume.name) / "checkpoint.pt"
            checkpoint.chmod(0o644)
            checkpoint.write_bytes(b"drift")
            checkpoint.chmod(0o444)
            with self.assertRaises(HfCheckpointOperationRejected):
                verify_hf_checkpoint(
                    fixture.repository,
                    release,
                    docker_binary=fixture.docker,
                    runtime_directory=fixture.runtime,
                )


if __name__ == "__main__":
    unittest.main()
