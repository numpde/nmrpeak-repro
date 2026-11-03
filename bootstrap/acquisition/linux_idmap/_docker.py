"""Resolve images and manage probe containers on one selected Docker engine."""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
import re
import stat
import tempfile
from typing import Mapping

from ._files import FileIdentity
from ._command import DockerError, _command_failure, _run_command, _deadline, _remaining
from ._probe_model import Cancellation
from ._probe_spec import OWNER_LABEL, TOKEN_LABEL, ISOLATION_FIELDS


class DockerEngine:
    """Selected socket and daemon identity retained for one complete operation."""

    def __init__(self, identity: EngineIdentity, directory: tempfile.TemporaryDirectory,
                 executable: str, environment: Mapping[str, str]):
        self.identity = identity
        self._directory = directory
        self._executable = executable
        self._environment = dict(environment)
        self._closed = False

    @classmethod
    # silent-defaults: allow-contract -- Use the Docker CLI transport unless the caller supplies a test executable; never choose a Docker endpoint implicitly; tests/test_docker.py::EngineTests.
    def connect(cls, endpoint: str, *, timeout: float, cancel: Cancellation | None = None,
                executable: str = "docker") -> DockerEngine:
        """Connect to the selected Unix socket and record its daemon identity."""
        deadline = _deadline(timeout)
        if not _text(endpoint) or not endpoint.startswith("unix:///") or "?" in endpoint or "#" in endpoint:
            raise DockerError("unsupported_engine", "The selected Docker endpoint must be a local Unix socket.")
        # Docker trims its host option; do not stat one path and dial another.
        if endpoint != endpoint.strip():
            raise DockerError("unsupported_engine", "The selected Docker endpoint must not have surrounding whitespace.")
        # Keep the caller's absolute path, including symlinks: subsequent
        # caller commands use this spelling, so every guard must follow it.
        socket_path = endpoint[7:]
        socket = _socket_identity(socket_path)
        environment = _clean_environment(os.environ)
        directory = tempfile.TemporaryDirectory(prefix="linux-idmap-client-")
        try:
            argv = [executable, "--config", directory.name, "--host", endpoint, "info", "--format", INFO_FORMAT]
            info = EngineInfo.of(_run_command(argv, env=environment, timeout=_remaining(deadline), cancel=cancel))
            if socket != _socket_identity(socket_path):
                raise DockerError("engine_changed", "The selected Docker socket changed while resolving its identity.")
            identity = EngineIdentity(endpoint, socket, info)
            return cls(identity, directory, executable, environment)
        except BaseException as operation:
            try:
                directory.cleanup()
            except BaseException as cleanup:
                raise _command_failure(operation, cleanup, mutation_uncertain=False) from operation
            raise

    def image_id(self, image: str, *, timeout: float, cancel: Cancellation | None = None) -> str:
        if not _text(image, 512) or not image or image.startswith("-"):
            raise DockerError("invalid_policy", "An explicit approved local image reference is required.")
        value = _json(self._call(["image", "inspect", "--format", "[{{json .ID}},{{json .Config.Volumes}}]", image], timeout=timeout, cancel=cancel))
        if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or IMAGE_PATTERN.fullmatch(value[0]) is None:
            raise DockerError("invalid_response", "The approved local image did not resolve to an immutable image ID.")
        if value[1] not in (None, {}):
            raise DockerError("invalid_policy", "The approved probe image must not declare implicit volumes.")
        return value[0]

    def create(self, arguments: list[str], *, timeout: float,
               cancel: Cancellation | None = None) -> str:
        value = self._call(arguments, timeout=timeout, cancel=cancel, mutation=True).strip()
        try:
            _cid(value)
        except DockerError as exc:
            exc.mutation_uncertain = True
            raise
        return value

    def start(self, cid: str, *, timeout: float, cancel: Cancellation | None = None) -> None:
        _cid(cid)
        self._call(["container", "start", cid], timeout=timeout, cancel=cancel, mutation=True)

    def inspect(self, cid: str, *, timeout: float, cancel: Cancellation | None = None) -> Container | None:
        _cid(cid)
        deadline = _deadline(timeout)
        try:
            result = self._call(["container", "inspect", "--format", INSPECT_FORMAT, cid],
                                timeout=_remaining(deadline), cancel=cancel)
        except DockerError as error:
            if error.category != "docker_command":
                raise
            # A nonzero inspect is not proof of absence. Confirm it by a
            # successful exact-ID inventory, without parsing localized stderr.
            # Existing containers need only the inspect and its engine guards.
            values = self._list("id=" + cid, timeout=_remaining(deadline), cancel=cancel)
            if not values:
                return None
            if values != (cid,):
                raise DockerError("identity_mismatch", "Docker exact-ID listing returned another container.") from error
            raise
        return Container.from_json(result, cid)

    def remove(self, cid: str, *, timeout: float, cancel: Cancellation | None = None) -> None:
        _cid(cid)
        # The owning probe verifies this exact container ID before removal.
        self._call(["container", "rm", "--force", cid], timeout=timeout, cancel=cancel, mutation=True)

    def candidates(self, token: str, *, timeout: float, cancel: Cancellation | None = None) -> tuple[str, ...]:
        _token(token)
        return self._list("label=" + TOKEN_LABEL + "=" + token, timeout=timeout, cancel=cancel)

    def __enter__(self) -> DockerEngine:
        return self

    def __exit__(self, _kind, operation, _traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if operation is not None:
                raise _command_failure(operation, cleanup, mutation_uncertain=True) from operation
            raise

    def close(self) -> None:
        self._directory.cleanup()
        self._closed = True

    def _argv(self, args: list[str]) -> list[str]:
        if self._closed:
            raise DockerError("invalid_policy", "This Docker engine operation has already closed.")
        return [self._executable, "--config", self._directory.name, "--host", self.identity.endpoint, *args]

    def _socket_check(self) -> None:
        if _socket_identity(self.identity.endpoint[7:]) != self.identity.socket:
            raise DockerError("engine_changed", "The selected Docker socket identity changed.")

    def assert_unchanged(self, *, timeout: float, cancel: Cancellation | None = None) -> None:
        deadline = _deadline(timeout)
        self._socket_check()
        value = EngineInfo.of(_run_command(self._argv(["info", "--format", INFO_FORMAT]),
                                         env=self._environment, timeout=_remaining(deadline), cancel=cancel))
        if value != self.identity.info:
            raise DockerError("engine_changed", "The selected Docker daemon identity or namespace configuration changed.")
        self._socket_check()

    # silent-defaults: allow-contract -- Transport queries are nonmutating; create/start/remove explicitly identify mutation uncertainty; tests/test_docker.py::CommandTests.
    def _call(self, args: list[str], *, timeout: float, cancel: Cancellation | None = None,
              mutation: bool = False) -> str:
        deadline = _deadline(timeout)
        self.assert_unchanged(timeout=_remaining(deadline), cancel=cancel)
        completed = False
        try:
            result = _run_command(self._argv(args), env=self._environment, timeout=_remaining(deadline),
                                  cancel=cancel, mutation=mutation)
            completed = True
            self.assert_unchanged(timeout=_remaining(deadline), cancel=cancel)
            return result
        except (DockerError, BaseExceptionGroup) as exc:
            if completed and mutation:
                if isinstance(exc, BaseExceptionGroup):
                    raise _command_failure(exc, mutation_uncertain=True) from exc
                exc.mutation_uncertain = True
            raise

    def _list(self, filter_value: str, *, timeout: float, cancel: Cancellation | None = None) -> tuple[str, ...]:
        result = self._call(["container", "ls", "--all", "--no-trunc", "--quiet", "--filter", filter_value],
                            timeout=timeout, cancel=cancel)
        values = result.splitlines()
        if len(values) > 64 or len(set(values)) != len(values):
            raise DockerError("invalid_response", "Docker candidate listing exceeded the identity bound.")
        for value in values:
            _cid(value)
        return tuple(values)


# Docker protocol admits only full container IDs.
# repo-check: allow-module-constant
CID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
# Owned label grammar accepts supported token wire lengths.
# repo-check: allow-module-constant
TOKEN_PATTERN = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{64})\Z")
# Resolved Docker image identity is a full sha256 digest.
# repo-check: allow-module-constant
IMAGE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
# Fixed minimal Docker evidence projection; raw engine output never becomes diagnostics.
# repo-check: allow-module-constant
INFO_FORMAT = ('{"daemon_id":{{json .ID}},"security_options":{{json .SecurityOptions}},'
               '"root_dir":{{json .DockerRootDir}},"driver":{{json .Driver}},"os_type":{{json .OSType}}}')
# Fixed ownership/isolation evidence projection shared with ProbeSpec.verify.
# repo-check: allow-module-constant
INSPECT_FORMAT = (
    '{"cid":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
    '"token":{{json (index .Config.Labels "' + TOKEN_LABEL + '")}},'
    '"owner":{{json (index .Config.Labels "' + OWNER_LABEL + '")}},'
    '"pid":{{json .State.Pid}},"running":{{json .State.Running}},'
    '"started_at":{{json .State.StartedAt}},'
    '"user":{{json .Config.User}},"userns_mode":{{json .HostConfig.UsernsMode}},'
    '"entrypoint":{{json .Config.Entrypoint}},"command":{{json .Config.Cmd}},'
    '"mounts":{{json .Mounts}},"isolation":{' +
    ",".join('"' + field + '":{{json .HostConfig.' + field + '}}' for field in ISOLATION_FIELDS) + '}}'
)


def _clean_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in source.items()
            if not key.startswith(("DOCKER_", "COMPOSE_", "BUILDX_"))
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}


def _json(text: str):
    def object_pairs(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Duplicate structured field")
        return value
    try:
        return json.loads(text, object_pairs_hook=object_pairs)
    except (ValueError, TypeError, RecursionError) as exc:
        raise DockerError("invalid_response", "Docker returned an invalid structured response.") from exc


# silent-defaults: allow-contract -- Docker evidence text has a fixed 4096-character protocol bound; callers can supply a narrower bound.
def _text(value, maximum=4096) -> bool:
    return isinstance(value, str) and len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _cid(value: str) -> None:
    if not isinstance(value, str) or CID_PATTERN.fullmatch(value) is None:
        raise DockerError("invalid_identity", "An exact full Docker container ID is required.")


def _token(value: str) -> None:
    if not isinstance(value, str) or TOKEN_PATTERN.fullmatch(value) is None:
        raise DockerError("invalid_identity", "A random 128-bit or 256-bit hexadecimal operation token is required.")


def _socket_identity(path: str) -> FileIdentity:
    try:
        info = os.stat(path)
    except OSError as exc:
        raise DockerError("engine_changed", "The selected local Docker socket is unavailable.") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise DockerError("unsupported_engine", "The selected Docker endpoint is not a Unix socket.")
    return FileIdentity.of(info)


@dataclass(frozen=True)
class EngineInfo:
    """The daemon settings that must stay fixed throughout a probe operation."""

    daemon_id: str
    security_options: tuple[str, ...]
    root_dir: str
    driver: str
    os_type: str

    @classmethod
    def of(cls, text: str) -> EngineInfo:
        value = _json(text)
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
        if any(not _text(value[key]) or not value[key]
               for key in ("daemon_id", "root_dir", "driver", "os_type")):
            raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
        # silent-defaults: allow-contract -- Moby emits null for a nil SecurityOptions slice; null and [] both mean no entries. See test_docker.py::EngineTests.test_null_and_empty_security_options_are_equivalent_but_change_is_not.
        options = [] if value["security_options"] is None else value["security_options"]
        if (not isinstance(options, list) or len(options) > 32
                or not all(_text(option, 512) for option in options)):
            raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
        if value["os_type"] != "linux":
            raise DockerError("unsupported_engine", "Only local Linux Docker engines are supported.")
        value["security_options"] = tuple(sorted(options))
        return cls(**value)


@dataclass(frozen=True)
class EngineIdentity:
    endpoint: str
    socket: FileIdentity
    info: EngineInfo


@dataclass(frozen=True)
class Container:
    cid: str
    name: str
    image_id: str
    token: str | None
    owner: str | None
    pid: int
    running: bool
    started_at: str
    user: str
    userns_mode: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    mounts: list
    isolation: dict

    @classmethod
    def from_json(cls, text: str, cid: str) -> Container:
        """Parse the complete projected record before it can serve as evidence."""
        value = _json(text)
        keys = {field.name for field in fields(cls)}
        if not isinstance(value, dict) or set(value) != keys:
            raise DockerError("invalid_response", "Docker returned invalid container evidence.")
        if (not isinstance(value["mounts"], list) or not isinstance(value["isolation"], dict)
                or set(value["isolation"]) != set(ISOLATION_FIELDS)):
            raise DockerError("invalid_response", "Docker returned invalid isolation evidence.")
        text_fields = ("cid", "name", "image_id", "started_at", "user", "userns_mode")
        if any(not _text(value[key]) for key in text_fields):
            raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        if any(value[key] is not None and not _text(value[key]) for key in ("token", "owner")):
            raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        if type(value["pid"]) is not int or value["pid"] < 0 or type(value["running"]) is not bool:
            raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        for field in ("entrypoint", "command"):
            arguments = value[field]
            if (not isinstance(arguments, list) or len(arguments) > 16
                    or any(not _text(item) for item in arguments)):
                raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        if value["cid"] != cid:
            raise DockerError("identity_mismatch", "Docker inspection returned another container.")
        value["name"] = value["name"].removeprefix("/")
        value["entrypoint"] = tuple(value["entrypoint"])
        value["command"] = tuple(value["command"])
        return cls(**value)
