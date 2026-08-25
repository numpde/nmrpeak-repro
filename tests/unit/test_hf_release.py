"""Prove HF checkpoint identity without repeating shared ZIP safety tests."""

from pathlib import Path
from contextlib import contextmanager
from tempfile import TemporaryDirectory
import unittest
import zipfile

from nmrpeak_provider.canonical_json import canonical_json_bytes, parse_canonical_json_bytes
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CONTRACT_ID
from nmrpeak_provider.product import HF_OFFERING
from nmrpeak_provider.product_result import HF_RESULT_IDENTITY, NMRPEAK_SOURCE_CLOSURE_REF
from repository_checks.hf_release import (
    ARCHIVE_MEMBER,
    HfReleaseRejected,
    candidate_release_bytes,
    parse_release_bytes,
    verify_release_bytes,
)


_CHECKPOINT = b"HF checkpoint fixture bytes"
_SOURCE_REVISION = "1" * 40


class HfReleaseTests(unittest.TestCase):
    def test_declaration_binds_the_exact_hf_product_and_checkpoint(self) -> None:
        with _hf_archive() as archive:
            raw = candidate_release_bytes(
                archive,
                "hf-test-v1",
                source_revision=_SOURCE_REVISION,
            )
            release = verify_release_bytes(
                raw,
                archive,
                expected_release_name="hf-test-v1",
                expected_source_revision=_SOURCE_REVISION,
            )
        document = parse_canonical_json_bytes(raw)
        self.assertEqual(document["analysis_kind_ref"], HF_OFFERING.analysis_kind_ref)
        self.assertEqual(document["runner_ref"], HF_RESULT_IDENTITY.runner_ref)
        self.assertEqual(document["runner_contract_id"], HF_RUNNER_CONTRACT_ID)
        self.assertEqual(
            document["decode_policy_id"],
            HF_RESULT_IDENTITY.decode_policy.decode_policy_id,
        )
        self.assertEqual(document["source_closure_sha256"], NMRPEAK_SOURCE_CLOSURE_REF)
        self.assertEqual(document["checkpoint"]["archive_member"], ARCHIVE_MEMBER)
        self.assertEqual(release.checkpoint_bytes, len(_CHECKPOINT))

    def test_chf_member_cannot_authorize_the_hf_lane(self) -> None:
        chf_member = ARCHIVE_MEMBER.replace("/HF/", "/CHF/")
        with _archive(chf_member) as archive, self.assertRaises(HfReleaseRejected):
            candidate_release_bytes(
                archive,
                "hf-test-v1",
                source_revision=_SOURCE_REVISION,
            )

    def test_lane_identity_drift_is_rejected_without_remeasuring(self) -> None:
        with _hf_archive() as archive:
            raw = candidate_release_bytes(
                archive,
                "hf-test-v1",
                source_revision=_SOURCE_REVISION,
            )
        document = parse_canonical_json_bytes(raw)
        for field in ("analysis_kind_ref", "runner_ref", "runner_contract_id"):
            with self.subTest(field=field), self.assertRaises(HfReleaseRejected):
                parse_release_bytes(
                    canonical_json_bytes(dict(document) | {field: "wrong"}),
                    expected_release_name="hf-test-v1",
                    expected_source_revision=_SOURCE_REVISION,
                )


def _hf_archive():
    return _archive(ARCHIVE_MEMBER)


@contextmanager
def _archive(member: str):
    with TemporaryDirectory() as temporary:
        archive = Path(temporary).resolve() / "weights.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(member, _CHECKPOINT)
        yield archive


if __name__ == "__main__":
    unittest.main()
