"""Decode the small mutable transport and timing policy for one provider process."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import tomllib

from .attempt_journal import validate_frozen_generation_id
from .attempt_lifecycle import ObservationPolicy
from .canonical_json import canonical_json_bytes
from .provider_https import (
    ProviderHttpsEndpoint,
    validate_provider_https_endpoint_config,
)
from .provider_process import ProviderProcessPolicy
from .runner_session import RunnerDeadlines


SCHEMA_ID = "nmrpeak.provider.runtime_config.v1"
CONFIG_PATH = Path("/run/config/nmrpeak-provider/provider.toml")
CA_PATH = Path("/run/config/nmrpeak-provider/server-a-ca.crt")
CREDENTIAL_PATH = Path("/run/secrets/nmrpeak-provider/signing.private.json")
FROZEN_ROOT = Path("/run/nmrpeak-provider/frozen")
IDENTITY_LOCK_PATH = Path("/run/nmrpeak-provider-lock/provider.lock")
JOURNAL_PATH = Path("/var/lib/nmrpeak-provider/journal")
JOURNAL_MAXIMUM_RECORDS = 10_000
HF_SOCKET_PATH = "/run/nmrpeak-provider/hf/session.sock"
CHF_SOCKET_PATH = "/run/nmrpeak-provider/chf/session.sock"


@dataclass(frozen=True, slots=True)
class ProviderEndpointConfig:
    """Validated Server A facts that do not acquire TLS trust material."""

    origin: str
    expected_topology: str
    connect_timeout_seconds: float
    io_deadline_seconds: float
    ca_file: Path | None

    def __post_init__(self) -> None:
        validate_provider_https_endpoint_config(
            self.origin,
            self.expected_topology,
            self.connect_timeout_seconds,
            self.io_deadline_seconds,
        )

    def materialize(self) -> ProviderHttpsEndpoint:
        """Load configured TLS trust at the runtime transport boundary."""

        return ProviderHttpsEndpoint(
            origin=self.origin,
            expected_topology=self.expected_topology,
            connect_timeout_seconds=self.connect_timeout_seconds,
            io_deadline_seconds=self.io_deadline_seconds,
            ca_file=self.ca_file,
        )


@dataclass(frozen=True, slots=True)
class ProviderRuntimeConfig:
    """Validated mutable facts that remain outside frozen execution identity."""

    frozen_generation_id: str
    endpoint: ProviderEndpointConfig
    journal_maximum_records: int
    journal_filesystem_reserve_bytes: int
    process: ProviderProcessPolicy
    runner: RunnerDeadlines


def server_a_authority_id(endpoint: ProviderEndpointConfig) -> str:
    """Identify the Server A namespace to which durable Attempts belong."""

    if type(endpoint) is not ProviderEndpointConfig:
        raise TypeError("Server A authority identity requires endpoint config")
    if endpoint.ca_file is not None:
        raise ValueError(
            "Server A authority identity requires the public trust configuration"
        )
    material = canonical_json_bytes(
        {
            "origin": endpoint.origin,
            "topology": endpoint.expected_topology,
        }
    )
    return "sha256:" + sha256(b"nmrpeak.server_a_authority.v1\0" + material).hexdigest()


def decode_provider_runtime_config(raw: bytes) -> ProviderRuntimeConfig:
    """Decode one closed TOML document into existing boundary-owned values."""

    if type(raw) is not bytes or len(raw) > 65_536:
        raise ValueError("Provider runtime config must be bounded bytes")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("Provider runtime config is not valid TOML") from None
    _fields(
        "top level",
        document,
        {"frozen_generation_id", "journal", "process", "runner", "schema_id", "server_a"},
    )
    if document["schema_id"] != SCHEMA_ID:
        raise ValueError("Provider runtime config schema is unsupported")
    frozen_id = document["frozen_generation_id"]
    validate_frozen_generation_id(frozen_id)
    server = _table(
        document,
        "server_a",
        {"connect_timeout_seconds", "io_deadline_seconds", "origin", "topology"},
        {"use_private_ca"},
    )
    use_private_ca = server.get("use_private_ca", False)
    if type(use_private_ca) is not bool:
        raise ValueError("Provider runtime private-CA selection must be a boolean")
    endpoint = ProviderEndpointConfig(
        origin=server["origin"],
        expected_topology=server["topology"],
        connect_timeout_seconds=server["connect_timeout_seconds"],
        io_deadline_seconds=server["io_deadline_seconds"],
        ca_file=CA_PATH if use_private_ca else None,
    )
    journal = _table(
        document,
        "journal",
        {"filesystem_reserve_bytes", "maximum_records"},
    )
    maximum_records = journal["maximum_records"]
    reserve = journal["filesystem_reserve_bytes"]
    if (
        type(maximum_records) is not int
        or maximum_records < 1
        or maximum_records > JOURNAL_MAXIMUM_RECORDS
    ):
        raise ValueError(
            "Provider runtime journal record bound must be between 1 and 10000"
        )
    if type(reserve) is not int or reserve < 0:
        raise ValueError("Provider runtime journal reserve cannot be negative")
    process = _table(
        document,
        "process",
        {
            "feed_interval_seconds",
            "forced_join_seconds",
            "hello_interval_seconds",
            "inventory_maximum_pages",
            "maximum_consecutive_unavailable",
            "observation_interval_seconds",
            "observation_maximum_gap_seconds",
            "shutdown_drain_seconds",
        },
    )
    process_policy = ProviderProcessPolicy(
        feed_interval_seconds=process["feed_interval_seconds"],
        hello_interval_seconds=process["hello_interval_seconds"],
        shutdown_drain_seconds=process["shutdown_drain_seconds"],
        forced_join_seconds=process["forced_join_seconds"],
        inventory_maximum_pages=process["inventory_maximum_pages"],
        maximum_consecutive_unavailable=process["maximum_consecutive_unavailable"],
        observation=ObservationPolicy(
            process["observation_interval_seconds"],
            process["observation_maximum_gap_seconds"],
        ),
    )
    runner = _table(
        document,
        "runner",
        {
            "connect_seconds",
            "generate_seconds",
            "ready_seconds",
            "retire_seconds",
            "validate_seconds",
        },
    )
    deadlines = RunnerDeadlines(
        runner["connect_seconds"],
        runner["ready_seconds"],
        runner["validate_seconds"],
        runner["generate_seconds"],
        runner["retire_seconds"],
    )
    return ProviderRuntimeConfig(
        frozen_id,
        endpoint,
        maximum_records,
        reserve,
        process_policy,
        deadlines,
    )


def _table(
    document: dict[str, object],
    name: str,
    required: set[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    value = document[name]
    if type(value) is not dict:
        raise ValueError(f"Provider runtime config [{name}] must be a table")
    _fields(name, value, required, optional)
    return value


def _fields(
    name: str,
    value: dict[str, object],
    required: set[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    actual = set(value)
    if not required <= actual or actual - required - optional:
        raise ValueError(f"Provider runtime config {name} has invalid fields")
