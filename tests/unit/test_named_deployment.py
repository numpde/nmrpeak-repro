"""Prove named selections render inputs accepted by their runtime consumers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from nmrpeak_provider.frozen_generation import load_frozen_generation
from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from nmrpeak_provider.provider_config import decode_provider_runtime_config
from repository_checks.chf_release import ARCHIVE_MEMBER as CHF_MEMBER
from repository_checks.chf_release import candidate_release_bytes as chf_release_bytes
from repository_checks.hf_release import ARCHIVE_MEMBER as HF_MEMBER
from repository_checks.hf_release import candidate_release_bytes as hf_release_bytes
from repository_checks.named_deployment import (
    NamedDeploymentRejected,
    admit_deployment_releases,
    load_named_deployment,
    render_generation,
)
from repository_checks.deployment_topology import (
    DeploymentCheckpoints,
    DeploymentImages,
    project_deployment_topology,
)
from tests.unit.test_deployment_topology import compose_document
from tests.unit.test_frozen_generation import materialized_generation


ROOT = Path(__file__).parents[2]
SOURCE_REVISION = "1" * 40
HF_IMAGE_INPUT = "sha256:" + "4" * 64
CHF_IMAGE_INPUT = "sha256:" + "6" * 64


class NamedDeploymentTests(unittest.TestCase):
    def test_exact_two_lane_selection_renders_consumer_accepted_inputs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection_path = root / "deployment.toml"
            selection_path.write_text(_selection(), encoding="utf-8")
            selection = load_named_deployment(selection_path)
            archive = root / "weights.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(HF_MEMBER, b"hf checkpoint")
                bundle.writestr(CHF_MEMBER, b"chf checkpoint")
            hf_declaration = hf_release_bytes(
                archive,
                "hf-release",
                source_revision=SOURCE_REVISION,
            )
            chf_declaration = chf_release_bytes(
                archive,
                "chf-release",
                source_revision=SOURCE_REVISION,
            )
            checkpoints = DeploymentCheckpoints(
                parse_canonical_json_bytes(hf_declaration)["checkpoint"]["sha256"],
                parse_canonical_json_bytes(chf_declaration)["checkpoint"]["sha256"],
            )
            compose = compose_document()
            compose["services"]["hf-runner"]["command"][1] = checkpoints.hf
            compose["services"]["chf-runner"]["command"][1] = checkpoints.chf
            rendered = render_generation(
                selection,
                admit_deployment_releases(
                    selection,
                    hf_release_declaration=hf_declaration,
                    chf_release_declaration=chf_declaration,
                    upstream_revision=SOURCE_REVISION,
                ),
                provider_config_template=(ROOT / "config/provider.toml.example").read_bytes(),
                hf_image_input_id=HF_IMAGE_INPUT,
                chf_image_input_id=CHF_IMAGE_INPUT,
                hf_hello=b"HF description",
                chf_hello=b"CHF description",
                topology=project_deployment_topology(
                    compose,
                    DeploymentImages(
                        "sha256:" + "1" * 64,
                        "sha256:" + "2" * 64,
                        "sha256:" + "3" * 64,
                        "sha256:" + "4" * 64,
                        "sha256:" + "5" * 64,
                        "sha256:" + "6" * 64,
                    ),
                    checkpoints,
                ),
            )

        configured = decode_provider_runtime_config(rendered.provider_config)
        self.assertEqual(configured.frozen_generation_id, rendered.frozen_generation_id)
        with materialized_generation(rendered.manifest, rendered.files) as generation:
            loaded = load_frozen_generation(
                generation,
                expected_frozen_generation_id=rendered.frozen_generation_id,
            )
        self.assertEqual(loaded.runtime.hf.generation.generation_id, "hf-generation")
        self.assertEqual(loaded.runtime.chf.generation.generation_id, "chf-generation")

    def test_selection_is_closed_and_never_infers_a_target_or_lane(self) -> None:
        invalid = (
            _selection().replace('target = "cpu-x86_64"\n', "", 1),
            _selection().replace('target = "cpu-x86_64"', 'target = "cuda"', 1),
            _selection().replace(
                '[implementations.chf]\n',
                '[implementations.extra]\ntarget = "cpu-x86_64"\n'
                'release = "x"\n\n[implementations.chf]\n',
            ),
        )
        for content in invalid:
            with self.subTest(content=content), TemporaryDirectory() as temporary:
                path = Path(temporary) / "deployment.toml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(NamedDeploymentRejected):
                    load_named_deployment(path)


def _selection() -> str:
    return '''schema_id = "nmrpeak.named_deployment.v1"
provider_ref = "provider:nmrpeak"

[implementations.hf]
target = "cpu-x86_64"
release = "hf-release"

[implementations.hf.run_generation]
generation_id = "hf-generation"
not_before = "2026-08-25T00:00:00Z"

[implementations.chf]
target = "cpu-x86_64"
release = "chf-release"

[implementations.chf.run_generation]
generation_id = "chf-generation"
not_before = "2026-08-25T00:00:00Z"
'''


if __name__ == "__main__":
    unittest.main()
    admit_deployment_releases,
