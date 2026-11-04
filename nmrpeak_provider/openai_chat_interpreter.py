"""Capture provider-owned model endpoints and compose shared SDK carriers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import ssl

from provider_core.api_config import https_trust
from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec

from nmrpeak_provider.interpreter import require_interpreter_configuration_id
from nmrpeak_provider.interpreter_policy import MAX_INTERPRETER_CONFIG_BYTES, MAX_INTERPRETER_ENDPOINTS
from nmrpeak_provider.interpreter_settings import parse_endpoint_settings
from nmrpeak_provider.local_input import (
    LocalInputSnapshotError, read_ordered_files_with_references,
)


_MAX_CONFIG_DIRECTORY_ENTRIES = 32
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_ERROR_RESPONSE_BYTES = 16 * 1024
_RETIREMENT_SECONDS = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAIChatEndpointSpec:
    configuration_id: str
    url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str
    reasoning_effort: str | None = None
    trust: ssl.SSLContext = field(repr=False, compare=False)

    def __post_init__(self):
        require_interpreter_configuration_id(self.configuration_id)
        # The shared carrier is the authority for canonical URL and header limits.
        ChatEndpointSpec(configuration_id=self.configuration_id, model=self.model,
                         url=self.url, api_key=self.api_key, reasoning_effort=self.reasoning_effort)
        if not isinstance(self.trust, ssl.SSLContext):
            raise TypeError("Interpreter endpoint requires verified TLS trust.")


def load_openai_chat_endpoint_specs(directory: str | Path, /) -> tuple[OpenAIChatEndpointSpec, ...]:
    """Snapshot the filename-ordered endpoints and their referenced inputs."""
    directory = Path(directory)
    try:
        parsed = {}
        def referenced(selected):
            requested = {}
            for name, raw in selected:
                settings = parse_endpoint_settings(raw)
                parsed[name] = settings
                requested[settings["key_file"]] = MAX_INTERPRETER_CONFIG_BYTES
                if "ca_file" in settings:
                    requested.setdefault(settings["ca_file"], 1024 * 1024)
            return requested

        selected, inputs = read_ordered_files_with_references(
            directory, filename_suffix=".toml", maximum_directory_entries=_MAX_CONFIG_DIRECTORY_ENTRIES,
            maximum_files=MAX_INTERPRETER_ENDPOINTS, maximum_file_bytes=MAX_INTERPRETER_CONFIG_BYTES,
            references=referenced,
        )
        if not selected:
            raise ValueError("Interpreter configuration directory has no TOML files.")
        result = []
        for name, _raw in selected:
            settings = parsed[name]
            key_bytes = inputs[settings["key_file"]]
            if not key_bytes.isascii():
                raise ValueError("Interpreter API key must contain ASCII text.")
            api_key = key_bytes.decode("ascii").removesuffix("\n")
            ca_file = settings.get("ca_file")
            trust = https_trust(None if ca_file is None else inputs[ca_file], label="interpreter")
            result.append(OpenAIChatEndpointSpec(
                configuration_id=name.removesuffix(".toml"), url=settings["url"], api_key=api_key,
                model=settings["model"], reasoning_effort=settings.get("reasoning_effort"), trust=trust,
            ))
        return tuple(result)
    except (LocalInputSnapshotError, UnicodeError, OSError, ssl.SSLError) as error:
        raise ValueError("Cannot load the captured interpreter endpoint inputs.") from error


def endpoint_factories(specs: tuple[OpenAIChatEndpointSpec, ...]):
    """Bind TLS and credentials while the SDK chooses each model's time window."""
    return tuple(
        ChatEndpointFactory(
            spec=ChatEndpointSpec(configuration_id=spec.configuration_id, model=spec.model,
                                  url=spec.url, api_key=spec.api_key, reasoning_effort=spec.reasoning_effort),
            trust=spec.trust, retirement_seconds=_RETIREMENT_SECONDS,
            request_bytes=_MAX_REQUEST_BYTES, response_bytes=_MAX_RESPONSE_BYTES,
            error_response_bytes=_MAX_ERROR_RESPONSE_BYTES,
        )
        for spec in specs
    )


def interpreter_tools(*, submit_description: str, value_schema: dict, report_description: str) -> list[dict]:
    """Advertise Peak's current typed submission and unverified report actions."""
    return [
        {"type": "function", "function": {"name": "submit_interpretation", "description": submit_description,
          "parameters": {"type": "object", "properties": {"value": value_schema},
                         "required": ["value"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "report_interpretation_problem", "description": report_description,
          "parameters": {"type": "object", "properties": {"message": {"type": "string"}},
                         "required": ["message"], "additionalProperties": False}}},
    ]
