"""Prove stable run-generation and logical provider Attempt identities."""

from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
import unittest

from nmrpeak_provider.attempt_identity import derive_provider_attempt_key
from nmrpeak_provider.canonical_json import canonical_json_bytes
from nmrpeak_provider.run_generation import (
    CreatedAtWindow,
    RunGenerationIdentity,
    parse_canonical_utc_timestamp,
    run_generation_fingerprint,
    run_generation_material,
)


VECTOR_ROOT = Path(__file__).parents[2] / "contracts/vectors"


def load_vectors(name: str) -> dict[str, object]:
    return json.loads((VECTOR_ROOT / name).read_text(encoding="utf-8"))


def generation_from_material(material: dict[str, object]) -> RunGenerationIdentity:
    if material["v"] != 1:
        raise ValueError("unsupported test vector version")
    scope = material["scope"]
    if scope["kind"] != "created_at_window":
        raise ValueError("unsupported test vector scope")
    return RunGenerationIdentity(
        provider_ref=material["provider_ref"],
        analysis_kind_ref=material["analysis_kind_ref"],
        generation_id=material["generation_id"],
        scope=CreatedAtWindow(
            not_before=parse_canonical_utc_timestamp(scope["not_before"]),
            not_after=(
                parse_canonical_utc_timestamp(scope["not_after"])
                if scope["not_after"] is not None
                else None
            ),
        ),
    )


class RunIdentityTests(unittest.TestCase):
    def test_run_generation_vectors_are_stable(self) -> None:
        vectors = load_vectors("run_generations.v1.json")
        self.assertEqual("nmrpeak.run_generation.vectors.v1", vectors["schema_id"])
        for vector in vectors["positive"]:
            with self.subTest(name=vector["name"]):
                material = vector["material"]
                generation = generation_from_material(material)
                canonical = canonical_json_bytes(material)
                digest = sha256(b"nmrpeak.run_generation.v1\0" + canonical)
                self.assertEqual(material, run_generation_material(generation))
                self.assertEqual(vector["canonical_material"], canonical.decode())
                self.assertEqual(vector["digest_hex"], digest.hexdigest())
                self.assertEqual(
                    vector["fingerprint"],
                    run_generation_fingerprint(generation),
                )

    def test_malformed_run_generation_vectors_are_rejected(self) -> None:
        vectors = load_vectors("run_generations.v1.json")
        base = vectors["positive"][0]["material"]
        for vector in vectors["malformed"]:
            material = json.loads(json.dumps(base))
            field = vector["field"]
            if field in {"not_before", "not_after"}:
                material["scope"][field] = vector["value"]
            else:
                material[field] = vector["value"]
            with self.subTest(field=vector["field"], value=vector["value"]):
                with self.assertRaises(ValueError):
                    generation_from_material(material)

    def test_created_at_window_has_inclusive_start_and_exclusive_end(self) -> None:
        vectors = load_vectors("run_generations.v1.json")
        generation = generation_from_material(vectors["positive"][1]["material"])

        self.assertTrue(generation.scope.contains(generation.scope.not_before))
        self.assertTrue(
            generation.scope.contains(
                parse_canonical_utc_timestamp("2026-08-31T23:59:59Z")
            )
        )
        self.assertFalse(
            generation.scope.contains(
                parse_canonical_utc_timestamp("2026-08-23T23:59:59Z")
            )
        )
        self.assertFalse(generation.scope.contains(generation.scope.not_after))

    def test_generation_id_boundaries_are_explicit(self) -> None:
        base = generation_from_material(
            load_vectors("run_generations.v1.json")["positive"][0]["material"]
        )
        for generation_id in ("a", "a" + ".-_a" * 15 + "-bc"):
            with self.subTest(length=len(generation_id)):
                identity = RunGenerationIdentity(
                    provider_ref=base.provider_ref,
                    analysis_kind_ref=base.analysis_kind_ref,
                    generation_id=generation_id,
                    scope=base.scope,
                )
                self.assertEqual(generation_id, identity.generation_id)

    def test_run_fingerprint_changes_with_each_owned_fact(self) -> None:
        base = generation_from_material(
            load_vectors("run_generations.v1.json")["positive"][0]["material"]
        )
        original = run_generation_fingerprint(base)
        alternatives = (
            RunGenerationIdentity(
                "provider:nmrpeak-other",
                base.analysis_kind_ref,
                base.generation_id,
                base.scope,
            ),
            RunGenerationIdentity(
                base.provider_ref,
                "mol_from_1h_13c_formula",
                base.generation_id,
                base.scope,
            ),
            RunGenerationIdentity(
                base.provider_ref,
                base.analysis_kind_ref,
                "another-generation",
                base.scope,
            ),
            RunGenerationIdentity(
                base.provider_ref,
                base.analysis_kind_ref,
                base.generation_id,
                CreatedAtWindow(datetime(2026, 8, 25, tzinfo=UTC)),
            ),
            RunGenerationIdentity(
                base.provider_ref,
                base.analysis_kind_ref,
                base.generation_id,
                CreatedAtWindow(
                    base.scope.not_before,
                    base.scope.not_before + timedelta(days=1),
                ),
            ),
        )
        for alternative in alternatives:
            with self.subTest(identity=alternative):
                self.assertNotEqual(
                    original,
                    run_generation_fingerprint(alternative),
                )

    def test_windows_require_exact_utc_datetimes(self) -> None:
        utc_value = datetime(2026, 8, 24, tzinfo=UTC)
        non_utc = datetime(
            2026,
            8,
            24,
            tzinfo=timezone(timedelta(hours=1)),
        )
        with self.assertRaises(TypeError):
            CreatedAtWindow("2026-08-24T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "UTC timezone"):
            CreatedAtWindow(non_utc)
        with self.assertRaisesRegex(ValueError, "later than"):
            CreatedAtWindow(utc_value, utc_value)

    def test_reference_types_and_analysis_kind_length_are_exact(self) -> None:
        class StringSubclass(str):
            pass

        with self.assertRaises(TypeError):
            derive_provider_attempt_key(
                provider_ref=StringSubclass("provider:nmrpeak-test"),
                run_generation_fingerprint="sha256:" + "a" * 64,
                job_ref="job:example",
                input_fingerprint="sha256:" + "b" * 64,
            )
        with self.assertRaisesRegex(ValueError, "exceeds 128"):
            RunGenerationIdentity(
                provider_ref="provider:nmrpeak-test",
                analysis_kind_ref="a" + "_a" * 64,
                generation_id="generation-1",
                scope=CreatedAtWindow(datetime(2026, 8, 24, tzinfo=UTC)),
            )

    def test_attempt_key_vectors_are_stable(self) -> None:
        vectors = load_vectors("attempt_keys.v1.json")
        self.assertEqual(
            "nmrpeak.provider_attempt_key.vectors.v1",
            vectors["schema_id"],
        )
        for vector in vectors["positive"]:
            with self.subTest(name=vector["name"]):
                material = vector["material"]
                canonical = canonical_json_bytes(material)
                digest = sha256(b"nmrpeak.provider_attempt.v1\0" + canonical)
                self.assertEqual(
                    vector["canonical_material"],
                    canonical.decode(),
                )
                self.assertEqual(vector["digest_hex"], digest.hexdigest())
                self.assertEqual(
                    vector["provider_attempt_key"],
                    derive_provider_attempt_key(
                        **{key: value for key, value in material.items() if key != "v"}
                    ),
                )

    def test_malformed_attempt_key_vectors_are_rejected(self) -> None:
        vectors = load_vectors("attempt_keys.v1.json")
        base = dict(vectors["positive"][0]["material"])
        base.pop("v")
        for vector in vectors["malformed"]:
            material = {**base, vector["field"]: vector["value"]}
            with self.subTest(field=vector["field"], value=vector["value"]):
                with self.assertRaises(ValueError):
                    derive_provider_attempt_key(**material)

    def test_attempt_key_changes_with_each_owned_fact(self) -> None:
        vectors = load_vectors("attempt_keys.v1.json")
        material = dict(vectors["positive"][0]["material"])
        material.pop("v")
        original = derive_provider_attempt_key(**material)
        alternatives = {
            "provider_ref": "provider:nmrpeak-other",
            "run_generation_fingerprint": "sha256:" + "c" * 64,
            "job_ref": "job:example-other",
            "input_fingerprint": "sha256:" + "d" * 64,
        }
        for field, value in alternatives.items():
            with self.subTest(field=field):
                self.assertNotEqual(
                    original,
                    derive_provider_attempt_key(**{**material, field: value}),
                )


if __name__ == "__main__":
    unittest.main()
