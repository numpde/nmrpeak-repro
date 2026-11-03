"""Check image retirement ownership and retained deployment protections."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
retirement = runpy.run_path(str(ROOT / "scripts/retire-images.py"))
select = retirement["select_images"]
retained = retirement["retained_images"]
staged = retirement["staged_images"]
Refused = retirement["RetirementRefused"]
NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
I = "sha256:" + "1" * 64
B = "sha256:" + "2" * 64
IMAGE = "sha256:" + "3" * 64


def provider():
    return {
        "Id": IMAGE,
        "Created": (NOW - timedelta(days=2)).isoformat(),
        "Config": {"Labels": {
            "io.numpde.nmrpeak.image.input-id": I,
            "io.numpde.nmrpeak.build.input-id": B,
            "io.numpde.nmrpeak.provider.contract-id": "nmr.provider.http.v1",
        }},
        "RepoTags": ["numpde/nmrpeak-provider:" + I[7:], "numpde/nmrpeak-provider-build:" + B[7:]],
        "RepoDigests": [],
    }


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class RetirementSelectionTests(unittest.TestCase):
    def test_retained_build_receipts_protect_exact_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / ".work/image-builds/build.abcdefgh"
            stage.mkdir(parents=True, mode=0o700)
            stage.parent.chmod(0o700)
            (stage / "base.id").write_text(I + "\n")
            (stage / "image.id").write_text(IMAGE + "\n")
            self.assertEqual(staged(root), {I, IMAGE})
            (stage / "image.id").write_text("incomplete")
            with self.assertRaises(Refused):
                staged(root)

    def test_only_owned_obsolete_images_are_candidates(self):
        image = provider()
        self.assertEqual(select([image], set(), set(), "0" * 64, NOW), [IMAGE])
        self.assertEqual(select([image], {IMAGE}, set(), "0" * 64, NOW), [])
        self.assertEqual(select([image], set(), {I}, "0" * 64, NOW), [])
        image["RepoTags"].append("operator/keep:latest")
        self.assertEqual(select([image], set(), set(), "0" * 64, NOW), [])
        image["RepoTags"].pop()
        image["RepoDigests"] = ["operator/keep@" + IMAGE]
        self.assertEqual(select([image], set(), set(), "0" * 64, NOW), [])
        image["RepoDigests"] = []
        image["Created"] = NOW.isoformat()
        self.assertEqual(select([image], set(), set(), "0" * 64, NOW), [])

    def test_current_test_image_and_malformed_metadata_are_protected(self):
        image = {"Id": IMAGE, "Created": (NOW - timedelta(days=2)).isoformat(),
                 "Config": {"Labels": {"io.numpde.nmrpeak.test-base-key": "a" * 64}},
                 "RepoTags": ["nmrpeak-repro/python-test:" + "a" * 64], "RepoDigests": []}
        self.assertEqual(select([image], set(), set(), "a" * 64, NOW), [])
        self.assertEqual(select([image], set(), set(), "b" * 64, NOW), [IMAGE])
        del image["RepoDigests"]
        with self.assertRaises(Refused):
            select([image], set(), set(), "b" * 64, NOW)

    def exercise_two_aliases(self, after_alias, *, borrower=False, fail_removal=False):
        image = provider()
        image["Created"] = "2020-01-01T00:00:00+00:00"
        calls = []
        current = {str(n): "sha256:" + str(n) * 64 for n in (4, 5, 6)}

        def command(arguments, *, stdout, stderr, **kwargs):
            calls.append(arguments)
            if arguments[0] == "git":
                result = "" if "status" in arguments else "a" * 40 + "\n"
            elif arguments[0].endswith("test-image-key.sh"):
                result = "b" * 64 + "\n"
            elif arguments[3:6] == ["ps", "--all", "--quiet"]:
                result = "f" * 64 + "\n" if borrower and len(image["RepoTags"]) == 1 else ""
            elif arguments[3:6] == ["image", "ls", "--all"]:
                result = IMAGE + "\n" if arguments[-1].endswith("image.input-id") else ""
            elif arguments[3:5] == ["image", "inspect"]:
                result = json.dumps([image])
            elif arguments[3:6] == ["image", "rm", "--no-prune"]:
                target = arguments[-1]
                if fail_removal:
                    return type("Result", (), {"returncode": 1})()
                if target != IMAGE:
                    image["RepoTags"].remove(target)
                    after_alias(image)
                result = ""
            else:
                self.fail(f"Unexpected command: {arguments}")
            stdout.write(result.encode())
            return type("Result", (), {"returncode": 0})()

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(retirement["retire"].__globals__, {"_image_input_ids": lambda root, revision: current}), \
             patch("subprocess.run", side_effect=command):
            if fail_removal:
                with self.assertRaises(Refused):
                    retirement["retire"](Path(directory), "unix:///engine.sock")
            else:
                retirement["retire"](Path(directory), "unix:///engine.sock")
        removals = [item[-1] for item in calls if item[3:6] == ["image", "rm", "--no-prune"]]
        return calls, removals

    def test_two_owned_aliases_are_rechecked_before_final_removal(self):
        calls, removals = self.exercise_two_aliases(lambda image: None)
        self.assertEqual(len(removals), 2)
        self.assertIn(removals[0], provider()["RepoTags"])
        self.assertEqual(removals[1], IMAGE)
        self.assertGreaterEqual(sum(item[3:5] == ["image", "inspect"] for item in calls), 3)

    def test_new_foreign_alias_stops_the_removal(self):
        calls, removals = self.exercise_two_aliases(lambda image: image["RepoTags"].append("operator/keep:latest"))
        self.assertEqual(len(removals), 1)
        self.assertNotIn(IMAGE, removals)

    def test_new_borrower_stops_the_removal(self):
        calls, removals = self.exercise_two_aliases(lambda image: None, borrower=True)
        self.assertEqual(len(removals), 1)
        self.assertNotIn(IMAGE, removals)

    def test_failed_removal_stops_without_further_mutation(self):
        calls, removals = self.exercise_two_aliases(lambda image: None, fail_removal=True)
        self.assertEqual(len(removals), 1)
        self.assertNotIn(IMAGE, removals)

    def test_digest_bound_historical_topology_protects_each_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "secrets/deployments/demo/generations"
            topology = canonical({"schema_id": "nmrpeak.deployment_topology.v1", "services": [
                {"role": role, "image": "sha256:" + digit * 64, "image_input_id": "sha256:" + str(int(digit) + 3) * 64}
                for role, digit in (("provider", "1"), ("hf", "2"), ("chf", "3"))]})
            manifest = canonical({"schema_id": "nmrpeak.frozen_generation.v1", "provider_http_release": "historical",
                                  "files": [{"path": "deployment/topology.json", "byte_length": len(topology),
                                             "sha256": "sha256:" + sha256(topology).hexdigest()}]})
            generation = sha256(b"nmrpeak.frozen_generation.v1\0" + manifest).hexdigest()
            frozen = folder / generation / "frozen"
            (frozen / "deployment").mkdir(parents=True)
            (frozen / "manifest.json").write_bytes(manifest)
            (frozen / "deployment/topology.json").write_bytes(topology)
            images, inputs = retained(root)
            self.assertEqual(len(images), 3)
            self.assertEqual(len(inputs), 3)
            (frozen / "deployment/topology.json").write_bytes(topology + b" ")
            with self.assertRaises(ValueError):
                retained(root)


if __name__ == "__main__":
    unittest.main()
