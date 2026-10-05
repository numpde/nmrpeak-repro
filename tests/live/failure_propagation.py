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


class LiveTestError(RuntimeError):
    pass


class HttpStatusError(LiveTestError):
    def __init__(self, status: int, evidence: str) -> None:
        super().__init__(evidence)
        self.status = status


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
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
    finally:
        if temporary.exists():
            temporary.unlink()


class SignedApi:
    def __init__(self, origin: str, credential_path: Path, topology: str,
                 ca_certificate: Path | None, timeout: float) -> None:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise LiveTestError("API origin must be an HTTPS origin without path or query")
        self.origin = f"https://{parsed.hostname.lower()}" + (f":{parsed.port}" if parsed.port and parsed.port != 443 else "")
        self.authority = self.origin.removeprefix("https://")
        self.host, self.port, self.timeout = parsed.hostname, parsed.port or 443, timeout
        self.context = ssl.create_default_context(cafile=str(ca_certificate) if ca_certificate else None)
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
        self.credential_ref = credential["credential_ref"]
        self.actor_ref = credential["principal_ref"]
        key = load_pem_private_key(credential["private_key_pkcs8_pem"].encode("ascii"), password=None)
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
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(1_048_577)
            request_id = response.getheader("X-Request-ID", "missing")
            topology = response.getheader("Nmr-Api-Topology")
        finally:
            connection.close()
        if len(payload) > 1_048_576:
            raise LiveTestError(f"oversized API response; request_id={request_id}")
        if topology != self.topology:
            raise LiveTestError(f"unexpected API topology {topology!r}; request_id={request_id}")
        if response.status not in expected:
            digest = hashlib.sha256(payload).hexdigest()
            raise HttpStatusError(
                response.status,
                f"HTTP {response.status}; request_id={request_id}; body_sha256={digest}",
            )
        try:
            result = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
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
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_id") != "nmrpeak.live_failure_propagation.state.v1":
        raise LiveTestError("state has the wrong schema")
    return value


def _assert_binding(state: dict[str, object], api: SignedApi, args: argparse.Namespace) -> None:
    expected = {"api_origin": api.origin, "expected_topology": args.expected_topology,
                "project_ref": args.project_ref, "provider_ref": args.provider_ref,
                "run_label": args.run_label}
    for key, value in expected.items():
        if state.get(key) != value:
            raise LiveTestError(f"state {key} does not match this invocation")


def _all_attempts(api: SignedApi, job_ref: str) -> list[dict[str, object]]:
    attempts: list[dict[str, object]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        query = urlencode({"limit": "100", **({"cursor": cursor} if cursor else {})})
        page = api.request("GET", f"/v1/jobs/{job_ref}/execution-attempts", query)
        if page.get("job_ref") != job_ref or not isinstance(page.get("attempts"), list):
            raise LiveTestError("malformed Attempt-list response")
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
    if not isinstance(job, dict) or any(job.get(k) != v for k, v in expected.items()):
        raise LiveTestError("Job read does not match the persisted create intent")
    if read.get("specification") != request["specification"]:
        raise LiveTestError("Job specification does not match the persisted create intent")
    return job


def _create_and_open(api: SignedApi, state: dict[str, object], path: Path, args: argparse.Namespace) -> None:
    for entry in state["jobs"]:
        if "job_ref" not in entry:
            receipt = api.request("POST", "/v1/jobs", body_text=entry["create_body_text"], expected=(200, 201))
            if (receipt.get("operation_ref") != entry["operation_ref"] or
                    not isinstance(receipt.get("job_ref"), str) or
                    not receipt["job_ref"].startswith("job:")):
                raise LiveTestError("create receipt identity mismatch")
            entry["job_ref"] = receipt.get("job_ref")
            _write_state(path, state)
        job = _validate_job(api, entry, args)
        selection_path = f"/v1/jobs/{entry['job_ref']}/providers"
        selected = api.request("GET", selection_path).get("provider_refs")
        if selected == []:
            try:
                api.request("PUT", selection_path, body_text=_canonical({"provider_refs": [args.provider_ref]}))
            except HttpStatusError as error:
                if 400 <= error.status < 500:
                    raise
                raise LiveTestError(f"provider selection response uncertain; {error}") from error
            except Exception as error:
                raise LiveTestError(
                    f"provider selection response uncertain; rerun to reconcile; {error}"
                ) from error
        elif selected != [args.provider_ref]:
            raise LiveTestError(f"Job has unexpected provider selection: {selected!r}")
        if api.request("GET", selection_path).get("provider_refs") != [args.provider_ref]:
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
            except Exception as error:
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
        raise LiveTestError("Attempt does not carry the expected public failure")
    match = COUNT_PATTERN.match(failure["message"])
    if not match:
        raise LiveTestError("failure does not carry the measured tokenizer count")
    count = int(match.group(1))
    if count <= 511 or failure["message"] != render_direct_runner_rejection(count):
        raise LiveTestError("failure message is not the canonical direct runner rejection")
    return {"execution_attempt_ref": item.get("execution_attempt_ref"), "failure_code": "input_rejected",
            "failure_message": failure["message"], "token_count": count}


def _observe(api: SignedApi, state: dict[str, object], path: Path, args: argparse.Namespace) -> None:
    deadline = monotonic() + args.wait_seconds
    while True:
        complete = True
        for entry in state["jobs"]:
            _validate_job(api, entry, args)
            matches = [item for item in _all_attempts(api, entry["job_ref"])
                       if item.get("provider_ref") == args.provider_ref]
            failed = [item for item in matches if item.get("state") == "failed"]
            if len(matches) > 1:
                raise LiveTestError(
                    f"Job {entry['job_ref']} has multiple Attempts from {args.provider_ref}"
                )
            if len(matches) == 1 and len(failed) == 1:
                entry["evidence"] = _failure_evidence(failed[0], args.provider_ref)
            elif matches:
                complete = False
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
    api = SignedApi(args.api_origin, args.credential, args.expected_topology,
                    args.ca_certificate, args.request_timeout)
    if args.state.exists():
        state = _load(args.state)
    elif args.mode == "observe":
        raise LiveTestError("observe requires an existing state file")
    else:
        state = _new_state(api, args)
        _write_state(args.state, state)  # exact create bytes precede the first POST
    _assert_binding(state, api, args)
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
