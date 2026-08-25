"""Prove image resolution returns only a role- and input-matched local image."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deployment.local_image import (
    LocalImageRejected,
    LocalImageSpec,
    resolve_local_image,
)


INPUT_ID = "sha256:" + "1" * 64
IMAGE_ID = "sha256:" + "2" * 64
SPEC = LocalImageSpec(
    "numpde/test",
    INPUT_ID,
    (("role", "provider"),),
    ("python", "-m", "provider"),
)


class LocalImageTests(unittest.TestCase):
    def test_resolves_exact_runtime_and_label_identity(self) -> None:
        with fake_docker(image_document()) as docker:
            image = resolve_local_image(docker, SPEC)

        self.assertEqual(image.image_id, IMAGE_ID)
        self.assertEqual(image.input_id, INPUT_ID)

    def test_rejects_input_label_and_runtime_drift(self) -> None:
        changed_label = image_document()
        changed_label["Config"]["Labels"]["io.numpde.nmrpeak.image.input-id"] = (
            "sha256:" + "3" * 64
        )
        changed_user = image_document()
        changed_user["Config"]["User"] = "0:0"
        for document in (changed_label, changed_user):
            with self.subTest(document=document), fake_docker(document) as docker:
                with self.assertRaises(LocalImageRejected):
                    resolve_local_image(docker, SPEC)


def image_document() -> dict[str, object]:
    return {
        "Id": IMAGE_ID,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "User": "65532:65532",
            "Entrypoint": ["python", "-m", "provider"],
            "Labels": {
                "role": "provider",
                "io.numpde.nmrpeak.image.input-id": INPUT_ID,
            },
        },
    }


class fake_docker:
    def __init__(self, document: dict[str, object]) -> None:
        self.document = document
        self.temporary = TemporaryDirectory()

    def __enter__(self) -> Path:
        executable = Path(self.temporary.name) / "docker"
        executable.write_text(
            "#!/bin/sh\nprintf '%s' '" + json.dumps([self.document]) + "'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def __exit__(self, *exc_info: object) -> None:
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
