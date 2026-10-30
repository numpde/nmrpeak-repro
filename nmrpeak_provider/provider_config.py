"""Admit local policy separately from the deployment-generated API route grant."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import tomllib

from .attempt_journal import validate_frozen_generation_id
from .canonical_json import canonical_json_bytes
from .interpreter_policy import InterpreterPolicy, OpenAIChatCallPolicy
from .native_policy import RETIREMENT_SECONDS

SCHEMA_ID = "nmrpeak.provider.runtime_config.v3"
CONFIG_PATH = Path("/run/config/nmrpeak-provider/provider.toml")
API_INPUT_ROOT = CONFIG_PATH.parent / "grants"
FROZEN_ROOT = Path("/run/nmrpeak-provider/frozen")
IDENTITY_LOCK_PATH = Path("/run/nmrpeak-provider-lock/provider.lock")
JOURNAL_PATH = Path("/var/lib/nmrpeak-provider/journal")
HF_SOCKET_PATH = "/run/nmrpeak-provider/hf/session.sock"
CHF_SOCKET_PATH = "/run/nmrpeak-provider/chf/session.sock"

class ProviderConfigurationRejected(ValueError):
    """An operator configuration violates a reviewed NMR-owned input contract."""


@dataclass(frozen=True, slots=True)
class RunnerDeadlines:
    """Bounded waits for each private runner exchange phase."""

    connect_seconds: float
    ready_seconds: float
    validate_seconds: float
    generate_seconds: float
    retire_seconds: float

    def __post_init__(self) -> None:
        for value in (self.connect_seconds, self.ready_seconds, self.validate_seconds,
                      self.generate_seconds, self.retire_seconds):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ProviderConfigurationRejected("NMRPeak runner deadlines must be positive finite seconds")


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    """Poll live Attempts and pace recovery without changing Attempt authority."""

    poll_interval_seconds: float
    retry_initial_seconds: float
    retry_max_seconds: float

    def __post_init__(self):
        for value in (self.poll_interval_seconds, self.retry_initial_seconds, self.retry_max_seconds):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ProviderConfigurationRejected("Observation intervals and retry delays must be positive finite seconds")
        if self.retry_initial_seconds > self.retry_max_seconds:
            raise ProviderConfigurationRejected("Observation initial retry delay must not exceed its maximum")


@dataclass(frozen=True, slots=True)
class ProviderProcessPolicy:
    """The bounded waits and inventory extent owned by one provider process."""

    feed_interval_seconds: float
    hello_interval_seconds: float
    shutdown_drain_seconds: float
    forced_join_seconds: float
    inventory_maximum_pages: int
    observation: ObservationPolicy

    def __post_init__(self) -> None:
        for value in (self.feed_interval_seconds, self.hello_interval_seconds,
                      self.shutdown_drain_seconds, self.forced_join_seconds):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ProviderConfigurationRejected("NMRPeak process waits must be positive finite seconds")
        if type(self.inventory_maximum_pages) is not int or self.inventory_maximum_pages < 1:
            raise ProviderConfigurationRejected("NMRPeak inventory page bound must be positive")
        if type(self.observation) is not ObservationPolicy:
            raise TypeError("NMRPeak process requires an admitted observation policy")


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """Operator selection and scientific policy without a transport route grant."""

    api: dict
    shared_project: str | None
    shared_gid: int | None
    process: ProviderProcessPolicy
    runner: RunnerDeadlines
    interpreter: InterpreterPolicy


@dataclass(frozen=True, slots=True)
class ProviderRuntimeConfig:
    """Frozen policy plus the API socket granted by the actual SDK projection."""

    frozen_generation_id: str
    policy: ProviderPolicy
    api_proxy_socket: str
    outbox_socket: str


def _document(raw, fields):
    if type(raw) is not bytes or len(raw) > 65536:
        raise ProviderConfigurationRejected("Provider configuration must be bounded bytes")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as cause:
        raise ProviderConfigurationRejected("Provider configuration must be valid UTF-8 TOML") from cause
    _fields("top level", document, fields)
    if document["schema_id"] != SCHEMA_ID:
        raise ProviderConfigurationRejected("Provider configuration schema is unsupported; startup requires v3")
    return document


_POLICY_FIELDS = {"schema_id", "api", "process", "runner", "interpreter"}


def decode_provider_template(raw: bytes) -> ProviderPolicy:
    """Admit operator input, refusing deployment-generated route fields."""
    document = _document(raw, _POLICY_FIELDS | {"shared"})
    shared = _table(document, "shared", {"project", "gid"})
    project, gid = shared["project"], shared["gid"]
    if type(project) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", project) is None:
        raise ProviderConfigurationRejected("Shared deployment project must be a lowercase container project name")
    if type(gid) is not int or not 0 < gid <= 4294967294:
        raise ProviderConfigurationRejected("Shared socket group ID must be a nonzero unsigned group ID")
    return _policy(document, project, gid)


def decode_provider_runtime_config(raw: bytes) -> ProviderRuntimeConfig:
    """Require a frozen route before runtime can construct an API transport."""
    document = _document(raw, _POLICY_FIELDS | {"frozen_generation_id", "api_route"})
    frozen_id = document["frozen_generation_id"]
    try:
        validate_frozen_generation_id(frozen_id)
    except ValueError as cause:
        raise ProviderConfigurationRejected(str(cause)) from cause
    route = _table(document, "api_route", {"proxy_socket", "outbox_socket"})
    socket = _absolute_path(route["proxy_socket"], "Generated API proxy socket")
    outbox = _absolute_path(route["outbox_socket"], "Generated outbox socket")
    return ProviderRuntimeConfig(frozen_id, _policy(document, None, None), str(socket), str(outbox))


def render_provider_runtime_config(policy, *, frozen_generation_id, api_proxy_socket, outbox_socket):
    """Encode admitted policy with the deployment's generated identity and grants."""
    process, observation = policy.process, policy.process.observation
    tables = (
        ("api", captured_api_fields(policy.api)),
        ("process", {
            "feed_interval_seconds": process.feed_interval_seconds,
            "hello_interval_seconds": process.hello_interval_seconds,
            "shutdown_drain_seconds": process.shutdown_drain_seconds,
            "forced_join_seconds": process.forced_join_seconds,
            "inventory_maximum_pages": process.inventory_maximum_pages,
            "observation_interval_seconds": observation.poll_interval_seconds,
            "observation_retry_initial_seconds": observation.retry_initial_seconds,
            "observation_retry_max_seconds": observation.retry_max_seconds,
        }),
        ("runner", asdict(policy.runner)),
        ("interpreter", {
            "request_timeout_seconds": policy.interpreter.call_policy.request_timeout_seconds,
            "turn_timeout_seconds": policy.interpreter.call_policy.turn_timeout_seconds,
            "interpretation_timeout_seconds": policy.interpreter.interpretation_timeout_seconds,
        }),
        ("api_route", {"proxy_socket": api_proxy_socket, "outbox_socket": outbox_socket}),
    )
    lines = [f"schema_id = {json.dumps(SCHEMA_ID)}",
             f"frozen_generation_id = {json.dumps(frozen_generation_id)}"]
    for section, values in tables:
        lines.append(f"\n[{section}]")
        lines.extend(f"{key} = {json.dumps(value, ensure_ascii=True, allow_nan=False)}"
                     for key, value in values.items())
    return ("\n".join(lines) + "\n").encode("utf-8")


def runtime_config_id(files: dict[str, bytes]) -> str:
    """Identify every filename and byte in one captured startup-input tree."""
    material = {path: {"sha256": sha256(raw).hexdigest(), "byte_length": len(raw)}
                for path, raw in files.items()}
    return "sha256:" + sha256(
        b"nmrpeak.provider_runtime_config.v2\0" + canonical_json_bytes(material),
    ).hexdigest()


def api_input_target(path: str) -> Path:
    """Name an SDK-admitted file grant without importing its host path layout."""
    return API_INPUT_ROOT / sha256(path.encode("utf-8")).hexdigest()


def captured_api_fields(api):
    fields = dict(api)
    for name in ("credential_file", "ca_file"):
        if name in fields:
            fields[name] = str(api_input_target(fields[name]))
    return fields


def _absolute_path(value, label):
    if type(value) is not str or not value or "\0" in value:
        raise ProviderConfigurationRejected(f"{label} must be an absolute normalized path")
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts or str(path) != value:
        raise ProviderConfigurationRejected(f"{label} must be an absolute normalized path below root")
    return path


def _policy(document, shared_project, shared_gid):
    api = document["api"]
    if type(api) is not dict or "credential_file" not in api:
        raise ProviderConfigurationRejected("Provider API input must include a credential file")
    for field in ("credential_file", "ca_file"):
        if field in api:
            _absolute_path(api[field], f"API {field}")
    process = _table(document, "process", {
        "feed_interval_seconds", "hello_interval_seconds", "shutdown_drain_seconds",
        "forced_join_seconds", "inventory_maximum_pages", "observation_interval_seconds",
        "observation_retry_initial_seconds", "observation_retry_max_seconds",
    })
    process_policy = ProviderProcessPolicy(
        process["feed_interval_seconds"], process["hello_interval_seconds"],
        process["shutdown_drain_seconds"], process["forced_join_seconds"],
        process["inventory_maximum_pages"], ObservationPolicy(
            process["observation_interval_seconds"], process["observation_retry_initial_seconds"],
            process["observation_retry_max_seconds"],
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
    if deadlines.retire_seconds < RETIREMENT_SECONDS:
        raise ProviderConfigurationRejected(
            f"runner.retire_seconds must be at least {RETIREMENT_SECONDS} for native TERM, KILL, scratch cleanup and observation"
        )
    interpreter = _table(
        document,
        "interpreter",
        {
            "interpretation_timeout_seconds",
            "request_timeout_seconds",
            "turn_timeout_seconds",
        },
    )
    try:
        interpreter_policy = InterpreterPolicy(
            call_policy=OpenAIChatCallPolicy(
                request_timeout_seconds=interpreter["request_timeout_seconds"],
                turn_timeout_seconds=interpreter["turn_timeout_seconds"],
            ),
            interpretation_timeout_seconds=interpreter[
                "interpretation_timeout_seconds"
            ],
        )
    except ValueError as cause:
        raise ProviderConfigurationRejected(str(cause)) from cause
    return ProviderPolicy(dict(api), shared_project, shared_gid, process_policy, deadlines, interpreter_policy)

def _table(
    document: dict[str, object],
    name: str,
    required: set[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    value = document[name]
    if type(value) is not dict:
        raise ProviderConfigurationRejected(f"Provider runtime config [{name}] must be a table")
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
        raise ProviderConfigurationRejected(f"Provider runtime config {name} has invalid fields")
