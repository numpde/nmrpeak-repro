"""Adapt generic interpreter turns to bounded OpenAI Chat Completions calls.

This module owns endpoint-file parsing, authenticated HTTP attempts, response
release, and normalization into transport-neutral turns. Endpoint fallback,
protocol repair, and model-specific value construction remain with the generic
interpreter and its injected capability.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import InitVar, dataclass, field, replace
import json
from pathlib import Path
import re
import tomllib
from typing import Never, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from provider_core.diagnostics import failure_evidence
from provider_core.decoded_body import DecodedBodyRejected
from provider_core.network import network_availability
from provider_core.transport import ExchangeFailed, HttpTransport

from nmrpeak_provider.diagnostic_text import diagnostic_text, exception_diagnostic
from nmrpeak_provider.interpreter import (
    InterpreterEndpoint,
    InterpreterEndpointFailure,
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
_MAX_ERROR_RESPONSE_BYTES = 16 * 1024
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


class _EndpointUnavailable(RuntimeError):
    """A settled SDK network or request-deadline failure permits policy retry."""


class OpenAIChatEndpoints:
    """Retain the actual HTTP owner across ordered endpoint calls and retirement."""

    __slots__ = ("_transport", "_closed", "_trust", "_before_send", "endpoints")

    def __init__(self, *, before_send):
        self._transport = None
        self._closed = False
        self._before_send = before_send
        self._trust = httpx.create_ssl_context(trust_env=False)
        self.endpoints = ()

    async def retire(self):
        """Close admission before observing the same original transport's cleanup."""
        self._closed = True
        if self._transport is not None:
            await self._transport.retire(owner=self)

    async def post(self, request, *, request_deadline, deadline, secrets):
        loop = asyncio.get_running_loop()
        if self._closed:
            raise RuntimeError("Cannot send interpreter request: this Attempt's endpoint owner is retiring")
        if loop.time() >= request_deadline:
            raise TimeoutError("Interpreter request deadline expired before HTTP dispatch")
        if self._before_send is not None:
            async with asyncio.timeout_at(request_deadline):
                await self._before_send(request)
        now = loop.time()
        remaining = request_deadline - now
        if remaining <= 0:
            raise TimeoutError("Interpreter request deadline expired before HTTP dispatch")
        if self._closed:
            raise RuntimeError("Cannot send interpreter request: this Attempt's endpoint owner is retiring")
        if self._transport is not None and not self._transport.retirement_confirmed:
            raise ExchangeFailed("Cannot send interpreter request: the original HTTP owner remains unsettled")
        transport = HttpTransport(trust=self._trust, exchange_seconds=remaining,
                                  retirement_seconds=deadline-now, allow_http=True)
        self._transport = transport
        primary = None
        try:
            try:
                response = await transport.exchange_decoded(
                    request, maximum_bytes=_MAX_RESPONSE_BYTES,
                    error_maximum_bytes=_MAX_ERROR_RESPONSE_BYTES, owner=self, not_after=deadline,
                )
            except BaseException as cause:
                primary = cause
                raise
            finally:
                try:
                    await transport.retire(owner=self)
                except BaseException as cleanup:
                    if primary is not None and cleanup is not primary:
                        raise BaseExceptionGroup("Interpreter HTTP exchange and retirement failed", [primary, cleanup]) from None
                    raise
        except ExchangeFailed as failure:
            if (not transport.retirement_confirmed or failure.cleanup_failures
                    or failure.caller_interruption is not None):
                raise
            head = failure.response_head
            if type(failure.primary) is DecodedBodyRejected:
                request_id = None if head is None else httpx.Headers(head.headers).get("x-request-id")
                if head is not None and head.status != 200:
                    return head.status, None, request_id, str(failure.primary)
                if head is not None and failure.primary.reason == "size_limit":
                    return head.status, None, request_id, None
                raise _EndpointUnavailable("Interpreter response decoding failed after confirmed retirement") from failure
            if (network_availability(failure.primary, failure.network_io, network_failure=failure.network_failure)
                    or type(failure.primary) is TimeoutError and loop.time() >= request_deadline):
                if head is not None and head.status != 200:
                    return head.status, None, httpx.Headers(head.headers).get("x-request-id"), (
                        "Error body could not be read: "
                        + json.dumps(failure_evidence(failure, secrets=secrets), ensure_ascii=True)
                    )
                raise _EndpointUnavailable("Interpreter network request failed after confirmed retirement") from failure
            raise
        return response.status, response.body, httpx.Headers(response.headers).get("x-request-id"), None


class _OpenAIChatCall:
    __slots__ = (
        "_http_owner",
        "_policy",
        "_spec",
        "_tools",
        "_secrets",
        "_report_endpoint_failure",
    )

    def __init__(
        self,
        spec: OpenAIChatEndpointSpec,
        policy: OpenAIChatCallPolicy,
        *,
        http_owner: OpenAIChatEndpoints,
        tools: tuple[dict[str, object], dict[str, object]],
        secrets: tuple[str, ...],
        report_endpoint_failure: Callable[[InterpreterEndpointFailure], None] | None,
    ) -> None:
        self._http_owner = http_owner
        self._policy = policy
        self._spec = spec
        self._tools = tools
        self._secrets = secrets
        self._report_endpoint_failure = report_endpoint_failure

    async def __call__(self, prompt: InterpreterPrompt) -> InterpreterTurn:
        """Return a normalized turn; classify expected failures without content.

        All attempts, pacing, retries and retirement share the original turn
        deadline. Unconfirmed HTTP retirement escapes retry and fallback.
        """

        request_body: dict[str, object] = {
            "model": self._spec.model,
            "messages": prompt,
            "tools": self._tools,
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
                        status, response_body, request_id, detail_problem = await self._post_once(
                            request_body, deadline=deadline,
                        )
                    except (_EndpointUnavailable, TimeoutError) as error:
                        if final_attempt:
                            raise
                        self._record_retry({"explanation": json.dumps(failure_evidence(error, secrets=self._secrets), ensure_ascii=True)})
                    else:
                        if status == 200:
                            if response_body is None:
                                raise InterpreterTransportError(
                                    "response_too_large"
                                )
                            break
                        if final_attempt or status not in _RETRYABLE_HTTP_STATUSES:
                            raise InterpreterTransportError(
                                f"http_{status}",
                                http_status=status,
                                **_endpoint_error_facts(response_body, request_id, secrets=self._secrets, detail_problem=detail_problem),
                            )

                        self._record_retry(_endpoint_error_facts(
                            response_body, request_id, secrets=self._secrets, detail_problem=detail_problem,
                        ), http_status=status)

                    # Retrying here, rather than in generic interpretation,
                    # keeps endpoint fallback and protocol repair independent
                    # of OpenAI-specific operational failures. The sleep and
                    # all attempts remain inside the one advertised deadline.
                    await asyncio.sleep(self._policy.retry_delay_seconds)
        except (_EndpointUnavailable, TimeoutError) as error:
            raise InterpreterTransportError(
                "endpoint_unavailable", explanation=json.dumps(failure_evidence(error, secrets=self._secrets), ensure_ascii=True)
            ) from None

        try:
            turn = _parse_completion(response_body)
            invocation = turn.invocation
            if (invocation is not None and invocation.name == "report_input_problem"
                    and type(invocation.arguments) is dict
                    and type(invocation.arguments.get("message")) is str):
                arguments = dict(invocation.arguments)
                arguments["message"] = diagnostic_text(arguments["message"], secrets=self._secrets, limit=8192)
                turn = replace(turn, invocation=replace(invocation, arguments=arguments))
            return turn
        except (UnicodeError, ValueError, RecursionError) as error:
            raise InterpreterTransportError(
                "invalid_response_envelope", explanation=json.dumps(failure_evidence(error, secrets=self._secrets), ensure_ascii=True)
            ) from None

    def _record_retry(self, facts: dict[str, str], *, http_status: int | None = None) -> None:
        if self._report_endpoint_failure is not None:
            self._report_endpoint_failure(InterpreterEndpointFailure(
                configuration_id=self._spec.configuration_id, failure_kind="transport",
                failure_reason="endpoint_retry", http_status=http_status, **facts,
            ))

    async def _post_once(self, body, *, deadline):
        request_deadline = min(deadline, asyncio.get_running_loop().time() + self._policy.request_timeout_seconds)
        request = httpx.Request("POST", self._spec.url,
                                headers={"Authorization": f"Bearer {self._spec.api_key}"}, json=body)
        return await self._http_owner.post(request, request_deadline=request_deadline,
                                           deadline=deadline, secrets=self._secrets)


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
    before_send=None,
    submit_interpretation_description: str,
    submit_interpretation_value_schema: dict[str, object],
    report_input_problem_description: str,
    report_endpoint_failure: Callable[[InterpreterEndpointFailure], None] | None = None,
) -> OpenAIChatEndpoints:
    """Snapshot one capability tool contract and bind it to every endpoint."""

    tools = _interpreter_tools(
        submit_interpretation_description=submit_interpretation_description,
        submit_interpretation_value_schema=submit_interpretation_value_schema,
        report_input_problem_description=report_input_problem_description,
    )
    owner = OpenAIChatEndpoints(before_send=before_send)
    owner.endpoints = tuple(
        _bind_endpoint(spec, policy, owner, tools, tuple(item.api_key for item in specs), report_endpoint_failure)
        for spec in specs
    )
    return owner


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
        _raise_config_snapshot_error(error)


def _bind_endpoint(
    spec: OpenAIChatEndpointSpec,
    policy: OpenAIChatCallPolicy,
    http_owner: OpenAIChatEndpoints,
    tools: tuple[dict[str, object], dict[str, object]],
    secrets: tuple[str, ...],
    report_endpoint_failure: Callable[[InterpreterEndpointFailure], None] | None,
) -> InterpreterEndpoint:
    call = _OpenAIChatCall(
        spec,
        policy,
        http_owner=http_owner,
        tools=tools,
        secrets=secrets,
        report_endpoint_failure=report_endpoint_failure,
    )
    return InterpreterEndpoint(spec.configuration_id, call)


def _interpreter_tools(
    *,
    submit_interpretation_description: str,
    submit_interpretation_value_schema: dict[str, object],
    report_input_problem_description: str,
) -> tuple[dict[str, object], dict[str, object]]:
    for name, description in (
        ("submit_interpretation_description", submit_interpretation_description),
        ("report_input_problem_description", report_input_problem_description),
    ):
        if type(description) is not str or not description.strip():
            raise ValueError(f"{name} must be non-empty text")
    if type(submit_interpretation_value_schema) is not dict:
        raise TypeError("submit_interpretation_value_schema must be an object")
    try:
        encoded_schema = json.dumps(
            submit_interpretation_value_schema,
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
        value_schema = json.loads(encoded_schema)
    except (RecursionError, TypeError, UnicodeError, ValueError):
        raise ValueError(
            "submit_interpretation_value_schema must contain valid JSON"
        ) from None
    return (
        {
            "type": "function",
            "function": {
                "name": InterpreterTool.SUBMIT_INTERPRETATION,
                "description": submit_interpretation_description,
                "parameters": {
                    "type": "object",
                    "properties": {"value": value_schema},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": InterpreterTool.REPORT_INPUT_PROBLEM,
                "description": report_input_problem_description,
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            },
        },
    )


def _raise_config_snapshot_error(error: LocalInputSnapshotError) -> Never:
    reason = error.reason
    if reason is LocalInputFailureReason.TOO_MANY_SELECTED_FILES:
        raise ValueError(
            f"at most {MAX_INTERPRETER_ENDPOINTS} interpreter endpoints are supported"
        ) from error
    if reason is LocalInputFailureReason.TOO_MANY_DIRECTORY_ENTRIES:
        raise ValueError(
            "interpreter configuration directory has too many entries"
        ) from error
    if reason is LocalInputFailureReason.INVALID_SELECTED_FILE:
        raise ValueError("invalid interpreter endpoint configuration") from error
    raise ValueError("interpreter configuration directory is unreadable") from error


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
    except TypeError as error:
        raise ValueError("invalid interpreter endpoint identity") from error


def _parse_config_document(raw: bytes) -> dict[str, object]:
    try:
        document = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("invalid interpreter endpoint configuration") from error
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
    except UnicodeEncodeError as error:
        raise TypeError("model must be bounded non-blank UTF-8 text") from error
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
    except ValueError as error:
        raise ValueError("invalid interpreter base URL") from error
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
    except httpx.InvalidURL as error:
        raise ValueError("invalid interpreter base URL") from error


def _endpoint_error_facts(
    body: bytes | None, request_id: str | None, *, secrets: tuple[str, ...],
    detail_problem: str | None,
) -> dict[str, str]:
    """Keep endpoint explanations without treating remote claims as diagnoses."""
    facts: dict[str, str] = {}
    if request_id is not None:
        facts["request_id"] = diagnostic_text(request_id, secrets=secrets, limit=256)
    details = [detail_problem] if detail_problem else []
    if body is None:
        if not detail_problem:
            details.append("Error body exceeded the diagnostic read limit.")
    else:
        try:
            document = json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_object_without_duplicates)
        except (UnicodeError, ValueError, RecursionError) as error:
            details.append("Endpoint error body is not valid JSON: " + exception_diagnostic(error, secrets=secrets, limit=1024))
            details.append(diagnostic_text(body.decode("utf-8", errors="replace"), secrets=secrets, limit=2048))
        else:
            error = document.get("error") if type(document) is dict else None
            if type(error) is dict:
                for source, destination in (("type", "error_type"), ("code", "error_code")):
                    if type(error.get(source)) is str:
                        facts[destination] = diagnostic_text(error[source], secrets=secrets, limit=256)
                for name in ("message", "param"):
                    if type(error.get(name)) is str:
                        details.append(name + ": " + error[name])
            else:
                details.append("Endpoint error response: " + body.decode("utf-8", errors="replace"))
    if details:
        facts["explanation"] = diagnostic_text("\n".join(details), secrets=secrets, limit=4096)
    return facts


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
    # Some reasoning-capable providers require prior thinking on tool-result
    # turns. The generic repair loop replays this message, so keep the optional
    # provider field rather than silently discarding it. The response-size cap
    # already bounds it; do not reinterpret it as ordinary assistant content.
    reasoning = raw_message.get("reasoning_content")
    if reasoning is not None:
        if type(reasoning) is not str:
            raise ValueError("reasoning_content must be text or null")
        assistant_message["reasoning_content"] = reasoning
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
