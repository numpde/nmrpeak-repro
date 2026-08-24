"""Stateful TLS Server A fake for complete CHF provider lifecycle proofs."""

from __future__ import annotations

from base64 import b64decode, b64encode
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
from threading import Lock, Thread

from nmrpeak_provider.provider_requests import (
    prepare_execution_attempt_complete,
    prepare_execution_attempt_progress,
    prepare_execution_attempt_start,
)


_ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64
_ANALYSIS_RESULT_REF = "analysis_result:sha256:" + "c" * 64
@dataclass(slots=True)
class _Attempt:
    job_ref: str
    provider_attempt_key: str
    state: str = "in_progress"
    job_state: str = "open"
    progress_phase: str | None = None
    terminal_body: bytes | None = None
    terminal_receipt: dict[str, object] | None = None


class ChfServerA:
    """Own the minimal remote state observed by one CHF lifecycle proof."""

    def __init__(
        self,
        *,
        canonical_input: bytes,
    ) -> None:
        self.canonical_input = canonical_input
        self.requests: list[tuple[str, str, bytes]] = []
        self.failures: list[str] = []
        self.attempt: _Attempt | None = None
        self._lock = Lock()

    @property
    def input_fingerprint(self) -> str:
        return "sha256:" + sha256(self.canonical_input).hexdigest()

    def serve(self, method: str, raw_target: str, headers, body: bytes):
        """Verify one signed request before applying its Server A transition."""

        try:
            with self._lock:
                self._require_signed_request(headers, body)
                self.requests.append((method, raw_target, body))
                return 200, self._dispatch(method, raw_target, body)
        except Exception:
            # This fake is an assertion boundary. Keep signed bodies and model
            # input out of failure evidence while making any drift fail the test.
            self.failures.append("invalid_provider_request")
            return 400, {
                "type": "urn:nmr-api:problem:bad-request",
                "title": "Bad request",
                "status": 400,
                "instance": "/provider/v1/problems/fake-server",
                "request_id": "fake-server-request",
                "code": "invalid_request",
            }

    def _require_signed_request(self, headers, body: bytes) -> None:
        # Released signing vectors own RFC 9421 parity. This lifecycle lane only
        # proves that production composition sends the signed request class.
        required = {"Host", "Signature", "Signature-Input"}
        if body:
            required |= {"Content-Digest", "Content-Type"}
        if any(not headers.get(name) for name in required):
            raise ValueError("provider request is not signed")

    def _dispatch(self, method: str, raw_target: str, body: bytes) -> dict[str, object]:
        if method == "GET" and raw_target == (
            "/provider/v1/jobs?analysis_kind_ref=mol_from_1h_13c_formula"
            "&has_provider_execution_attempt=false&limit=50"
        ):
            return self._jobs_page()
        if method == "GET" and raw_target == (
            "/provider/v1/jobs/job:selected/input"
            "?analysis_kind_ref=mol_from_1h_13c_formula"
        ):
            return self._job_input()
        if method == "POST" and raw_target == "/provider/v1/execution-attempts/start":
            return self._start(body)
        if self.attempt is not None and raw_target == (
            f"/provider/v1/execution-attempts/{_ATTEMPT_REF}/progress"
        ) and method == "PUT":
            return self._progress(body)
        if self.attempt is not None and raw_target == (
            f"/provider/v1/execution-attempts/{_ATTEMPT_REF}"
        ) and method == "GET":
            return self._attempt_snapshot()
        if method == "POST" and raw_target == "/provider/v1/execution-attempts/complete":
            return self._complete(body)
        raise AssertionError(f"unexpected provider request: {method} {raw_target}")

    def _jobs_page(self) -> dict[str, object]:
        return {
            "schema_id": "nmr.provider.jobs.list.response.v1",
            "analysis_kind_ref": "mol_from_1h_13c_formula",
            "has_provider_execution_attempt": False,
            "jobs": [
                {
                    "job_ref": "job:selected",
                    "analysis_kind_ref": "mol_from_1h_13c_formula",
                    "input_fingerprint": self.input_fingerprint,
                    "input_schema_id": "nmr.job.specification.text.v1",
                    "input_byte_length": len(self.canonical_input),
                    "created_at": "2026-08-24T12:00:00Z",
                }
            ],
            "next_cursor": None,
        }

    def _job_input(self) -> dict[str, object]:
        return {
            "schema_id": "nmr.provider.job_input.read.response.v1",
            "job_ref": "job:selected",
            "input_fingerprint": self.input_fingerprint,
            "input_schema_id": "nmr.job.specification.text.v1",
            "input_byte_length": len(self.canonical_input),
            "canonical_input_base64": b64encode(self.canonical_input).decode("ascii"),
        }

    def _start(self, body: bytes) -> dict[str, object]:
        command = _json_command(body)
        expected = prepare_execution_attempt_start(
            job_ref=command["job_ref"],
            provider_attempt_key=command["provider_attempt_key"],
        )
        assert expected.body == body and command["job_ref"] == "job:selected"
        replayed = self.attempt is not None
        if self.attempt is None:
            self.attempt = _Attempt(
                job_ref=command["job_ref"],
                provider_attempt_key=command["provider_attempt_key"],
            )
        else:
            assert command["provider_attempt_key"] == self.attempt.provider_attempt_key, (
                "start conflicted with the retained provider Attempt key"
            )
        return {
            "schema_id": "nmr.provider.execution_attempt_start_response.v1",
            "execution_attempt_ref": _ATTEMPT_REF,
            "job_ref": self.attempt.job_ref,
            "analysis_kind_ref": "mol_from_1h_13c_formula",
            "provider_ref": "provider:nmrpeak",
            "state": self.attempt.state,
            "started_at": "2026-08-24T12:00:00Z",
            "replayed": replayed,
        }

    def _progress(self, body: bytes) -> dict[str, object]:
        assert self.attempt is not None and self.attempt.state == "in_progress"
        command = _json_command(body)
        expected = prepare_execution_attempt_progress(
            execution_attempt_ref=_ATTEMPT_REF,
            phase=command["phase"],
            condition_code=command["condition_code"],
        )
        assert expected.body == body
        self.attempt.progress_phase = command["phase"]
        return {
            "schema_id": "nmr.provider.execution_attempt_progress_response.v1",
            "execution_attempt_ref": _ATTEMPT_REF,
            "phase": command["phase"],
            "condition_code": None,
            "updated_at": "2026-08-24T12:01:00Z",
        }

    def _attempt_snapshot(self) -> dict[str, object]:
        assert self.attempt is not None
        return {
            "schema_id": "nmr.provider.execution_attempt_read_response.v1",
            "execution_attempt_ref": _ATTEMPT_REF,
            "job_ref": self.attempt.job_ref,
            "state": self.attempt.state,
            "job_state": self.attempt.job_state,
        }

    def _complete(self, body: bytes) -> dict[str, object]:
        assert self.attempt is not None
        if self.attempt.terminal_body is not None:
            assert body == self.attempt.terminal_body, "completion conflicted with retained bytes"
            assert self.attempt.terminal_receipt is not None
            return self.attempt.terminal_receipt | {"replayed": True}
        command = _json_command(body)
        result = b64decode(command["canonical_result_base64"], validate=True)
        expected = prepare_execution_attempt_complete(
            execution_attempt_ref=command["execution_attempt_ref"],
            result_schema_id=command["result_schema_id"],
            canonical_result=result,
        )
        assert expected.body == body and command["execution_attempt_ref"] == _ATTEMPT_REF
        self.attempt.state = "succeeded"
        self.attempt.job_state = "closed"
        self.attempt.terminal_body = body
        self.attempt.terminal_receipt = {
            "schema_id": "nmr.provider.execution_attempt_complete_response.v1",
            "execution_attempt_ref": _ATTEMPT_REF,
            "analysis_result_ref": _ANALYSIS_RESULT_REF,
            "result_schema_id": command["result_schema_id"],
            "result_fingerprint": "sha256:" + sha256(result).hexdigest(),
            "result_byte_length": len(result),
            "committed_at": "2026-08-24T12:02:00Z",
            "replayed": False,
        }
        return self.attempt.terminal_receipt


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        pass


@contextmanager
def serve_chf_server_a(*, state: ChfServerA, certificate_directory: Path):
    """Serve one CHF fake over a real localhost TLS connection."""

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
            body = self.rfile.read(length)
            status, document = state.serve(
                self.command,
                self.path,
                self.headers,
                body,
            )
            response = json.dumps(document, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json" if status == 200 else "application/problem+json",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Nmr-Api-Topology", "dev-local")
            if status != 200:
                self.send_header("X-Request-ID", "fake-server-request")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = _QuietServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(
        certificate_directory / "server.pem",
        certificate_directory / "server-key.pem",
    )
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _json_command(body: bytes) -> dict[str, object]:
    document = json.loads(body)
    assert type(document) is dict, "provider command is not a JSON object"
    return document
