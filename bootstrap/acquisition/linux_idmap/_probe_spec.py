"""One definition of what we create and what we are allowed to remove."""

from dataclasses import dataclass

from ._probe_model import ProbeRequest, StepError
from . import _confinement


# Shared owned-probe label wire name; creation and verification must agree.
# repo-check: allow-module-constant
OWNER_LABEL = "linux-idmap.operation"
# Shared operation-token label wire name; creation and verification must agree.
# repo-check: allow-module-constant
TOKEN_LABEL = "linux-idmap.token"
# Versioned owned-probe protocol identifier.
# repo-check: allow-module-constant
OWNER = "identity-v3"
# Finite inert probe payload; the caller supplies its lifetime and interpreter.
# repo-check: allow-module-constant
PROBE_SCRIPT = "import sys,time; time.sleep(int(sys.argv[1]))"
# Probe lifecycle and confinement evidence.
# repo-check: allow-module-constant
ISOLATION_FIELDS = ("AutoRemove", "RestartPolicy", "NetworkMode", "ReadonlyRootfs", "CapDrop",
                    "SecurityOpt", "Privileged", "PidMode", "CapAdd", "Devices", "DeviceRequests")


@dataclass(frozen=True)
class ProbeSpec:
    token: str
    image_id: str
    request: ProbeRequest

    @property
    def name(self) -> str:
        return "linux-idmap-" + self.token

    @property
    def command(self) -> tuple[str, ...]:
        return ("-I", "-S", "-B", "-c", PROBE_SCRIPT, str(self.request.bounds.lifetime_seconds), self.token)

    def create_arguments(self) -> list[str]:
        request = self.request
        # silent-defaults: allow-contract -- Default namespace mode adds no Docker option.
        namespace = ["--userns=host"] if request.userns_mode == "host" else []
        return ["container", "create", "--pull=never", "--name", self.name,
                "--label", OWNER_LABEL + "=" + OWNER, "--label", TOKEN_LABEL + "=" + self.token,
                "--user", f"{request.user_uid}:{request.user_gid}",
                "--rm", "--restart", "no", "--no-healthcheck",
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", *namespace,
                "--entrypoint", request.interpreter, self.image_id, *self.command]

    def verify(self, container, cid: str) -> None:
        request = self.request
        # silent-defaults: allow-contract -- Docker inspect encodes the explicitly selected default namespace as empty text, and host selection as host; this is wire encoding, not policy fallback.
        expected_userns_mode = "" if request.userns_mode == "default" else "host"
        if (container.cid != cid or container.name != self.name or container.token != self.token
                or container.owner != OWNER or container.image_id != self.image_id
                or container.user != f"{request.user_uid}:{request.user_gid}"
                or container.userns_mode != expected_userns_mode
                or tuple(container.entrypoint) != (request.interpreter,)
                or tuple(container.command) != self.command):
            raise StepError("ownership", "probe ownership or configuration does not match its specification")

        isolation = container.isolation
        if (isolation["AutoRemove"] is not True
                or not isinstance(isolation["RestartPolicy"], dict)
                or isolation["RestartPolicy"].get("Name") not in ("", "no")):
            raise StepError("ownership", "probe cleanup or restart policy changed")
        _confinement.verify(container)
