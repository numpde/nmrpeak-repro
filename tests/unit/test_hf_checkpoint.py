"""Prove HF identity at the shared checkpoint-volume boundary."""

import json
import unittest

from nmrpeak_provider.product_result import HF_RESULT_IDENTITY
from repository_checks.hf_checkpoint import (
    HfCheckpointOperationRejected,
    VOLUME_SCHEMA_ID,
    checkpoint_volume_name,
    recover_hf_checkpoint,
)
from repository_checks.hf_release import ARCHIVE_MEMBER
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


if __name__ == "__main__":
    unittest.main()
