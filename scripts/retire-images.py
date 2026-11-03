#!/usr/bin/env python3
"""Retire only obsolete checkout-owned images on the selected local engine."""

import fcntl
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from nmrpeak_provider.canonical_json import parse_canonical_json_bytes
from nmrpeak_provider.frozen_generation import frozen_generation_id, _load_files, _read_regular_file, _require_exact_inventory
from deployment.provider_deployment import _image_input_ids

ID = re.compile(r"sha256:[0-9a-f]{64}")
HEX = re.compile(r"[0-9a-f]{64}")
INPUT_LABEL = "io.numpde.nmrpeak.image.input-id"
BUILD_LABEL = "io.numpde.nmrpeak.build.input-id"
TEST_LABEL = "io.numpde.nmrpeak.test-base-key"


class RetirementRefused(RuntimeError):
    pass


def directory(path):
    if path.is_symlink() or not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise RetirementRefused("A retained deployment directory is inaccessible or is a symlink.")


def retained_images(root):
    """Authenticate each historical manifest and protect its complete topology."""
    protected, inputs = set(), set()
    secrets = root / "secrets"
    deployments = secrets / "deployments"
    if not secrets.exists() and not secrets.is_symlink():
        return protected, inputs
    directory(secrets)
    if not deployments.exists() and not deployments.is_symlink():
        return protected, inputs
    directory(deployments)
    for deployment in sorted(deployments.iterdir()):
        directory(deployment)
        generations = deployment / "generations"
        if not generations.exists() and not generations.is_symlink():
            continue
        directory(generations)
        for generation in sorted(generations.iterdir()):
            directory(generation)
            name = generation.name.removeprefix("sha256:")
            if not HEX.fullmatch(name):
                raise RetirementRefused("A retained generation has an unrecognized identity.")
            frozen = generation / "frozen"
            directory(frozen)
            manifest_path = frozen / "manifest.json"
            if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 262144:
                raise RetirementRefused("A retained generation manifest is unavailable.")
            manifest = _read_regular_file(manifest_path, 262144)
            if frozen_generation_id(manifest) != "sha256:" + name:
                raise RetirementRefused("A retained generation manifest identity does not match.")
            document = parse_canonical_json_bytes(manifest)
            if not isinstance(document, dict) or document.get("schema_id") != "nmrpeak.frozen_generation.v1" or not isinstance(document.get("files"), list):
                raise RetirementRefused("A retained generation uses an unrecognized schema.")
            files = _load_files(frozen, document["files"])
            _require_exact_inventory(frozen, files)
            matches = [entry.content for entry in files if entry.path == "deployment/topology.json"]
            if len(matches) != 1:
                raise RetirementRefused("A retained generation has no authenticated deployment topology.")
            topology = parse_canonical_json_bytes(matches[0])
            if not isinstance(topology, dict) or topology.get("schema_id") != "nmrpeak.deployment_topology.v1" or not isinstance(topology.get("services"), list):
                raise RetirementRefused("A retained deployment topology is unrecognized.")
            roles = set()
            for service in topology["services"]:
                if not isinstance(service, dict) or service.get("role") not in {"provider", "hf", "chf"}:
                    raise RetirementRefused("A retained topology has an unrecognized service.")
                role = service["role"]
                image, image_input = service.get("image"), service.get("image_input_id")
                if role in roles or not isinstance(image, str) or not ID.fullmatch(image) or not isinstance(image_input, str) or not ID.fullmatch(image_input):
                    raise RetirementRefused("A retained topology has incomplete image identities.")
                roles.add(role)
                protected.add(image)
                inputs.add(image_input)
            if roles != {"provider", "hf", "chf"}:
                raise RetirementRefused("A retained topology omits a required service.")
    return protected, inputs


def staged_images(root):
    """Protect immutable IDs recorded in preserved build captures."""
    work = root / ".work"
    captures = work / "image-builds"
    if not work.exists() and not work.is_symlink():
        return set()
    directory(work)
    if not captures.exists() and not captures.is_symlink():
        return set()
    directory(captures)
    if captures.stat().st_uid != os.geteuid() or captures.stat().st_mode & 0o077:
        raise RetirementRefused("The image capture directory is not private and owned.")
    protected = set()
    for stage in sorted(captures.iterdir()):
        directory(stage)
        if not re.fullmatch(r"build\.[A-Za-z0-9]{8}", stage.name) or stage.stat().st_uid != os.geteuid():
            raise RetirementRefused("An image capture has an unrecognized owner or name.")
        for name in ("base.id", "image.id"):
            path = stage / name
            if not path.exists() and not path.is_symlink():
                continue
            try:
                value = _read_regular_file(path, 80).decode("ascii").strip()
            except (OSError, UnicodeError, ValueError):
                raise RetirementRefused("An image capture identity is unreadable.") from None
            if not ID.fullmatch(value):
                raise RetirementRefused("An image capture identity is incomplete.")
            protected.add(value)
    return protected


def select_images(images, protected, inputs, test_key, now):
    selected = []
    cutoff = now - timedelta(hours=24)
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("Id"), str) or not ID.fullmatch(image["Id"]):
            raise RetirementRefused("Docker returned an invalid image identity.")
        identifier = image["Id"]
        try:
            created = datetime.fromisoformat(image["Created"])
            if created.utcoffset() is None:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RetirementRefused("Docker returned an invalid image creation time.") from None
        config = image.get("Config")
        if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
            raise RetirementRefused("Docker returned incomplete image labels.")
        labels = config["Labels"]
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
            raise RetirementRefused("Docker returned malformed image labels.")
        tags, digests = image.get("RepoTags"), image.get("RepoDigests")
        if "RepoTags" not in image or "RepoDigests" not in image or any(x is not None and (not isinstance(x, list) or any(not isinstance(i, str) for i in x)) for x in (tags, digests)):
            raise RetirementRefused("Docker returned incomplete image aliases.")
        tags, digests = tags or [], digests or []
        if identifier in protected or digests or created > cutoff:
            continue
        if labels.get(INPUT_LABEL) in inputs:
            continue
        test = labels.get(TEST_LABEL)
        if isinstance(test, str) and HEX.fullmatch(test):
            if test == test_key:
                continue
            known = {"nmrpeak-repro/python-test:" + test}
        else:
            image_input, build = labels.get(INPUT_LABEL), labels.get(BUILD_LABEL)
            if not isinstance(image_input, str) or not ID.fullmatch(image_input) or not isinstance(build, str) or not ID.fullmatch(build):
                continue
            if image_input in inputs:
                continue
            if labels.get("io.numpde.nmrpeak.provider.contract-id") == "nmr.provider.http.v1":
                repository = "numpde/nmrpeak-provider"
            elif labels.get("io.numpde.nmrpeak.runner.ref") == "nmrpeak_hf_v1" and labels.get("io.numpde.nmrpeak.runner.contract-id") == "nmrpeak.runner_session.hf.v1" and labels.get("io.numpde.nmrpeak.runner.target") == "cpu-x86_64":
                repository = "numpde/nmrpeak-hf-runner"
            elif labels.get("io.numpde.nmrpeak.runner.ref") == "nmrpeak_chf_v1" and labels.get("io.numpde.nmrpeak.runner.contract-id") == "nmrpeak.runner_session.chf.v1" and labels.get("io.numpde.nmrpeak.runner.target") == "cpu-x86_64":
                repository = "numpde/nmrpeak-chf-runner"
            else:
                continue
            known = {repository + ":" + image_input[7:], repository + "-build:" + build[7:]}
        if set(tags) <= known:
            selected.append(identifier)
    return selected


def retire(root, host):
    deadline = time.monotonic() + 180

    def run(arguments, description):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetirementRefused("Image retirement reached its deadline.")
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            try:
                result = subprocess.run(arguments, stdout=output, stderr=error, timeout=min(30, remaining), check=False,
                    preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024)))
            except (OSError, subprocess.TimeoutExpired):
                raise RetirementRefused(f"{description} could not be completed; its outcome may be unknown.") from None
            if result.returncode:
                raise RetirementRefused(f"{description} failed with client return code {result.returncode}.")
            output.seek(0)
            raw = output.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise RetirementRefused(f"{description} exceeded the metadata limit.")
            try:
                return raw.decode("utf-8")
            except UnicodeError:
                raise RetirementRefused(f"{description} returned invalid text.") from None

    def docker(description, *arguments):
        return run(["docker", "--host", host, *arguments], description)

    if run(["git", "-C", str(root), "status", "--porcelain"], "Checkout status"):
        raise RetirementRefused("The checkout has uncommitted changes; retain images until its inputs are settled.")
    revision = run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], "Source revision").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RetirementRefused("The committed source revision is invalid.")
    current = set(_image_input_ids(root, revision).values())
    if len(current) != 3 or any(not ID.fullmatch(item) for item in current):
        raise RetirementRefused("The current image input identities are incomplete.")
    key = run([str(root / "scripts/test-image-key.sh"), str(root)], "Test image selection").strip()
    if not HEX.fullmatch(key):
        raise RetirementRefused("The current test image identity is invalid.")
    protected, retained_inputs = retained_images(root)
    protected.update(staged_images(root))
    current.update(retained_inputs)
    containers = docker("Container inventory", "ps", "--all", "--quiet", "--no-trunc").splitlines()
    if len(containers) != len(set(containers)) or any(not HEX.fullmatch(item) for item in containers):
        raise RetirementRefused("The container inventory is incomplete.")
    for offset in range(0, len(containers), 64):
        batch = containers[offset:offset + 64]
        images = docker("Container image inspection", "inspect", "--type", "container", "--format", "{{.Image}}", *batch).splitlines()
        if len(images) != len(batch) or any(not ID.fullmatch(item) for item in images):
            raise RetirementRefused("The container image inventory is incomplete.")
        protected.update(images)
    owned = set()
    for label in (INPUT_LABEL, TEST_LABEL):
        images = docker("Owned image inventory", "image", "ls", "--all", "--quiet", "--no-trunc", "--filter", "label=" + label).splitlines()
        if any(not ID.fullmatch(item) for item in images):
            raise RetirementRefused("The owned image inventory is incomplete.")
        owned.update(images)
    selected = []
    owned = sorted(owned)
    now = datetime.now(timezone.utc)
    for offset in range(0, len(owned), 64):
        batch = owned[offset:offset + 64]
        values = json.loads(docker("Owned image inspection", "image", "inspect", *batch))
        if not isinstance(values, list) or len(values) != len(batch) or any(not isinstance(value, dict) or not isinstance(value.get("Id"), str) for value in values) or {value["Id"] for value in values} != set(batch):
            raise RetirementRefused("The owned image inspection is incomplete.")
        selected.extend(select_images(values, protected, current, key, now))
    removed = 0
    for identifier in selected:
        while True:
            if identifier in staged_images(root):
                break
            if docker("Image borrower recheck", "ps", "--all", "--quiet", "--no-trunc", "--filter", "ancestor=" + identifier).strip():
                break
            values = json.loads(docker("Image alias recheck", "image", "inspect", identifier))
            if not isinstance(values, list) or select_images(values, protected, current, key, now) != [identifier]:
                break
            tags = values[0]["RepoTags"] or []
            if len(tags) > 1:
                docker("Owned image alias removal", "image", "rm", "--no-prune", sorted(tags)[0])
                continue
            docker("Image removal", "image", "rm", "--no-prune", identifier)
            removed += 1
            break
    print(f"Retired {removed} obsolete images; retained deployments and existing containers remain protected.")


def main():
    try:
        host = os.environ.get("NMRPEAK_DOCKER_HOST", "")
        if len(sys.argv) != 1 or not re.fullmatch(r"unix:///[^\s]+", host):
            raise RetirementRefused("Select a local Docker context with make packages/images/retire DOCKER_CONTEXT=<name>.")
        descriptor = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RetirementRefused("Image work is active in this checkout; retry when it finishes.") from None
            retire(ROOT, host)
        finally:
            os.close(descriptor)
        return 0
    except (RetirementRefused, OSError, ValueError) as failure:
        message = str(failure) if isinstance(failure, RetirementRefused) else "Required retirement metadata could not be validated."
        print("Cannot complete image retirement: " + message + " Retain remaining images; earlier removals are not rolled back.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
