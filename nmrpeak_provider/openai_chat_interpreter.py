"""Adapt generic interpreter turns to bounded OpenAI Chat Completions calls.

This module owns endpoint-file parsing, authenticated HTTP attempts, response
release, and normalization into transport-neutral turns. Endpoint fallback,
protocol repair, and model-specific value construction remain with the generic
interpreter and its injected capability.
"""

from __future__ import annotations

import asyncio
from dataclasses import InitVar, dataclass, field
import json
from pathlib import Path
import tomllib
from typing import Never, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from nmrpeak_provider.http_response import best_effort_release_response
from nmrpeak_provider.interpreter import (
    InterpreterEndpoint,
    InterpreterPrompt,
    InterpreterTool,
    InterpreterToolInvocation,
    InterpreterTransportError,
    InterpreterTurn,
    require_interpreter_configuration_id,
)
from nmrpeak_provider.interpreter_policy import (
    MAX_INTERPRETER_CONFIG_BYTES,
    MAX_INTERPRETER_ENDPOINTS,
    OpenAIChatCallPolicy,
)
from nmrpeak_provider.local_input import (
    LocalInputFailureReason,
    LocalInputSnapshotError,
    read_ordered_bounded_regular_files,
)


_MAX_CONFIG_DIRECTORY_ENTRIES = 32
_MAX_MODEL_BYTES = 256
_MAX_RESPONSE_BYTES = 256 * 1024
# OpenAI documents 401 as permanent. This deployment has also observed it
# intermittently among successful calls, so one retry is cheaper and more
# robust than depending on undocumented error-body distinctions.
_RETRYABLE_HTTP_STATUSES = frozenset({401, 408, 409, 429, 500, 502, 503, 504})
_REQUIRED_CONFIG_FIELDS = {"id", "base_url", "api_key", "model"}
_OPTIONAL_CONFIG_FIELDS = {"reasoning_effort"}
_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": InterpreterTool.SUBMIT_INTERPRETATION,
            "description": "Submit the complete interpreted value.",
            "parameters": {
                "type": "object",
                "properties": {"value": {}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": InterpreterTool.REPORT_INPUT_PROBLEM,
            "description": (
                "Name required source data that is missing or conflicting and state "
                "what must be provided or clarified in a new Job."
            ),
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAIChatEndpointSpec:
    configuration_id: str
    base_url: InitVar[str] = field(repr=False)
    api_key: str = field(repr=False)
    model: str
    reasoning_effort: str | None
    url: str = field(init=False, repr=False)

    def __post_init__(self, base_url: str) -> None:
        _require_api_key(self.api_key)
        _require_model(self.model)
        _require_reasoning_effort(self.reasoning_effort)
        url = _chat_completions_url(base_url)
        require_interpreter_configuration_id(self.configuration_id)
        object.__setattr__(self, "url", url)


class OpenAIChatEndpoints:
    """Ordered generic endpoints plus the response releases they initiate."""

    __slots__ = ("_pending_response_releases", "endpoints")

    def __init__(
        self,
        endpoints: tuple[InterpreterEndpoint, ...],
        pending_response_releases: set[asyncio.Task[None]],
    ) -> None:
        self.endpoints = endpoints
        self._pending_response_releases = pending_response_releases

    async def join_response_releases(self) -> None:
        """Prove every scheduled response release has reached a terminal state."""

        while self._pending_response_releases:
            await asyncio.gather(*tuple(self._pending_response_releases))


class _OpenAIChatCall:
    __slots__ = (
        "_http_client",
        "_pending_response_releases",
        "_policy",
        "_spec",
    )

    def __init__(
        self,
        spec: OpenAIChatEndpointSpec,
        policy: OpenAIChatCallPolicy,
        *,
        http_client: httpx.AsyncClient,
        pending_response_releases: set[asyncio.Task[None]],
    ) -> None:
        self._http_client = http_client
        self._pending_response_releases = pending_response_releases
        self._policy = policy
        self._spec = spec

    async def __call__(self, prompt: InterpreterPrompt) -> InterpreterTurn:
        """Return a normalized turn; classify expected failures without content.

        All HTTP attempts and retry delays share the turn deadline. Any acquired
        response that cannot be released synchronously remains process-owned
        for bounded shutdown cleanup.
        """

        request_body: dict[str, object] = {
            "model": self._spec.model,
            "messages": prompt,
            "tools": _TOOLS,
            "tool_choice": "required",
            "stream": False,
        }
        if self._spec.reasoning_effort is not None:
            request_body["reasoning_effort"] = self._spec.reasoning_effort
        deadline = (
            asyncio.get_running_loop().time() + self._policy.turn_timeout_seconds
        )
        try:
            async with asyncio.timeout_at(deadline):
                for attempt in range(self._policy.maximum_attempts):
                    final_attempt = attempt + 1 == self._policy.maximum_attempts
                    try:
                        # A stalled request must leave time for the promised
                        # retry. The enclosing turn deadline remains the owner
                        # of both attempts, their backoff, and cleanup.
                        async with asyncio.timeout(
                            self._policy.request_timeout_seconds
                        ):
                            status, response_body = await self._post_once(
                                request_body,
                                deadline=deadline,
                            )
                    except (httpx.TransportError, TimeoutError):
                        if final_attempt:
                            raise
                    else:
                        if status == 200:
                            if response_body is None:
                                raise InterpreterTransportError(
                                    "response_too_large"
                                )
                            break
                        if final_attempt or status not in _RETRYABLE_HTTP_STATUSES:
                            raise InterpreterTransportError(f"http_{status}")

                    # Retrying here, rather than in generic interpretation,
                    # keeps endpoint fallback and protocol repair independent
                    # of OpenAI-specific operational failures. The sleep and
                    # all attempts remain inside the one advertised deadline.
                    await asyncio.sleep(self._policy.retry_delay_seconds)
        except (httpx.TransportError, TimeoutError):
            raise InterpreterTransportError("endpoint_unavailable") from None

        try:
            return _parse_completion(response_body)
        except (UnicodeError, ValueError, RecursionError):
            raise InterpreterTransportError("invalid_response_envelope") from None

    async def _post_once(
        self,
        body: dict[str, object],
        *,
        deadline: float,
    ) -> tuple[int, bytes | None]:
        request = self._http_client.build_request(
            "POST",
            self._spec.url,
            headers={"Authorization": f"Bearer {self._spec.api_key}"},
            json=body,
            # __call__ owns one aggregate deadline across retry and backoff.
            # Client per-phase defaults would add a hidden, shorter timeout.
            timeout=None,
        )
        response: httpx.Response | None = None
        try:
            response = await self._http_client.send(request, stream=True)
            status = response.status_code
            if status != 200:
                response_body: bytes | None = b""
            else:
                response_body = await _read_bounded_response(response)
        except asyncio.CancelledError:
            if response is not None:
                self._schedule_response_release(response, deadline=deadline)
            raise
        except (httpx.TransportError, httpx.DecodingError):
            if response is not None:
                release = self._schedule_response_release(
                    response,
                    deadline=deadline,
                )
                await asyncio.shield(release)
            raise httpx.TransportError("response delivery failed") from None
        except Exception:
            if response is not None:
                release = self._schedule_response_release(
                    response,
                    deadline=deadline,
                )
                await asyncio.shield(release)
                if response.is_closed:
                    # httpx closes after stream exhaustion, before its byte
                    # iterator declares the body complete. A close exception
                    # is therefore an incomplete delivery at this boundary,
                    # not a trustworthy model result followed by housekeeping.
                    raise httpx.TransportError("response close failed") from None
            # Do not disguise an adapter or invariant defect as endpoint
            # unavailability; generic fallback is only for operational errors.
            raise
        except BaseException:
            if response is not None:
                release = self._schedule_response_release(
                    response,
                    deadline=deadline,
                )
                await asyncio.shield(release)
            raise

        try:
            # The enclosing turn timeout already owns this deadline. A nested
            # timeout at the same instant would make cancellation ownership
            # depend on asyncio's internal cancellation-count ordering.
            await response.aclose()
        except asyncio.CancelledError:
            self._schedule_response_release(response, deadline=deadline)
            raise
        except Exception:
            # This path is reachable for statuses whose body we deliberately
            # ignore. Preserve that authoritative status while retrying the
            # raw stream release in the background.
            self._schedule_response_release(response, deadline=deadline)
        return status, response_body

    def _schedule_response_release(
        self,
        response: httpx.Response,
        *,
        deadline: float,
    ) -> asyncio.Task[None]:
        """Release an acquired response without delaying caller cancellation."""

        release = asyncio.create_task(
            best_effort_release_response(response, deadline=deadline),
            name="interpreter-response-release",
        )
        self._pending_response_releases.add(release)
        release.add_done_callback(self._pending_response_releases.discard)
        return release


async def _read_bounded_response(response: httpx.Response) -> bytes | None:
    body = bytearray()
    remaining = _MAX_RESPONSE_BYTES + 1
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        # Bound our buffer even when content decoding yields one oversized
        # chunk; the sentinel byte is enough to classify the response.
        if len(chunk) >= remaining:
            body.extend(chunk[:remaining])
            return None
        body.extend(chunk)
        remaining -= len(chunk)
    return bytes(body)


def load_openai_chat_endpoint_specs(
    directory: str | Path,
    /,
) -> tuple[OpenAIChatEndpointSpec, ...]:
    """Snapshot and validate ordered endpoint configuration without live I/O."""

    raw_configs = _snapshot_endpoint_configs(Path(directory))
    if not raw_configs:
        raise ValueError("interpreter configuration directory has no TOML files")
    specs = tuple(_parse_config(raw) for raw in raw_configs)
    configuration_ids = [spec.configuration_id for spec in specs]
    if len(set(configuration_ids)) != len(configuration_ids):
        raise ValueError("interpreter configuration IDs must be unique")
    return specs


def bind_openai_chat_endpoints(
    specs: tuple[OpenAIChatEndpointSpec, ...],
    policy: OpenAIChatCallPolicy,
    /,
    *,
    http_client: httpx.AsyncClient,
) -> OpenAIChatEndpoints:
    """Bind prepared endpoint facts to one live HTTP and release owner."""

    pending_response_releases: set[asyncio.Task[None]] = set()
    endpoints = tuple(
        _bind_endpoint(spec, policy, http_client, pending_response_releases)
        for spec in specs
    )
    return OpenAIChatEndpoints(endpoints, pending_response_releases)


def _snapshot_endpoint_configs(directory: Path) -> tuple[bytes, ...]:
    try:
        return read_ordered_bounded_regular_files(
            directory,
            filename_suffix=".toml",
            maximum_directory_entries=_MAX_CONFIG_DIRECTORY_ENTRIES,
            maximum_files=MAX_INTERPRETER_ENDPOINTS,
            maximum_file_bytes=MAX_INTERPRETER_CONFIG_BYTES,
        )
    except LocalInputSnapshotError as error:
        _raise_config_snapshot_error(error.reason)


def _bind_endpoint(
    spec: OpenAIChatEndpointSpec,
    policy: OpenAIChatCallPolicy,
    http_client: httpx.AsyncClient,
    pending_response_releases: set[asyncio.Task[None]],
) -> InterpreterEndpoint:
    call = _OpenAIChatCall(
        spec,
        policy,
        http_client=http_client,
        pending_response_releases=pending_response_releases,
    )
    return InterpreterEndpoint(spec.configuration_id, call)


def _raise_config_snapshot_error(reason: LocalInputFailureReason) -> Never:
    if reason is LocalInputFailureReason.TOO_MANY_SELECTED_FILES:
        raise ValueError(
            f"at most {MAX_INTERPRETER_ENDPOINTS} interpreter endpoints are supported"
        ) from None
    if reason is LocalInputFailureReason.TOO_MANY_DIRECTORY_ENTRIES:
        raise ValueError(
            "interpreter configuration directory has too many entries"
        ) from None
    if reason is LocalInputFailureReason.INVALID_SELECTED_FILE:
        raise ValueError("invalid interpreter endpoint configuration") from None
    raise ValueError("interpreter configuration directory is unreadable") from None


def _parse_config(raw: bytes) -> OpenAIChatEndpointSpec:
    document = _parse_config_document(raw)
    try:
        return OpenAIChatEndpointSpec(
            configuration_id=cast(str, document["id"]),
            base_url=cast(str, document["base_url"]),
            api_key=cast(str, document["api_key"]),
            model=cast(str, document["model"]),
            reasoning_effort=cast(str | None, document.get("reasoning_effort")),
        )
    except TypeError:
        raise ValueError("invalid interpreter endpoint identity") from None


def _parse_config_document(raw: bytes) -> dict[str, object]:
    try:
        document = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ValueError("invalid interpreter endpoint configuration") from None
    fields = set(document)
    if (
        not _REQUIRED_CONFIG_FIELDS.issubset(fields)
        or not fields.issubset(_REQUIRED_CONFIG_FIELDS | _OPTIONAL_CONFIG_FIELDS)
    ):
        raise ValueError("invalid interpreter endpoint configuration fields")
    return document


def _require_api_key(value: object) -> None:
    if type(value) is not str or not value:
        raise ValueError("invalid interpreter API key")


def _require_model(value: object) -> None:
    if type(value) is not str:
        raise TypeError("model must be bounded non-blank UTF-8 text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise TypeError("model must be bounded non-blank UTF-8 text") from None
    if not value.strip() or len(encoded) > _MAX_MODEL_BYTES:
        raise TypeError("model must be bounded non-blank UTF-8 text")


def _require_reasoning_effort(value: object) -> None:
    if value is not None and (
        type(value) is not str or value not in _REASONING_EFFORTS
    ):
        raise ValueError("invalid interpreter reasoning effort")


def _chat_completions_url(value: object) -> str:
    """Return the transport-validated URL for the fixed interpreter route."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(
            ord(character) <= 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        raise ValueError("invalid interpreter base URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid interpreter base URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid interpreter base URL")
    path = parsed.path.rstrip("/")
    canonical = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    try:
        # Validate with the transport that will send the request so URL syntax
        # accepted here cannot fail as an uncategorized error after admission.
        return str(httpx.URL(canonical + "/chat/completions"))
    except httpx.InvalidURL:
        raise ValueError("invalid interpreter base URL") from None


def _parse_completion(body: bytes) -> InterpreterTurn:
    document = json.loads(
        body.decode("utf-8", errors="strict"),
        object_pairs_hook=_object_without_duplicates,
    )
    if type(document) is not dict:
        raise ValueError
    choices = document.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise ValueError
    raw_message = choices[0].get("message")
    if type(raw_message) is not dict or raw_message.get("role") != "assistant":
        raise ValueError
    content = raw_message.get("content")
    if content is not None and type(content) is not str:
        raise ValueError

    assistant_message: dict[str, object] = {
        "role": "assistant",
        "content": content,
    }
    raw_tool_calls = raw_message.get("tool_calls")
    if raw_tool_calls is None:
        return InterpreterTurn(assistant_message, None, ())
    normalized = _normalize_tool_calls(raw_tool_calls)
    if normalized is None:
        return InterpreterTurn(assistant_message, None, None)
    assistant_message["tool_calls"] = normalized
    invocation = _parse_invocation(normalized)
    return InterpreterTurn(
        assistant_message,
        invocation,
        tuple(tool_call["id"] for tool_call in normalized),
    )


def _normalize_tool_calls(value: object) -> list[dict[str, object]] | None:
    if type(value) is not list or not value:
        return None
    normalized: list[dict[str, object]] = []
    for item in value:
        if type(item) is not dict:
            return None
        call_id = item.get("id")
        function = item.get("function")
        if (
            type(call_id) is not str
            or not call_id
            or item.get("type") != "function"
            or type(function) is not dict
            or type(function.get("name")) is not str
            or type(function.get("arguments")) is not str
        ):
            return None
        normalized.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": function["name"],
                    "arguments": function["arguments"],
                },
            }
        )
    return normalized


def _parse_invocation(
    tool_calls: list[dict[str, object]],
) -> InterpreterToolInvocation | None:
    if len(tool_calls) != 1:
        return None
    function = cast(dict[str, object], tool_calls[0]["function"])
    try:
        arguments = json.loads(
            function["arguments"],
            object_pairs_hook=_object_without_duplicates,
        )
    except (RecursionError, ValueError):
        return None
    return InterpreterToolInvocation(function["name"], arguments)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value
