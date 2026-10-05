#!/usr/bin/env python3
"""Create and observe durable live evidence for NMRPeak failure propagation.

The signing code is copied and adapted from nmr-api-v1's reviewed
``nmr_api/discovery_examples/sign_request.py``.  Mutating requests are
reconciled from signed reads after an uncertain response; create is the sole
exception because its operation_ref makes an exact byte-for-byte replay safe.
"""

from __future__ import annotations

import argparse
from base64 import b64encode, urlsafe_b64encode
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
from secrets import token_bytes
import ssl
import stat
import sys
from time import monotonic, sleep, time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from nmrpeak_provider.input_issue_message import render_direct_runner_rejection
from nmrpeak_provider.product import HF_OFFERING, NMRPEAK_PRODUCT
from nmrpeak_provider.product_input import parse_job_input

LANES = (("hf", "mol_from_1h_peaks", HF_OFFERING),
         ("chf", "mol_from_1h_13c_formula", NMRPEAK_PRODUCT.offerings[1]))
COUNT_PATTERN = re.compile(r"^Input rejected: this complete input produced ([0-9]+) tokenizer tokens;")
JOB_REF_PATTERN = re.compile(r"^job:[A-Za-z0-9_.-]{1,124}$")
ATTEMPT_REF_PATTERN = re.compile(r"^execution_attempt:sha256:[0-9a-f]{64}$")
FAILURE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
JOB_READ_SCHEMA_ID = "nmr.job.read.response.v1"
PROVIDER_SELECTION_SCHEMA_ID = "nmr.job.provider_selection.response.v1"
ATTEMPT_LIST_SCHEMA_ID = "nmr.job.execution_attempt.list.response.v1"
JOB_CREATE_SCHEMA_ID = "nmr.job.create.response.v1"


class LiveTestError(RuntimeError):
    pass


class HttpStatusError(LiveTestError):
    def __init__(self, status: int, evidence: str) -> None:
        super().__init__(evidence)
        self.status = status


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def _strict_json(value: bytes | str) -> object:
    return json.loads(
        value,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _public_problem_evidence(
    payload: bytes, *, content_type: str | None, status: int, request_id: str
) -> dict[str, str]:
    """Return only a correlated, closed, bounded public Problem envelope."""
    if content_type != "application/problem+json" or not 1 <= len(payload) <= 4096:
        return {}
    try:
        problem = _strict_json(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError, RecursionError):
        return {}
    if (not isinstance(problem, dict) or
            set(problem) != {"instance", "request_id", "status", "title", "type", "code", "detail"} or
            type(problem.get("status")) is not int or problem["status"] != status or
            problem.get("request_id") != request_id):
        return {}
    limits = {"type": 256, "title": 128, "code": 128, "detail": 1024}
    result: dict[str, str] = {}
    for key, limit in limits.items():
        value = problem.get(key)
        try:
            encoded = value.encode("utf-8") if isinstance(value, str) else b""
        except UnicodeError:
            return {}
        if not isinstance(value, str) or not value or len(encoded) > limit:
            return {}
        result[key] = value
    for key, limit in (("instance", 256), ("request_id", 128)):
        value = problem.get(key)
        try:
            encoded = value.encode("utf-8") if isinstance(value, str) else b""
        except UnicodeError:
            return {}
        if not isinstance(value, str) or not value or len(encoded) > limit:
            return {}
    return result


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _is_job_ref(value: object) -> bool:
    return isinstance(value, str) and JOB_REF_PATTERN.fullmatch(value) is not None


def _input_document(lane: str, peak_count: int) -> str:
    proton = [{"shift_lo": str(1 + (i % 80) / 10),
               "shift_hi": str(1 + (i % 80) / 10),
               "integral": "1", "multiplicity": "m", "j_hz": []}
              for i in range(peak_count)]
    spectra: dict[str, object] = {"1H": {"peaks": proton}}
    if lane == "chf":
        spectra["13C"] = {"peaks": [{"shift": str(10 + i % 190)} for i in range(peak_count)]}
    return _canonical({"schema_id": "nmrpeak.structure_generation.request.v1",
                       "model_input": {"formula": "C20H40", "spectra": spectra}})


def _state_permissions(path: Path, *, must_exist: bool) -> None:
    if path.is_symlink():
        raise LiveTestError(f"state path must not be a symlink: {path}")
    if must_exist and not path.is_file():
        raise LiveTestError(f"state file does not exist: {path}")
    if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise LiveTestError(f"state file must be owner-only: {path}")
    parent = path.parent
    if not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise LiveTestError(f"state directory must exist and be owner-only: {parent}")


def _write_state(path: Path, value: object) -> None:
    _state_permissions(path, must_exist=False)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise LiveTestError(
            f"durable state write failed: {type(error).__name__}"
        ) from error
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError as error:
            raise LiveTestError(
                f"state staging cleanup failed: {type(error).__name__}"
            ) from error


def _read_private_credential(path: Path) -> bytes:
    """Read one bounded owner-only credential without following the leaf."""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                stat.S_IMODE(metadata.st_mode) & 0o077):
            raise LiveTestError("credential must be an owner-only regular file owned by this user")
        if not 1 <= metadata.st_size <= 65_536:
            raise LiveTestError("credential must contain between 1 and 65536 bytes")
        payload = bytearray()
        while len(payload) < metadata.st_size:
            chunk = os.read(descriptor, metadata.st_size - len(payload))
            if not chunk:
                raise LiveTestError("credential ended before its measured size")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            raise LiveTestError("credential grew while it was being read")
        return bytes(payload)
    except OSError as error:
        raise LiveTestError(f"credential read failed: {type(error).__name__}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def _exclusive_state_lock(path: Path):
    """Prevent two processes from owning one durable create intent."""
    _state_permissions(path, must_exist=False)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise LiveTestError(f"state lock must be an owner-only regular file: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LiveTestError(f"state is already owned by another live-test process: {path}") from error
        yield
    except OSError as error:
        raise LiveTestError(f"state lock failed: {type(error).__name__}") from error
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


class SignedApi:
    def __init__(self, origin: str, credential_path: Path, topology: str,
                 ca_certificate: Path | None, timeout: float) -> None:
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError as error:
            raise LiveTestError("API origin has an invalid port") from error
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise LiveTestError("API origin must be an HTTPS origin without credentials, path, query, or fragment")
        self.origin = f"https://{parsed.hostname.lower()}" + (f":{port}" if port and port != 443 else "")
        self.authority = self.origin.removeprefix("https://")
        self.host, self.port, self.timeout = parsed.hostname, port or 443, timeout
        try:
            self.context = ssl.create_default_context(
                cafile=str(ca_certificate) if ca_certificate else None
            )
        except (OSError, ssl.SSLError) as error:
            raise LiveTestError(f"TLS trust loading failed: {type(error).__name__}") from error
        try:
            credential = _strict_json(_read_private_credential(credential_path).decode("utf-8"))
            if not isinstance(credential, dict):
                raise TypeError("credential is not an object")
            self.credential_ref = credential["credential_ref"]
            self.actor_ref = credential["principal_ref"]
            private_pem = credential["private_key_pkcs8_pem"]
            if not all(isinstance(value, str) for value in (
                self.credential_ref, self.actor_ref, private_pem
            )):
                raise TypeError("credential fields are not text")
            key = load_pem_private_key(private_pem.encode("ascii"), password=None)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise LiveTestError(
                f"signing credential loading failed: {type(error).__name__}"
            ) from error
        if not isinstance(key, Ed25519PrivateKey):
            raise LiveTestError("credential must contain an Ed25519 private key")
        self.key = key
        self.topology = topology

    def request(self, method: str, path: str, query: str = "", body_text: str | None = None,
                expected: tuple[int, ...] = (200,)) -> dict[str, object]:
        body = body_text.encode("utf-8") if body_text is not None else None
        created = int(time())
        components = ["@method", "@authority", "@path", "@query"]
        headers: dict[str, str] = {}
        if body is not None:
            components += ["content-type", "content-digest"]
            headers["content-type"] = "application/json"
            headers["content-digest"] = f"sha-256=:{b64encode(hashlib.sha256(body).digest()).decode('ascii')}:"
        component_list = " ".join(f'"{name}"' for name in components)
        nonce = urlsafe_b64encode(token_bytes(16)).rstrip(b"=").decode("ascii")
        parameters = (f"({component_list});created={created};expires={created + 300};nonce=\"{nonce}\";"
                      f"keyid=\"{self.credential_ref}\";tag=\"nmr-api-v1\"")
        values = {"@method": method, "@authority": self.authority, "@path": path,
                  "@query": "?" + query, **headers}
        base = "\n".join([*(f'"{name}": {values[name]}' for name in components),
                           f'"@signature-params": {parameters}'])
        headers["signature-input"] = "sig1=" + parameters
        headers["signature"] = f"sig1=:{b64encode(self.key.sign(base.encode('ascii'))).decode('ascii')}:"
        connection = http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=self.timeout)
        target = path + (("?" + query) if query else "")
        try:
            try:
                connection.request(method, target, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read(1_048_577)
                request_id = response.getheader("X-Request-ID", "missing")
                topology = response.getheader("Nmr-Api-Topology")
                content_type = response.getheader("Content-Type")
            except (OSError, http.client.HTTPException) as error:
                raise LiveTestError(
                    f"{method} {path} transport failed: {type(error).__name__}"
                ) from error
        finally:
            connection.close()
        if len(payload) > 1_048_576:
            raise LiveTestError(f"oversized API response; request_id={request_id}")
        if topology != self.topology:
            raise LiveTestError(f"unexpected API topology {topology!r}; request_id={request_id}")
        if response.status not in expected:
            digest = hashlib.sha256(payload).hexdigest()
            public_problem = _public_problem_evidence(
                payload,
                content_type=content_type,
                status=response.status,
                request_id=request_id,
            )
            problem_evidence = (
                f"; public_problem={_canonical(public_problem)}"
                if public_problem else ""
            )
            raise HttpStatusError(
                response.status,
                f"HTTP {response.status}; request_id={request_id}; "
                f"body_sha256={digest}{problem_evidence}",
            )
        try:
            result = _strict_json(payload)
        except (UnicodeError, ValueError, RecursionError) as error:
            raise LiveTestError(f"invalid JSON response; request_id={request_id}") from error
        if not isinstance(result, dict):
            raise LiveTestError(f"non-object response; request_id={request_id}")
        return result


def _new_state(api: SignedApi, args: argparse.Namespace) -> dict[str, object]:
    jobs = []
    for lane, analysis_kind, offering in LANES:
        specification = _input_document(lane, args.peak_count)
        parse_job_input(specification.encode(), offering)
        operation_ref = f"operation:live-nmrpeak-{lane}-{uuid4()}"
        unique = operation_ref.rsplit("-", 1)[-1]
        description = f"NMRPeak live failure propagation {args.run_label} {lane.upper()} {unique}"
        body = _canonical({"analysis_kind_ref": analysis_kind, "description": description,
                           "operation_ref": operation_ref, "project_ref": args.project_ref,
                           "specification": specification, "state": "closed"})
        jobs.append({"lane": lane, "analysis_kind_ref": analysis_kind, "description": description,
                     "operation_ref": operation_ref, "create_body_text": body})
    return {"schema_id": "nmrpeak.live_failure_propagation.state.v1", "api_origin": api.origin,
            "expected_topology": args.expected_topology, "project_ref": args.project_ref,
            "provider_ref": args.provider_ref, "run_label": args.run_label, "jobs": jobs}


def _load(path: Path) -> dict[str, object]:
    _state_permissions(path, must_exist=True)
    try:
        value = _strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise LiveTestError(f"state loading failed: {type(error).__name__}") from error
    _validate_state_shape(value, "nmrpeak.live_failure_propagation.state.v1")
    return value


def _validate_state_shape(
    value: object,
    schema_id: str,
    *,
    extra_top_level: frozenset[str] = frozenset(),
) -> None:
    top_level = {
        "schema_id", "api_origin", "expected_topology", "project_ref",
        "provider_ref", "run_label", "jobs",
    } | set(extra_top_level)
    if not isinstance(value, dict) or set(value) != top_level or value.get("schema_id") != schema_id:
        raise LiveTestError("state has the wrong schema or fields")
    for key in ("api_origin", "expected_topology", "project_ref", "provider_ref", "run_label"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise LiveTestError(f"state {key} is invalid")
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 2:
        raise LiveTestError("state must contain exactly two Jobs")
    expected_kinds = {lane: kind for lane, kind, _ in LANES}
    offerings = {lane: offering for lane, _, offering in LANES}
    observed_lanes: set[str] = set()
    base_fields = {
        "lane", "analysis_kind_ref", "description", "operation_ref",
        "create_body_text",
    }
    for entry in jobs:
        if (not isinstance(entry, dict) or not base_fields <= set(entry) or
                set(entry) - base_fields - {"job_ref", "evidence"}):
            raise LiveTestError("state Job fields are invalid")
        lane = entry.get("lane")
        if lane not in expected_kinds or lane in observed_lanes:
            raise LiveTestError("state Job lanes are not exactly HF and CHF")
        observed_lanes.add(lane)
        if entry.get("analysis_kind_ref") != expected_kinds[lane]:
            raise LiveTestError("state Job analysis kind does not match its lane")
        if any(not isinstance(entry.get(key), str) or not entry[key]
               for key in ("description", "operation_ref", "create_body_text")):
            raise LiveTestError("state Job identity fields are invalid")
        if "job_ref" in entry and not _is_job_ref(entry["job_ref"]):
            raise LiveTestError("state Job reference is invalid")
        if "evidence" in entry and not isinstance(entry["evidence"], dict):
            raise LiveTestError("state Job evidence is invalid")
        try:
            request = _strict_json(entry["create_body_text"])
        except (UnicodeError, ValueError, RecursionError) as error:
            raise LiveTestError("state Job create bytes are invalid JSON") from error
        expected_request = {
            "analysis_kind_ref": entry["analysis_kind_ref"],
            "description": entry["description"],
            "operation_ref": entry["operation_ref"],
            "project_ref": value["project_ref"],
            "state": "closed",
        }
        if (not isinstance(request, dict) or set(request) != {*expected_request, "specification"} or
                any(request.get(key) != item for key, item in expected_request.items()) or
                not isinstance(request.get("specification"), str) or
                _canonical(request) != entry["create_body_text"]):
            raise LiveTestError("state Job create intent is inconsistent")
        try:
            parse_job_input(request["specification"].encode("utf-8"), offerings[lane])
        except (UnicodeError, ValueError) as error:
            raise LiveTestError("state Job specification is not admitted by its lane") from error
    if observed_lanes != set(expected_kinds):
        raise LiveTestError("state Job lanes are not exactly HF and CHF")


def _assert_binding(
    state: dict[str, object],
    api: SignedApi,
    args: argparse.Namespace,
    expected_specifications: dict[str, str],
) -> None:
    expected = {"api_origin": api.origin, "expected_topology": args.expected_topology,
                "project_ref": args.project_ref, "provider_ref": args.provider_ref,
                "run_label": args.run_label}
    for key, value in expected.items():
        if state.get(key) != value:
            raise LiveTestError(f"state {key} does not match this invocation")
    for entry in state["jobs"]:
        request = _strict_json(entry["create_body_text"])
        if request["specification"] != expected_specifications[entry["lane"]]:
            raise LiveTestError(
                f"state {entry['lane']} specification does not match this invocation; "
                "use the original inputs or a fresh state path"
            )


def _all_attempts(api: SignedApi, job_ref: str) -> list[dict[str, object]]:
    attempts: list[dict[str, object]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        query = urlencode({"limit": "100", **({"cursor": cursor} if cursor else {})})
        page = api.request("GET", f"/v1/jobs/{job_ref}/execution-attempts", query)
        if (set(page) != {"schema_id", "job_ref", "attempts", "next_cursor"} or
                page.get("schema_id") != ATTEMPT_LIST_SCHEMA_ID or
                page.get("job_ref") != job_ref or not isinstance(page.get("attempts"), list)):
            raise LiveTestError("malformed Attempt-list response")
        for item in page["attempts"]:
            base = {
                "execution_attempt_ref", "provider_ref", "provider_condition",
                "started_at", "state", "updated_at",
            }
            if (not isinstance(item, dict) or
                    set(item) not in (base, base | {"provider_failure"})):
                raise LiveTestError("malformed Attempt-list item")
            condition = item.get("provider_condition")
            if (condition is not None and
                    (not isinstance(condition, dict) or
                     set(condition) != {"code", "observed_at"} or
                     any(not isinstance(condition.get(key), str) for key in condition))):
                raise LiveTestError("malformed Attempt provider condition")
            failure = item.get("provider_failure")
            if (failure is not None and
                    (not isinstance(failure, dict) or set(failure) != {"code", "message"} or
                     any(not isinstance(failure.get(key), str) for key in failure))):
                raise LiveTestError("malformed Attempt provider failure")
            if failure is not None:
                try:
                    failure["message"].encode("utf-8")
                except UnicodeError as error:
                    raise LiveTestError("malformed Attempt provider failure") from error
                if (FAILURE_CODE_PATTERN.fullmatch(failure["code"]) is None or
                        len(failure["code"]) > 128 or not failure["message"] or
                        len(failure["message"]) > 1024 or "\0" in failure["message"]):
                    raise LiveTestError("malformed Attempt provider failure")
            if (item.get("state") == "failed") != (failure is not None):
                raise LiveTestError("Attempt state and provider failure disagree")
            state = item.get("state")
            if state not in {"in_progress", "succeeded", "failed", "expired"}:
                raise LiveTestError("Attempt state is invalid")
            if state != "in_progress" and condition is not None:
                raise LiveTestError("terminal Attempt carries a provider condition")
            for key in ("execution_attempt_ref", "provider_ref", "started_at", "updated_at"):
                if not isinstance(item.get(key), str) or not item[key]:
                    raise LiveTestError("Attempt identity or timestamp is invalid")
            if ATTEMPT_REF_PATTERN.fullmatch(item["execution_attempt_ref"]) is None:
                raise LiveTestError("Attempt reference is invalid")
        attempts.extend(page["attempts"])
        cursor = page.get("next_cursor")
        if cursor is None:
            return attempts
        if not isinstance(cursor, str):
            raise LiveTestError("malformed Attempt-list cursor")
        if cursor in seen_cursors or len(seen_cursors) >= 100:
            raise LiveTestError("Attempt-list pagination did not make bounded progress")
        seen_cursors.add(cursor)


def _validate_job(api: SignedApi, entry: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    read = api.request("GET", f"/v1/jobs/{entry['job_ref']}")
    job = read.get("job")
    request = json.loads(entry["create_body_text"])
    expected = {"job_ref": entry["job_ref"], "operation_ref": entry["operation_ref"],
                "project_ref": args.project_ref, "analysis_kind_ref": entry["analysis_kind_ref"],
                "description": entry["description"], "submitter_ref": api.actor_ref}
    if (set(read) != {"schema_id", "job", "specification"} or
            read.get("schema_id") != JOB_READ_SCHEMA_ID or not isinstance(job, dict) or
            set(job) != {
                "submitter_ref", "created_at", "analysis_kind_ref", "job_ref",
                "operation_ref", "project_ref", "request_fingerprint",
                "specification_fingerprint", "description", "state",
            } or any(job.get(k) != v for k, v in expected.items())):
        raise LiveTestError("Job read does not match the persisted create intent")
    if read.get("specification") != request["specification"]:
        raise LiveTestError("Job specification does not match the persisted create intent")
    return job


def _provider_selection(api: SignedApi, path: str) -> list[str]:
    response = api.request("GET", path)
    if (set(response) != {"schema_id", "provider_refs"} or
            response.get("schema_id") != PROVIDER_SELECTION_SCHEMA_ID or
            not isinstance(response.get("provider_refs"), list) or
            any(not isinstance(item, str) for item in response["provider_refs"])):
        raise LiveTestError("malformed provider-selection response")
    return response["provider_refs"]


def _create_and_open(api: SignedApi, state: dict[str, object], path: Path, args: argparse.Namespace) -> None:
    for entry in state["jobs"]:
        if "job_ref" not in entry:
            try:
                receipt = api.request(
                    "POST", "/v1/jobs", body_text=entry["create_body_text"],
                    expected=(200, 201),
                )
            except HttpStatusError as error:
                if 400 <= error.status < 500:
                    raise
                raise LiveTestError(
                    "Job-create outcome is unconfirmed; exact request bytes are retained; "
                    f"rerun with this state to replay safely; {error}"
                ) from error
            except LiveTestError as error:
                raise LiveTestError(
                    "Job-create outcome is unconfirmed; exact request bytes are retained; "
                    f"rerun with this state to replay safely; {error}"
                ) from error
            receipt_fields = {
                "schema_id", "submitter_ref", "analysis_kind_ref", "created_at",
                "job_ref", "operation_ref", "project_ref", "replayed",
                "request_fingerprint", "specification_fingerprint",
            }
            if (set(receipt) != receipt_fields or
                    receipt.get("schema_id") != JOB_CREATE_SCHEMA_ID or
                    receipt.get("submitter_ref") != api.actor_ref or
                    receipt.get("analysis_kind_ref") != entry["analysis_kind_ref"] or
                    receipt.get("project_ref") != args.project_ref or
                    type(receipt.get("replayed")) is not bool or
                    any(not isinstance(receipt.get(key), str) or not receipt[key]
                        for key in ("created_at", "request_fingerprint", "specification_fingerprint")) or
                    receipt.get("operation_ref") != entry["operation_ref"] or
                    not _is_job_ref(receipt.get("job_ref"))):
                raise LiveTestError(
                    "Job-create outcome is unconfirmed because its receipt is malformed; "
                    "exact request bytes are retained; rerun with this state to replay safely"
                )
            entry["job_ref"] = receipt.get("job_ref")
            _write_state(path, state)
        job = _validate_job(api, entry, args)
        selection_path = f"/v1/jobs/{entry['job_ref']}/providers"
        selected = _provider_selection(api, selection_path)
        if selected == []:
            try:
                api.request("PUT", selection_path, body_text=_canonical({"provider_refs": [args.provider_ref]}))
            except HttpStatusError as error:
                if 400 <= error.status < 500:
                    raise
                raise LiveTestError(f"provider selection response uncertain; {error}") from error
            except LiveTestError as error:
                raise LiveTestError(
                    f"provider selection response uncertain; rerun to reconcile; {error}"
                ) from error
        elif selected != [args.provider_ref]:
            raise LiveTestError(f"Job has unexpected provider selection: {selected!r}")
        if _provider_selection(api, selection_path) != [args.provider_ref]:
            raise LiveTestError("provider selection was not committed exactly")
        attempts = _all_attempts(api, entry["job_ref"])
        if any(item.get("provider_ref") == args.provider_ref for item in attempts):
            continue
        if job.get("state") == "closed":
            try:
                api.request("PUT", f"/v1/jobs/{entry['job_ref']}/state",
                            body_text=_canonical({"state": "open"}))
            except HttpStatusError as error:
                if 400 <= error.status < 500:
                    raise
                raise LiveTestError(f"Job-open response uncertain; {error}") from error
            except LiveTestError as error:
                raise LiveTestError(
                    f"Job-open response uncertain; rerun to reconcile; {error}"
                ) from error
        elif job.get("state") != "open":
            raise LiveTestError(f"Job cannot be opened from state {job.get('state')!r}")
        opened = _validate_job(api, entry, args)
        if opened.get("state") != "open":
            raise LiveTestError("Job open was not confirmed by a signed read")


def _failure_evidence(item: dict[str, object], provider_ref: str) -> dict[str, object]:
    failure = item.get("provider_failure")
    if item.get("provider_ref") != provider_ref or item.get("state") != "failed" or not isinstance(failure, dict):
        raise LiveTestError("Attempt is not the expected provider's failed Attempt")
    if failure.get("code") != "input_rejected" or not isinstance(failure.get("message"), str):
        raise LiveTestError(
            f"Attempt does not carry the expected public failure: {_canonical(_failure_summary(item))}"
        )
    match = COUNT_PATTERN.match(failure["message"])
    if not match:
        raise LiveTestError(
            f"failure does not carry the measured tokenizer count: {_canonical(_failure_summary(item))}"
        )
    count = int(match.group(1))
    if count <= 511 or failure["message"] != render_direct_runner_rejection(count):
        raise LiveTestError("failure message is not the canonical direct runner rejection")
    return {"execution_attempt_ref": item.get("execution_attempt_ref"), "failure_code": "input_rejected",
            "failure_message": failure["message"], "token_count": count}


def _failure_summary(item: dict[str, object]) -> dict[str, object]:
    failure = item.get("provider_failure")
    if not isinstance(failure, dict):
        return {"execution_attempt_ref": item.get("execution_attempt_ref")}
    message = failure.get("message")
    raw = message.encode("utf-8") if isinstance(message, str) else b""
    return {
        "execution_attempt_ref": item.get("execution_attempt_ref"),
        "failure_code": failure.get("code"),
        "message_byte_length": len(raw),
        "message_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _negative_attempt_outcome(
    item: dict[str, object], provider_ref: str, lane: str
) -> dict[str, object] | None:
    state = item.get("state")
    if state == "failed":
        return _failure_evidence(item, provider_ref)
    if state == "in_progress":
        return None
    raise LiveTestError(
        f"negative {lane} Attempt {item.get('execution_attempt_ref')} "
        f"ended in state {state}"
    )


def _reject_cancelled_without_attempt(
    job: dict[str, object], matches: list[dict[str, object]], lane: str, job_ref: str
) -> None:
    if not matches and job.get("state") == "cancelled":
        raise LiveTestError(
            f"{lane} Job {job_ref} was cancelled before the provider started an Attempt"
        )


def _observe(api: SignedApi, state: dict[str, object], path: Path, args: argparse.Namespace) -> None:
    deadline = monotonic() + args.wait_seconds
    while True:
        complete = True
        for entry in state["jobs"]:
            job = _validate_job(api, entry, args)
            matches = [item for item in _all_attempts(api, entry["job_ref"])
                       if item.get("provider_ref") == args.provider_ref]
            _reject_cancelled_without_attempt(
                job, matches, f"negative {entry['lane']}", entry["job_ref"]
            )
            if len(matches) > 1:
                raise LiveTestError(
                    f"Job {entry['job_ref']} has multiple Attempts from {args.provider_ref}"
                )
            if len(matches) == 1:
                evidence = _negative_attempt_outcome(
                    matches[0], args.provider_ref, entry["lane"]
                )
                if evidence is None:
                    complete = False
                else:
                    entry["evidence"] = evidence
            else:
                complete = False
        _write_state(path, state)
        if complete:
            for entry in state["jobs"]:
                evidence = entry["evidence"]
                print(_canonical({"lane": entry["lane"], "job_ref": entry["job_ref"],
                                  "execution_attempt_ref": evidence["execution_attempt_ref"],
                                  "failure_code": evidence["failure_code"], "token_count": evidence["token_count"]}))
            return
        if monotonic() >= deadline:
            raise LiveTestError("timed out waiting for both failed Attempts")
        sleep(args.poll_seconds)


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "create", "observe"))
    parser.add_argument("--api-origin", required=True)
    parser.add_argument("--expected-topology", choices=("dev-local", "dev", "web"), required=True)
    parser.add_argument("--credential", type=Path, required=True)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--provider-ref", required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--ca-certificate", type=Path)
    parser.add_argument("--confirm-persistent-jobs", action="store_true")
    parser.add_argument("--peak-count", type=int, default=220)
    parser.add_argument("--wait-seconds", type=float, default=300)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--request-timeout", type=float, default=15)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _arguments(argv)
    if args.mode in ("run", "create") and not args.confirm_persistent_jobs:
        raise LiveTestError("creating persistent Jobs requires --confirm-persistent-jobs")
    with _exclusive_state_lock(args.state):
        api = SignedApi(args.api_origin, args.credential, args.expected_topology,
                        args.ca_certificate, args.request_timeout)
        if args.state.exists():
            state = _load(args.state)
        elif args.mode == "observe":
            raise LiveTestError("observe requires an existing state file")
        else:
            state = _new_state(api, args)
            _write_state(args.state, state)  # exact create bytes precede the first POST
        _assert_binding(
            state,
            api,
            args,
            {lane: _input_document(lane, args.peak_count) for lane, _, _ in LANES},
        )
        if args.mode in ("run", "create"):
            _create_and_open(api, state, args.state, args)
            _write_state(args.state, state)
        if args.mode in ("run", "observe"):
            _observe(api, state, args.state, args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except LiveTestError as error:
        print(f"live failure propagation test failed: {error}", file=sys.stderr)
        raise SystemExit(1)
