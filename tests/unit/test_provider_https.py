"""Exercise the one-send provider boundary against a real fake TLS peer."""

from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
import unittest

from cryptography.hazmat.primitives.asymmetric import ed25519

from nmrpeak_provider.provider_https import (
    ProviderHttpResponse,
    ProviderHttpsEndpoint,
    ProviderOperation,
    ProviderRequestUnavailable,
    ProviderResponseRejected,
    ProviderTlsRejected,
    RequestDelivery,
    ResponseRejection,
    send_provider_request,
)
from nmrpeak_provider.provider_outcomes import (
    AttemptMutationCommitPossible,
    interpret_execution_attempt_start,
)
from nmrpeak_provider.provider_problems import ProviderProblem
from nmrpeak_provider.provider_requests import (
    prepare_execution_attempt_start,
    sign_prepared_provider_request,
)
from nmrpeak_provider.provider_signing import sign_provider_request
from tests.fakes.tls_certificates import write_test_certificates


_PRIVATE_KEY = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


class ProviderHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = TemporaryDirectory()
        cls._certificate_directory = Path(cls._temporary_directory.name)
        write_test_certificates(cls._certificate_directory)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    def test_get_preserves_the_exact_signed_target_and_headers(self) -> None:
        with _tls_server(self._certificate_directory) as server:
            endpoint = self._endpoint(server.port)
            request = _signed_request(
                authority=endpoint.authority,
                method="GET",
                path="/provider/v1/jobs",
                query="analysis_kind_ref=mol_from_1h_peaks&limit=1",
                body=None,
            )
            outcome = send_provider_request(
                endpoint=endpoint,
                operation=ProviderOperation.JOBS_LIST,
                request=request,
            )

        self.assertIs(type(outcome), ProviderHttpResponse, repr(outcome))
        self.assertEqual(outcome.body, b"{}")
        self.assertEqual(len(server.requests), 1)
        captured = server.requests[0]
        self.assertEqual(
            captured["requestline"],
            (
                "GET /provider/v1/jobs?"
                "analysis_kind_ref=mol_from_1h_peaks&limit=1 HTTP/1.1"
            ),
        )
        self.assertEqual(captured["host"], endpoint.authority)
        self.assertEqual(captured["body"], b"")
        self.assertEqual(captured["signature-input"], request.headers["Signature-Input"])

    def test_post_preserves_exact_body_and_digest(self) -> None:
        body = b'{"schema_id":"nmr.provider.hello_request.v1"}'
        with _tls_server(self._certificate_directory) as server:
            endpoint = self._endpoint(server.port)
            request = _signed_request(
                authority=endpoint.authority,
                method="POST",
                path="/provider/v1/hello",
                query="",
                body=body,
            )
            outcome = send_provider_request(
                endpoint=endpoint,
                operation=ProviderOperation.PROVIDER_HELLO,
                request=request,
            )

        self.assertIs(type(outcome), ProviderHttpResponse, repr(outcome))
        captured = server.requests[0]
        self.assertEqual(captured["body"], body)
        self.assertEqual(captured["content-length"], str(len(body)))
        self.assertEqual(
            captured["content-digest"], request.headers["Content-Digest"]
        )

    def test_operation_mismatch_is_rejected_before_network_io(self) -> None:
        with _tls_server(self._certificate_directory) as server:
            endpoint = self._endpoint(server.port)
            request = _signed_request(
                authority=endpoint.authority,
                method="GET",
                path="/provider/v1/jobs",
                query="limit=1&analysis_kind_ref=mol_from_1h_peaks",
                body=None,
            )
            with self.assertRaisesRegex(ValueError, "query"):
                send_provider_request(
                    endpoint=endpoint,
                    operation=ProviderOperation.JOBS_LIST,
                    request=request,
                )
        self.assertEqual(server.requests, [])

    def test_query_values_retain_the_pinned_bounds_and_encoding(self) -> None:
        with _tls_server(self._certificate_directory) as server:
            endpoint = self._endpoint(server.port)
            invalid_queries = (
                "analysis_kind_ref=" + "a" * 129,
                "analysis_kind_ref=mol_from_1h_peaks&limit=01",
                "analysis_kind_ref=mol_from_1h_peaks&limit=101",
                "analysis_kind_ref=mol_from_1h_peaks&cursor=A",
                "analysis_kind_ref=mol_from_1h_peaks&cursor=AB",
            )
            for query in invalid_queries:
                with self.subTest(query=query):
                    with self.assertRaisesRegex(ValueError, "query"):
                        send_provider_request(
                            endpoint=endpoint,
                            operation=ProviderOperation.JOBS_LIST,
                            request=_signed_request(
                                authority=endpoint.authority,
                                method="GET",
                                path="/provider/v1/jobs",
                                query=query,
                                body=None,
                            ),
                        )
        self.assertEqual(server.requests, [])

    def test_connection_refusal_proves_the_request_was_not_sent(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        endpoint = self._endpoint(port)
        outcome = send_provider_request(
            endpoint=endpoint,
            operation=ProviderOperation.EXECUTION_ATTEMPTS_LIST,
            request=_signed_request(
                authority=endpoint.authority,
                method="GET",
                path="/provider/v1/execution-attempts",
                query="state=in_progress",
                body=None,
            ),
        )
        self.assertEqual(
            outcome,
            ProviderRequestUnavailable(RequestDelivery.NOT_SENT),
        )
        self.assertIsInstance(outcome.cause, ConnectionRefusedError)

    def test_untrusted_server_certificate_is_a_fatal_tls_rejection(self) -> None:
        with _tls_server(self._certificate_directory) as server:
            endpoint = ProviderHttpsEndpoint(
                origin=f"https://localhost:{server.port}",
                expected_topology="dev-local",
                connect_timeout_seconds=1,
                io_deadline_seconds=1,
            )
            outcome = send_provider_request(
                endpoint=endpoint,
                operation=ProviderOperation.EXECUTION_ATTEMPTS_LIST,
                request=_signed_request(
                    authority=endpoint.authority,
                    method="GET",
                    path="/provider/v1/execution-attempts",
                    query="state=in_progress",
                    body=None,
                ),
            )
        self.assertEqual(outcome, ProviderTlsRejected())
        self.assertIsInstance(outcome.cause, ssl.SSLError)
        self.assertEqual(server.requests, [])

    def test_response_envelope_rejects_each_untrusted_common_fact(self) -> None:
        cases = (
            ({"Content-Type": "text/plain"}, ResponseRejection.INVALID_CONTENT_TYPE),
            ({"Cache-Control": "private"}, ResponseRejection.INVALID_CACHE_CONTROL),
            ({"Nmr-Api-Topology": "web"}, ResponseRejection.INVALID_TOPOLOGY),
            (
                {"Content-Encoding": "gzip"},
                ResponseRejection.CONTENT_ENCODING_NOT_ADMITTED,
            ),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                headers = _valid_response_headers() | overrides
                with _tls_server(
                    self._certificate_directory,
                    response_headers=headers,
                ) as server:
                    outcome = self._send_inventory(server.port)
                self.assertEqual(
                    outcome,
                    ProviderResponseRejected(reason, 200),
                )

    def test_problem_response_requires_and_preserves_request_id(self) -> None:
        headers = _valid_response_headers(
            content_type="application/problem+json",
            request_id="request-test-1",
        )
        with _tls_server(
            self._certificate_directory,
            status=503,
            response_headers=headers,
        ) as server:
            outcome = self._send_inventory(server.port)
        self.assertIs(type(outcome), ProviderHttpResponse, repr(outcome))
        self.assertEqual(outcome.status, 503)
        self.assertEqual(outcome.request_id, "request-test-1")
        self.assertEqual(len(server.requests), 1)

    def test_edge_gateway_failures_are_unavailable_without_api_envelope(self) -> None:
        for status in (502, 504):
            with self.subTest(status=status):
                with _tls_server(
                    self._certificate_directory,
                    status=status,
                    response_headers={"Content-Type": "text/html"},
                    response_body=b"gateway failure",
                ) as server:
                    outcome = self._send_inventory(server.port)
                self.assertEqual(
                    outcome,
                    ProviderRequestUnavailable(
                        RequestDelivery.RESPONSE_RECEIVED,
                        status=status,
                    ),
                )

    def test_other_undeclared_status_remains_fatal(self) -> None:
        with _tls_server(
            self._certificate_directory,
            status=501,
            response_headers={"Content-Type": "text/html"},
        ) as server:
            outcome = self._send_inventory(server.port)
        self.assertEqual(
            outcome,
            ProviderResponseRejected(ResponseRejection.UNDECLARED_STATUS, 501),
        )

    def test_response_body_limit_accepts_exactly_the_cap_and_rejects_one_more(self) -> None:
        for length, expected_type in (
            (131_072, ProviderHttpResponse),
            (131_073, ProviderResponseRejected),
        ):
            with self.subTest(length=length):
                with _tls_server(
                    self._certificate_directory,
                    response_body=b"x" * length,
                ) as server:
                    endpoint = self._endpoint(server.port)
                    outcome = send_provider_request(
                        endpoint=endpoint,
                        operation=ProviderOperation.JOB_INPUT_READ,
                        request=_signed_request(
                            authority=endpoint.authority,
                            method="GET",
                            path="/provider/v1/jobs/job:test/input",
                            query="analysis_kind_ref=mol_from_1h_peaks",
                            body=None,
                        ),
                    )
                self.assertIs(type(outcome), expected_type, repr(outcome))

    def test_aggregate_read_deadline_rejects_a_drip_response(self) -> None:
        with _tls_server(
            self._certificate_directory,
            response_body=b"four",
            drip_seconds=0.08,
        ) as server:
            outcome = self._send_inventory(server.port, io_deadline_seconds=0.12)
        self.assertEqual(
            outcome,
            ProviderResponseRejected(ResponseRejection.RESPONSE_BODY_INCOMPLETE, 200),
        )
        self.assertIsInstance(outcome.cause, TimeoutError)

    def test_close_before_status_leaves_delivery_possible(self) -> None:
        with _tls_server(self._certificate_directory, close_before_status=True) as server:
            outcome = self._send_inventory(server.port)
        self.assertEqual(
            outcome,
            ProviderRequestUnavailable(RequestDelivery.POSSIBLE),
        )
        self.assertIsInstance(outcome.cause, http.client.HTTPException)
        self.assertEqual(len(server.requests), 1)

    def test_start_problem_preserves_commit_uncertainty_across_tls(self) -> None:
        document = {
            "type": "urn:nmr-api:problem:service-unavailable",
            "title": "Service unavailable",
            "status": 503,
            "instance": "/provider/v1/problems/test",
            "request_id": "body-request",
        }
        headers = _valid_response_headers(
            content_type="application/problem+json",
            request_id="header-request",
        )
        with _tls_server(
            self._certificate_directory,
            status=503,
            response_headers=headers,
            response_body=json.dumps(document, separators=(",", ":")).encode(),
        ) as server:
            endpoint = self._endpoint(server.port)
            prepared = prepare_execution_attempt_start(
                job_ref="job:test",
                provider_attempt_key="stable-key",
            )
            transport_outcome = send_provider_request(
                endpoint=endpoint,
                operation=ProviderOperation.EXECUTION_ATTEMPT_START,
                request=sign_prepared_provider_request(
                    prepared,
                    private_key=_PRIVATE_KEY,
                    credential_ref="credential:provider:test",
                    authority=endpoint.authority,
                    created=1_700_000_000,
                    nonce=bytes(range(16)),
                ),
            )

        outcome = interpret_execution_attempt_start(
            prepared,
            transport_outcome,
            expected_provider_ref="provider:test",
            expected_analysis_kind_ref="mol_from_1h_peaks",
        )
        self.assertIs(type(outcome), AttemptMutationCommitPossible)
        self.assertIs(type(outcome.evidence), ProviderProblem)
        self.assertEqual(outcome.evidence.status, 503)

    def _endpoint(
        self,
        port: int,
        *,
        io_deadline_seconds: float = 1,
    ) -> ProviderHttpsEndpoint:
        return ProviderHttpsEndpoint(
            origin=f"https://localhost:{port}",
            expected_topology="dev-local",
            connect_timeout_seconds=1,
            io_deadline_seconds=io_deadline_seconds,
            ca_file=self._certificate_directory / "ca.pem",
        )

    def _send_inventory(
        self,
        port: int,
        *,
        io_deadline_seconds: float = 1,
    ):
        endpoint = self._endpoint(
            port,
            io_deadline_seconds=io_deadline_seconds,
        )
        return send_provider_request(
            endpoint=endpoint,
            operation=ProviderOperation.EXECUTION_ATTEMPTS_LIST,
            request=_signed_request(
                authority=endpoint.authority,
                method="GET",
                path="/provider/v1/execution-attempts",
                query="state=in_progress",
                body=None,
            ),
        )


def _signed_request(
    *,
    authority: str,
    method: str,
    path: str,
    query: str,
    body: bytes | None,
):
    return sign_provider_request(
        private_key=_PRIVATE_KEY,
        credential_ref="credential:provider:test",
        method=method,
        authority=authority,
        path=path,
        query=query,
        body=body,
        created=1_700_000_000,
        nonce=bytes(range(16)),
    )


def _valid_response_headers(
    *,
    content_type: str = "application/json",
    request_id: str | None = None,
) -> dict[str, str]:
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        "Nmr-Api-Topology": "dev-local",
    }
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return headers


class _QuietThreadingHttpServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        pass


@contextmanager
def _tls_server(
    certificate_directory: Path,
    *,
    status: int = 200,
    response_headers: dict[str, str] | None = None,
    response_body: bytes = b"{}",
    close_before_status: bool = False,
    drip_seconds: float | None = None,
):
    requests: list[dict[str, str | bytes]] = []
    headers = _valid_response_headers() if response_headers is None else response_headers

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def do_PUT(self) -> None:
            self._serve()

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "requestline": self.requestline,
                    "host": self.headers.get("Host", ""),
                    "signature-input": self.headers.get("Signature-Input", ""),
                    "content-length": self.headers.get("Content-Length", ""),
                    "content-digest": self.headers.get("Content-Digest", ""),
                    "body": self.rfile.read(length),
                }
            )
            if close_before_status:
                self.close_connection = True
                return
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if drip_seconds is None:
                self.wfile.write(response_body)
            else:
                for byte in response_body:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    sleep(drip_seconds)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = _QuietThreadingHttpServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certificate_directory / "server.pem",
        certificate_directory / "server-key.pem",
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.requests = requests
    server.port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    unittest.main()
