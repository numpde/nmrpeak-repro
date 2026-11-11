"""Check the inspected probe restrictions."""

from ._probe_model import StepError

# Docker inspect fields required for every probe.
# repo-check: allow-module-constant
_SETTINGS = {
    "NetworkMode": "none",
    "ReadonlyRootfs": True,
    "CapDrop": ["ALL"],
    "SecurityOpt": ["no-new-privileges:true"],
}


def verify(container, *, resources=None) -> None:
    settings = container.isolation
    for field, expected in _SETTINGS.items():
        actual = settings[field]
        if type(actual) is not type(expected) or actual != expected:
            raise StepError("confinement", "probe differs from the required confinement policy")
    if (
        settings["Privileged"] is not False
        or settings["PidMode"] not in (None, "")
        or any(settings[key] not in (None, []) for key in ("CapAdd", "Devices", "DeviceRequests"))
        or container.mounts != []
    ):
        raise StepError("confinement", "probe acquired forbidden privileges, devices or mounts")
    if resources is not None and any(
        type(settings[field]) is not int or settings[field] != expected
        for field, expected in (
            ("Memory", resources.memory_bytes),
            ("MemorySwap", resources.memory_bytes),
            ("PidsLimit", resources.pids_limit),
            ("NanoCpus", resources.nano_cpus),
        )
    ):
        raise StepError("confinement", "probe differs from the selected resource ceilings")
