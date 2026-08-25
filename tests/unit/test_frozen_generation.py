"""Prove retained generation identity and runtime projection share one manifest."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nmrpeak_provider.chf_runner_protocol import (
    CHF_RUNNER_CODEC,
    CHF_RUNNER_CONTRACT_ID,
)
from nmrpeak_provider.frozen_generation import (
    FrozenFile,
    frozen_generation_id,
    load_frozen_generation,
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
from nmrpeak_provider.run_generation import CreatedAtWindow, RunGenerationIdentity


HF_FACTS = ProviderResultFacts(
    HF_RESULT_IDENTITY,
    HF_RUNNER_CONTRACT_ID,
    "sha256:" + "2" * 64,
    "sha256:" + "3" * 64,
)
CHF_FACTS = ProviderResultFacts(
    CHF_RESULT_IDENTITY,
    CHF_RUNNER_CONTRACT_ID,
    "sha256:" + "4" * 64,
    "sha256:" + "5" * 64,
)
FILES = (
    FrozenFile("hello/hf.txt", b"Structured formula and 1H NMR input."),
    FrozenFile("hello/chf.txt", b"Structured formula, 1H NMR, and 13C NMR input."),
    FrozenFile(
        "deployment/topology.json",
        b'{"schema_id":"nmrpeak.deployment_topology.v1"}',
    ),
)


class FrozenGenerationTests(unittest.TestCase):
    def test_canonical_identity_contains_runtime_facts_but_not_deployment_state(self) -> None:
        manifest = render_frozen_generation_manifest(runtime(), FILES)

        self.assertEqual(manifest, render_frozen_generation_manifest(runtime(), FILES))
        self.assertTrue(frozen_generation_id(manifest).startswith("sha256:"))
        for excluded in (
            b"deployment_name",
            b"api_origin",
            b"credential_ref",
            b"engine_object",
        ):
            self.assertNotIn(excluded, manifest)

    def test_load_rehashes_named_files_and_constructs_both_fixed_lanes(self) -> None:
        manifest = render_frozen_generation_manifest(runtime(), FILES)
        with materialized_generation(manifest, FILES) as root:
            frozen = load_frozen_generation(
                root,
                expected_frozen_generation_id=frozen_generation_id(manifest),
            )

        self.assertEqual(frozen.files, FILES)
        self.assertEqual(
            frozen.runtime.hf.generation.generation_id,
            "nmrpeak-hf-2026-08-24",
        )
        self.assertEqual(frozen.runtime.hf.result_facts, HF_FACTS)
        self.assertEqual(frozen.runtime.chf.result_facts, CHF_FACTS)
        self.assertEqual(
            frozen.runtime.frozen_generation_id,
            frozen.frozen_generation_id,
        )

    def test_manifest_or_named_file_drift_is_rejected(self) -> None:
        manifest = render_frozen_generation_manifest(runtime(), FILES)
        identity = frozen_generation_id(manifest)
        with materialized_generation(manifest, FILES) as root:
            (root / "manifest.json").write_bytes(
                manifest.replace(
                    b"nmrpeak-hf-2026-08-24",
                    b"nmrpeak-hf-2026-08-25",
                )
            )
            with self.assertRaisesRegex(ValueError, "identity does not match"):
                load_frozen_generation(root, expected_frozen_generation_id=identity)

        with materialized_generation(manifest, FILES) as root:
            (root / "hello/hf.txt").write_bytes(b"drift")
            with self.assertRaisesRegex(ValueError, "does not match its manifest"):
                load_frozen_generation(root, expected_frozen_generation_id=identity)

    def test_paths_and_directory_inventory_are_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "normalized and relative"):
            render_frozen_generation_manifest(
                runtime(),
                (FrozenFile("../escape", b"outside"),),
            )

        manifest = render_frozen_generation_manifest(runtime())
        with materialized_generation(manifest, ()) as root:
            (root / "extra.txt").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "inventory does not match"):
                load_frozen_generation(
                    root,
                    expected_frozen_generation_id=frozen_generation_id(manifest),
                )

        files = (FrozenFile("hello/hf.txt", b"outside"),)
        manifest = render_frozen_generation_manifest(runtime(), files)
        with TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "generation"
            outside = parent / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "manifest.json").write_bytes(manifest)
            (outside / "hf.txt").write_bytes(b"outside")
            os.symlink(outside, root / "hello", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "not a retained regular path"):
                load_frozen_generation(
                    root,
                    expected_frozen_generation_id=frozen_generation_id(manifest),
                )


def runtime() -> GenerationRuntime:
    return GenerationRuntime(
        "sha256:" + "1" * 64,
        GenerationLane(
            HF_LIFECYCLE_LANE,
            generation("mol_from_1h_peaks", "nmrpeak-hf-2026-08-24"),
            HF_FACTS,
            HF_RUNNER_CODEC,
        ),
        GenerationLane(
            CHF_LIFECYCLE_LANE,
            generation("mol_from_1h_13c_formula", "nmrpeak-chf-2026-08-24"),
            CHF_FACTS,
            CHF_RUNNER_CODEC,
        ),
    )


def generation(analysis_kind_ref: str, generation_id: str) -> RunGenerationIdentity:
    return RunGenerationIdentity(
        "provider:nmrpeak",
        analysis_kind_ref,
        generation_id,
        CreatedAtWindow(datetime(2026, 8, 24, tzinfo=UTC)),
    )


class materialized_generation:
    def __init__(self, manifest: bytes, files: tuple[FrozenFile, ...]) -> None:
        self.manifest = manifest
        self.files = files
        self.temporary = TemporaryDirectory()

    def __enter__(self) -> Path:
        root = Path(self.temporary.name) / "generation"
        root.mkdir()
        (root / "manifest.json").write_bytes(self.manifest)
        for frozen_file in self.files:
            destination = root / frozen_file.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(frozen_file.content)
        return root

    def __exit__(self, *exc_info: object) -> None:
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
