"""Send one exact signed provider operation over verified, bounded HTTPS."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import http.client
import math
from pathlib import Path
import re
import socket
import ssl
from time import monotonic

from .provider_signing import (
    SignedProviderRequest,
    is_canonical_https_authority,
)


_ANALYSIS_KIND = r"(?=[^&]{1,128}(?:&|$))[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_CURSOR = (
    r"(?:[A-Za-z0-9_-]{4})*"
    r"(?:[A-Za-z0-9_-][AQgw]|[A-Za-z0-9_-]{2}[AEIMQUYcgkosw048]|"
    r"[A-Za-z0-9_-]{4})"
)
_LIMIT = r"(?:[1-9]|[1-9][0-9]|100)"
_JOB_REF = r"job:[A-Za-z0-9_.-]{1,124}"
_ATTEMPT_REF = r"execution_attempt:sha256:[0-9a-f]{64}"
_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]{1,128}")
_MAX_QUERY_BYTES = 2_048
# Edge-generated failures do not carry the API envelope they replaced.
_EDGE_UNAVAILABLE_STATUSES = frozenset({502, 504})


class ProviderOperation(Enum):
    """The nine operations admitted by the pinned provider HTTP release."""

    EXECUTION_ATTEMPTS_LIST = "execution_attempts_list"
    EXECUTION_ATTEMPT_COMPLETE = "execution_attempt_complete"
    EXECUTION_ATTEMPT_FAIL = "execution_attempt_fail"
    EXECUTION_ATTEMPT_START = "execution_attempt_start"
    EXECUTION_ATTEMPT_READ = "execution_attempt_read"
    EXECUTION_ATTEMPT_PROGRESS = "execution_attempt_progress"
    PROVIDER_HELLO = "provider_hello"
    JOBS_LIST = "jobs_list"
    JOB_INPUT_READ = "job_input_read"


@dataclass(frozen=True, slots=True)
class _OperationProfile:
    method: str
    path: re.Pattern[str]
    query: re.Pattern[str]
    request_body_limit: int
    response_body_limit: int
    statuses: frozenset[int]


_COMMON_READ_STATUSES = frozenset({200, 400, 401, 403, 408, 414, 431, 500, 503})
_COMMON_MUTATION_STATUSES = frozenset(
    {200, 400, 401, 403, 404, 408, 409, 413, 414, 431, 500, 503}
)
_EMPTY_QUERY = re.compile("")
_PROFILES = {
    ProviderOperation.EXECUTION_ATTEMPTS_LIST: _OperationProfile(
        "GET",
        re.compile(r"/provider/v1/execution-attempts"),
        re.compile(rf"state=in_progress(?:&limit={_LIMIT})?(?:&cursor={_CURSOR})?"),
        0,
        65_536,
        _COMMON_READ_STATUSES,
    ),
    ProviderOperation.EXECUTION_ATTEMPT_COMPLETE: _OperationProfile(
        "POST",
        re.compile(r"/provider/v1/execution-attempts/complete"),
        _EMPTY_QUERY,
        2_097_152,
        65_536,
        _COMMON_MUTATION_STATUSES,
    ),
    ProviderOperation.EXECUTION_ATTEMPT_FAIL: _OperationProfile(
        "POST",
        re.compile(r"/provider/v1/execution-attempts/fail"),
        _EMPTY_QUERY,
        16_384,
        65_536,
        _COMMON_MUTATION_STATUSES,
    ),
    ProviderOperation.EXECUTION_ATTEMPT_START: _OperationProfile(
        "POST",
        re.compile(r"/provider/v1/execution-attempts/start"),
        _EMPTY_QUERY,
        4_096,
        65_536,
        _COMMON_MUTATION_STATUSES,
    ),
    ProviderOperation.EXECUTION_ATTEMPT_READ: _OperationProfile(
        "GET",
        re.compile(rf"/provider/v1/execution-attempts/{_ATTEMPT_REF}"),
        _EMPTY_QUERY,
        0,
        65_536,
        _COMMON_READ_STATUSES | {404},
    ),
    ProviderOperation.EXECUTION_ATTEMPT_PROGRESS: _OperationProfile(
        "PUT",
        re.compile(rf"/provider/v1/execution-attempts/{_ATTEMPT_REF}/progress"),
        _EMPTY_QUERY,
        4_096,
        65_536,
        _COMMON_MUTATION_STATUSES,
    ),
    ProviderOperation.PROVIDER_HELLO: _OperationProfile(
        "POST",
        re.compile(r"/provider/v1/hello"),
        _EMPTY_QUERY,
        524_288,
        65_536,
        _COMMON_MUTATION_STATUSES - {409},
    ),
    ProviderOperation.JOBS_LIST: _OperationProfile(
        "GET",
        re.compile(r"/provider/v1/jobs"),
        re.compile(
            rf"analysis_kind_ref={_ANALYSIS_KIND}"
            rf"(?:&has_provider_execution_attempt=(?:true|false))?"
            rf"(?:&limit={_LIMIT})?(?:&cursor={_CURSOR})?"
        ),
        0,
        65_536,
        _COMMON_READ_STATUSES,
    ),
    ProviderOperation.JOB_INPUT_READ: _OperationProfile(
        "GET",
        re.compile(rf"/provider/v1/jobs/{_JOB_REF}/input"),
        re.compile(rf"analysis_kind_ref={_ANALYSIS_KIND}"),
        0,
        131_072,
        _COMMON_READ_STATUSES | {404},
    ),
}


class RequestDelivery(Enum):
    """How far one request progressed at the HTTPS boundary."""

    NOT_SENT = "not_sent"
    POSSIBLE = "possible"
    RESPONSE_RECEIVED = "response_received"


class ResponseRejection(Enum):
    """Closed reasons why an HTTP response cannot enter provider parsing."""

    UNDECLARED_STATUS = "undeclared_status"
    INVALID_CONTENT_TYPE = "invalid_content_type"
    CONTENT_ENCODING_NOT_ADMITTED = "content_encoding_not_admitted"
    INVALID_CACHE_CONTROL = "invalid_cache_control"
    INVALID_TOPOLOGY = "invalid_topology"
    INVALID_REQUEST_ID = "invalid_request_id"
    DUPLICATE_REQUEST_ID = "duplicate_request_id"
    INVALID_CONTENT_LENGTH = "invalid_content_length"
    RESPONSE_BODY_TOO_LARGE = "response_body_too_large"


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    """A bounded response whose common provider envelope is valid."""

    status: int
    topology: str
    content_type: str
    request_id: str | None
    body: bytes


@dataclass(frozen=True, slots=True)
class ProviderRequestUnavailable:
    """No valid API response was received for this single send."""

    delivery: RequestDelivery
    cause: BaseException | None = field(default=None, compare=False, repr=False)
    status: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderTlsRejected:
    """TLS identity or protocol verification failed before HTTP delivery."""

    delivery: RequestDelivery = RequestDelivery.NOT_SENT
    cause: BaseException | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderResponseRejected:
    """The peer returned an HTTP response outside the pinned envelope."""

    reason: ResponseRejection
    status: int | None
    delivery: RequestDelivery = RequestDelivery.RESPONSE_RECEIVED
    cause: BaseException | None = field(default=None, compare=False, repr=False)


ProviderHttpsOutcome = (
    ProviderHttpResponse
    | ProviderRequestUnavailable
    | ProviderTlsRejected
    | ProviderResponseRejected
)


def provider_operation_admits_status(
    operation: ProviderOperation,
    status: int,
) -> bool:
    """Return whether the pinned operation declares this exact HTTP status."""

    if type(operation) is not ProviderOperation or type(status) is not int:
        return False
    return status in _PROFILES[operation].statuses


@dataclass(frozen=True, slots=True)
class ProviderHttpsEndpoint:
    """One canonical Server A origin and its transport trust policy."""

    origin: str
    expected_topology: str
    connect_timeout_seconds: float
    io_deadline_seconds: float
    ca_file: Path | None = None
    authority: str = field(init=False)
    host: str = field(init=False)
    port: int = field(init=False)
    tls_context: ssl.SSLContext = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_provider_https_endpoint_config(
            self.origin,
            self.expected_topology,
            self.connect_timeout_seconds,
            self.io_deadline_seconds,
        )
        prefix = "https://"
        authority = self.origin[len(prefix):]
        host, separator, port_text = authority.rpartition(":")
        if not separator:
            host = authority
            port = 443
        else:
            port = int(port_text)
        ca_file = None if self.ca_file is None else str(Path(self.ca_file))
        context = ssl.create_default_context(cafile=ca_file)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "tls_context", context)


def validate_provider_https_endpoint_config(
    origin: str,
    expected_topology: str,
    connect_timeout_seconds: float,
    io_deadline_seconds: float,
) -> None:
    """Validate endpoint facts without reading configured TLS trust."""

    prefix = "https://"
    if type(origin) is not str or not origin.startswith(prefix):
        raise ValueError("Provider API origin must be canonical HTTPS")
    if not is_canonical_https_authority(origin[len(prefix):]):
        raise ValueError("Provider API origin must contain a canonical authority")
    if expected_topology not in {"dev-local", "dev", "web"}:
        raise ValueError("Provider API topology must be dev-local, dev, or web")
    _require_positive_finite_timeout(
        connect_timeout_seconds,
        name="Provider API connect timeout",
    )
    _require_positive_finite_timeout(
        io_deadline_seconds,
        name="Provider API I/O deadline",
    )


def send_provider_request(
    *,
    endpoint: ProviderHttpsEndpoint,
    operation: ProviderOperation,
    request: SignedProviderRequest,
) -> ProviderHttpsOutcome:
    """Send exactly once and return transport evidence without retry policy."""

    if type(endpoint) is not ProviderHttpsEndpoint:
        raise TypeError("Provider request requires an exact HTTPS endpoint")
    if type(operation) is not ProviderOperation:
        raise TypeError("Provider request requires an admitted operation")
    if type(request) is not SignedProviderRequest:
        raise TypeError("Provider request must be an exact signed request")
    _validate_operation_request(endpoint, operation, request)

    connection = http.client.HTTPSConnection(
        endpoint.host,
        endpoint.port,
        timeout=endpoint.connect_timeout_seconds,
        context=endpoint.tls_context,
    )
    try:
        try:
            connection.connect()
        except (ssl.SSLCertVerificationError, ssl.SSLError) as error:
            return ProviderTlsRejected(cause=error)
        except (OSError, TimeoutError) as error:
            return ProviderRequestUnavailable(RequestDelivery.NOT_SENT, error)

        deadline = monotonic() + endpoint.io_deadline_seconds
        transport_socket = connection.sock
        if transport_socket is None:
            return ProviderRequestUnavailable(
                RequestDelivery.NOT_SENT,
                RuntimeError("HTTPS connection exposed no transport socket"),
            )
        try:
            _set_remaining_socket_timeout(transport_socket, deadline)
            connection.putrequest(
                request.method,
                request.raw_target,
                skip_host=True,
                skip_accept_encoding=True,
            )
            for name, value in request.headers.items():
                connection.putheader(name, value)
            if request.body is not None:
                connection.putheader("Content-Length", str(len(request.body)))
            connection.endheaders(request.body)
            _set_remaining_socket_timeout(transport_socket, deadline)
            response = connection.getresponse()
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            return ProviderRequestUnavailable(RequestDelivery.POSSIBLE, error)

        try:
            return _read_response(
                transport_socket=transport_socket,
                response=response,
                profile=_PROFILES[operation],
                expected_topology=endpoint.expected_topology,
                deadline=deadline,
            )
        finally:
            response.close()
    finally:
        connection.close()


def _validate_operation_request(
    endpoint: ProviderHttpsEndpoint,
    operation: ProviderOperation,
    request: SignedProviderRequest,
) -> None:
    profile = _PROFILES[operation]
    if request.authority != endpoint.authority:
        raise ValueError("Signed request authority does not match Provider API origin")
    if request.method != profile.method:
        raise ValueError("Signed request method does not match provider operation")
    if profile.path.fullmatch(request.path) is None:
        raise ValueError("Signed request path does not match provider operation")
    if (
        len(request.query.encode("ascii")) > _MAX_QUERY_BYTES
        or profile.query.fullmatch(request.query) is None
    ):
        raise ValueError("Signed request query does not match provider operation")
    body_length = 0 if request.body is None else len(request.body)
    body_required = profile.request_body_limit > 0
    if (request.body is not None) != body_required:
        raise ValueError(
            "Signed request body presence does not match provider operation"
        )
    if body_length > profile.request_body_limit:
        raise ValueError("Signed request body exceeds the provider operation limit")


def _read_response(
    *,
    transport_socket: socket.socket,
    response: http.client.HTTPResponse,
    profile: _OperationProfile,
    expected_topology: str,
    deadline: float,
) -> ProviderHttpResponse | ProviderRequestUnavailable | ProviderResponseRejected:
    status = response.status
    if status in _EDGE_UNAVAILABLE_STATUSES:
        return ProviderRequestUnavailable(
            RequestDelivery.RESPONSE_RECEIVED,
            status=status,
        )
    if status not in profile.statuses:
        return ProviderResponseRejected(ResponseRejection.UNDECLARED_STATUS, status)
    headers = response.getheaders()
    expected_media_type = (
        "application/json" if status == 200 else "application/problem+json"
    )
    if _single_header(headers, "Content-Type") != expected_media_type:
        return ProviderResponseRejected(ResponseRejection.INVALID_CONTENT_TYPE, status)
    if _header_values(headers, "Content-Encoding"):
        return ProviderResponseRejected(
            ResponseRejection.CONTENT_ENCODING_NOT_ADMITTED,
            status,
        )
    if _single_header(headers, "Cache-Control") != "no-store":
        return ProviderResponseRejected(ResponseRejection.INVALID_CACHE_CONTROL, status)
    topology = _single_header(headers, "Nmr-Api-Topology")
    if topology != expected_topology:
        return ProviderResponseRejected(ResponseRejection.INVALID_TOPOLOGY, status)
    request_ids = _header_values(headers, "X-Request-ID")
    request_id = request_ids[0] if len(request_ids) == 1 else None
    if status != 200 and (
        request_id is None or _VISIBLE_ASCII.fullmatch(request_id) is None
    ):
        return ProviderResponseRejected(ResponseRejection.INVALID_REQUEST_ID, status)
    if status == 200 and len(request_ids) > 1:
        return ProviderResponseRejected(ResponseRejection.DUPLICATE_REQUEST_ID, status)

    lengths = _header_values(headers, "Content-Length")
    if len(lengths) > 1 or (
        lengths and (not lengths[0].isdigit() or int(lengths[0]) < 0)
    ):
        return ProviderResponseRejected(ResponseRejection.INVALID_CONTENT_LENGTH, status)
    if lengths and int(lengths[0]) > profile.response_body_limit:
        return ProviderResponseRejected(ResponseRejection.RESPONSE_BODY_TOO_LARGE, status)
    declared_length = int(lengths[0]) if lengths else None
    body_parts: list[bytes] = []
    body_length = 0
    try:
        while body_length <= profile.response_body_limit:
            if body_length == declared_length or response.isclosed():
                break
            _set_remaining_socket_timeout(transport_socket, deadline)
            part = response.read1(
                min(65_536, profile.response_body_limit + 1 - body_length)
            )
            if not part:
                break
            body_parts.append(part)
            body_length += len(part)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        return ProviderRequestUnavailable(
            RequestDelivery.RESPONSE_RECEIVED,
            error,
            status,
        )
    body = b"".join(body_parts)
    if len(body) > profile.response_body_limit:
        return ProviderResponseRejected(ResponseRejection.RESPONSE_BODY_TOO_LARGE, status)
    if lengths and len(body) != int(lengths[0]):
        return ProviderRequestUnavailable(
            RequestDelivery.RESPONSE_RECEIVED,
            EOFError(
                f"HTTP response ended after {len(body)} of {int(lengths[0])} "
                "declared bytes"
            ),
            status=status,
        )
    return ProviderHttpResponse(status, topology, expected_media_type, request_id, body)


def _header_values(
    headers: list[tuple[str, str]],
    name: str,
) -> list[str]:
    lowered = name.casefold()
    return [value for header, value in headers if header.casefold() == lowered]


def _single_header(headers: list[tuple[str, str]], name: str) -> str | None:
    values = _header_values(headers, name)
    return values[0] if len(values) == 1 else None


def _set_remaining_socket_timeout(
    transport_socket: socket.socket,
    deadline: float,
) -> None:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    transport_socket.settimeout(remaining)


def _require_positive_finite_timeout(value: object, *, name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds")
