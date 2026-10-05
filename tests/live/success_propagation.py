#!/usr/bin/env python3
"""Create and verify successful deployed HF and CHF NMRPeak Jobs.

This is the positive companion to ``failure_propagation``. It deliberately
reuses that test's reviewed signing, durable-state, exact-create replay, and
mutation-reconciliation boundaries.
"""

from __future__ import annotations

import argparse
from base64 import b64decode, b64encode
import hashlib
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from urllib.parse import urlencode
from uuid import uuid4

from nmrpeak_provider.product import HF_OFFERING, NMRPEAK_PRODUCT
from nmrpeak_provider.product_input import parse_job_input
from nmrpeak_provider.product_result import (
    CHF_RESULT_IDENTITY,
    HF_RESULT_IDENTITY,
    MAX_RESULT_BYTES,
    ProviderResultFacts,
    RESULT_SCHEMA_ID,
    RunnerResultRejected,
    canonical_result_bytes,
)
from nmrpeak_provider.canonical_json import CanonicalJsonError, parse_canonical_json_bytes
from nmrpeak_provider.chf_runner_protocol import CHF_RUNNER_CONTRACT_ID
from nmrpeak_provider.hf_runner_protocol import HF_RUNNER_CONTRACT_ID
from tests.live.failure_propagation import (
    LANES,
    LiveTestError,
    SignedApi,
    _assert_binding,
    _canonical,
    _exclusive_state_lock,
    _failure_summary,
    _reject_cancelled_without_attempt,
    _create_and_open,
    _state_permissions,
    _strict_json,
    _validate_job,
    _validate_state_shape,
    _write_state,
)

STATE_SCHEMA_ID = "nmrpeak.live_success_propagation.state.v1"
RESULT_LIST_SCHEMA_ID = "nmr.job.analysis_result.list.response.v1"
RESULT_READ_SCHEMA_ID = "nmr.job.analysis_result.read.response.v1"
EXPECTED_RUNNER_CONTRACTS = {
    "hf": HF_RUNNER_CONTRACT_ID,
    "chf": CHF_RUNNER_CONTRACT_ID,
}
RESULT_REF_PATTERN = re.compile(r"^analysis_result:sha256:[0-9a-f]{64}$")


def _new_state(api: SignedApi, args: argparse.Namespace) -> dict[str, object]:
    jobs = []
    offerings = {"hf": HF_OFFERING, "chf": NMRPEAK_PRODUCT.offerings[1]}
    for lane, analysis_kind, _ in LANES:
        specification = _success_input_document(lane)
        parse_job_input(specification.encode("utf-8"), offerings[lane])
        operation_ref = f"operation:live-nmrpeak-success-{lane}-{uuid4()}"
        unique = operation_ref.rsplit("-", 1)[-1]
        description = f"NMRPeak live success {args.run_label} {lane.upper()} {unique}"
        body = _canonical({
            "analysis_kind_ref": analysis_kind,
            "description": description,
            "operation_ref": operation_ref,
            "project_ref": args.project_ref,
            "specification": specification,
            "state": "closed",
        })
        jobs.append({
            "lane": lane,
            "analysis_kind_ref": analysis_kind,
            "description": description,
            "operation_ref": operation_ref,
            "create_body_text": body,
        })
    return {
        "schema_id": STATE_SCHEMA_ID,
        "api_origin": api.origin,
        "expected_topology": args.expected_topology,
        "project_ref": args.project_ref,
        "provider_ref": args.provider_ref,
        "run_label": args.run_label,
        "expected_artifacts": _expected_artifacts(args),
        "jobs": jobs,
    }


def _success_input_document(lane: str) -> str:
    """Return the small admitted vector copied from the binding seam tests."""

    spectra: dict[str, object] = {
        "1H": {
            "peaks": [
                {
                    "shift_lo": "3.71", "shift_hi": "3.68", "integral": "3",
                    "multiplicity": "t", "j_hz": ["1.0", "7.1"],
                },
                {
                    "shift_lo": "4.91", "shift_hi": "4.99", "integral": "2",
                    "multiplicity": "m", "j_hz": [],
                },
            ]
        }
    }
    if lane == "chf":
        spectra["13C"] = {
            "peaks": [{"shift": "70.0"}, {"shift": "109.4"}, {"shift": "109.4"}]
        }
    return _canonical({
        "schema_id": "nmrpeak.structure_generation.request.v1",
        "model_input": {"formula": "O3H16C17N2", "spectra": spectra},
    })


def _load(path: Path) -> dict[str, object]:
    _state_permissions(path, must_exist=True)
    try:
        value = _strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise LiveTestError(f"state loading failed: {type(error).__name__}") from error
    _validate_state_shape(
        value, STATE_SCHEMA_ID, extra_top_level=frozenset({"expected_artifacts"})
    )
    if not isinstance(value.get("expected_artifacts"), dict):
        raise LiveTestError("state expected artifacts are invalid")
    return value


def _expected_artifacts(args: argparse.Namespace) -> dict[str, dict[str, str]]:
    artifacts = {
        "hf": {
            "checkpoint_sha256": args.expected_hf_checkpoint,
            "image_input_id": args.expected_hf_image_input,
        },
        "chf": {
            "checkpoint_sha256": args.expected_chf_checkpoint,
            "image_input_id": args.expected_chf_image_input,
        },
    }
    for lane, artifact in artifacts.items():
        identity = HF_RESULT_IDENTITY if lane == "hf" else CHF_RESULT_IDENTITY
        try:
            ProviderResultFacts(
                identity=identity,
                runner_contract_id=EXPECTED_RUNNER_CONTRACTS[lane],
                checkpoint_ref=artifact["checkpoint_sha256"],
                image_input_ref=artifact["image_input_id"],
            )
        except (TypeError, ValueError) as error:
            raise LiveTestError(f"expected {lane} artifact identity is invalid") from error
    return artifacts


def _all_results(api: SignedApi, job_ref: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        query = urlencode({"limit": "100", **({"cursor": cursor} if cursor else {})})
        page = api.request("GET", f"/v1/jobs/{job_ref}/analysis-results", query)
        if (set(page) != {"schema_id", "job_ref", "results", "next_cursor"} or
                page.get("schema_id") != RESULT_LIST_SCHEMA_ID or
                page.get("job_ref") != job_ref or not isinstance(page.get("results"), list)):
            raise LiveTestError("malformed Analysis Result-list response")
        for item in page["results"]:
            if not isinstance(item, dict) or set(item) != {
                "analysis_result_ref", "execution_attempt_ref", "created_at",
                "provider_ref", "result_byte_length", "result_fingerprint",
                "result_schema_id",
            }:
                raise LiveTestError("malformed Analysis Result-list item")
        results.extend(page["results"])
        cursor = page.get("next_cursor")
        if cursor is None:
            return results
        if not isinstance(cursor, str):
            raise LiveTestError("malformed Analysis Result-list cursor")
        if cursor in seen_cursors or len(seen_cursors) >= 100:
            raise LiveTestError("Analysis Result pagination did not make bounded progress")
        seen_cursors.add(cursor)


def _validate_result(
    api: SignedApi,
    *,
    job_ref: str,
    lane: str,
    provider_ref: str,
    attempt: dict[str, object],
    expected_artifacts: dict[str, dict[str, str]],
) -> dict[str, object]:
    results = _all_results(api, job_ref)
    matching = [
        item for item in results
        if item.get("execution_attempt_ref") == attempt.get("execution_attempt_ref")
    ]
    if len(results) != 1 or len(matching) != 1:
        raise LiveTestError("successful Job must have exactly one bound Analysis Result")
    item = matching[0]
    if item.get("provider_ref") != provider_ref or item.get("result_schema_id") != RESULT_SCHEMA_ID:
        raise LiveTestError("Analysis Result metadata has the wrong provider or schema")
    result_ref = item.get("analysis_result_ref")
    if not isinstance(result_ref, str) or RESULT_REF_PATTERN.fullmatch(result_ref) is None:
        raise LiveTestError("Analysis Result has an invalid reference")
    read = api.request("GET", f"/v1/jobs/{job_ref}/analysis-results/{result_ref}")
    if set(read) != {
        "schema_id", "job_ref", "analysis_result_ref", "execution_attempt_ref",
        "provider_ref", "created_at", "result_byte_length", "result_fingerprint",
        "result_schema_id", "canonical_result_base64",
    } or read.get("schema_id") != RESULT_READ_SCHEMA_ID:
        raise LiveTestError("malformed Analysis Result-read response")
    expected = {
        "job_ref": job_ref,
        "analysis_result_ref": result_ref,
        "execution_attempt_ref": attempt.get("execution_attempt_ref"),
        "provider_ref": provider_ref,
        "result_schema_id": RESULT_SCHEMA_ID,
        "result_byte_length": item.get("result_byte_length"),
        "result_fingerprint": item.get("result_fingerprint"),
        "created_at": item.get("created_at"),
    }
    if any(read.get(key) != value for key, value in expected.items()):
        raise LiveTestError("Analysis Result read does not match its list metadata")
    encoded = read.get("canonical_result_base64")
    if not isinstance(encoded, str):
        raise LiveTestError("Analysis Result body is missing")
    try:
        raw = b64decode(encoded, validate=True)
    except ValueError as error:
        raise LiveTestError("Analysis Result body is not canonical base64") from error
    if b64encode(raw).decode("ascii") != encoded:
        raise LiveTestError("Analysis Result body uses noncanonical base64")
    if (len(raw) != item.get("result_byte_length") or
            f"sha256:{hashlib.sha256(raw).hexdigest()}" != item.get("result_fingerprint")):
        raise LiveTestError("Analysis Result bytes do not match their public identity")
    if len(raw) > MAX_RESULT_BYTES:
        raise LiveTestError("Analysis Result exceeds the NMRPeak result limit")
    try:
        document = parse_canonical_json_bytes(raw)
    except CanonicalJsonError as error:
        raise LiveTestError("Analysis Result bytes are not canonical JSON") from error
    _validate_product_result(document, raw, lane, expected_artifacts[lane])
    return {
        "execution_attempt_ref": attempt.get("execution_attempt_ref"),
        "analysis_result_ref": result_ref,
        "result_fingerprint": item.get("result_fingerprint"),
        "candidate_count": len(document["candidates"]),
    }


def _validate_product_result(
    document: object,
    raw: bytes,
    lane: str,
    expected_artifact: dict[str, str],
) -> None:
    if not isinstance(document, dict) or set(document) != {"schema_id", "candidates", "provenance"}:
        raise LiveTestError("NMRPeak result has unexpected top-level fields")
    candidates = document.get("candidates")
    if document.get("schema_id") != RESULT_SCHEMA_ID or not isinstance(candidates, list):
        raise LiveTestError("NMRPeak result has an invalid schema or candidate collection")
    generated: list[str] = []
    for candidate in candidates:
        if (not isinstance(candidate, dict) or set(candidate) != {"generated_smiles"} or
                not isinstance(candidate["generated_smiles"], str)):
            raise LiveTestError("NMRPeak result contains an invalid generated candidate")
        generated.append(candidate["generated_smiles"])
    identity = HF_RESULT_IDENTITY if lane == "hf" else CHF_RESULT_IDENTITY
    facts = ProviderResultFacts(
        identity=identity,
        runner_contract_id=EXPECTED_RUNNER_CONTRACTS[lane],
        checkpoint_ref=expected_artifact["checkpoint_sha256"],
        image_input_ref=expected_artifact["image_input_id"],
    )
    try:
        expected = canonical_result_bytes(generated, facts)
    except (RunnerResultRejected, TypeError, ValueError) as error:
        raise LiveTestError("NMRPeak result violates the production result contract") from error
    if raw != expected:
        raise LiveTestError("NMRPeak result does not match exact lane and artifact provenance")


def _observe(api: SignedApi, state: dict[str, object], path: Path, args: argparse.Namespace) -> None:
    deadline = monotonic() + args.wait_seconds
    while True:
        complete = True
        for entry in state["jobs"]:
            job = _validate_job(api, entry, args)
            from tests.live.failure_propagation import _all_attempts
            matches = [
                item for item in _all_attempts(api, entry["job_ref"])
                if item.get("provider_ref") == args.provider_ref
            ]
            _reject_cancelled_without_attempt(
                job, matches, f"positive {entry['lane']}", entry["job_ref"]
            )
            if len(matches) > 1:
                raise LiveTestError(f"Job {entry['job_ref']} has multiple Attempts from the provider")
            if len(matches) == 1 and matches[0].get("state") == "succeeded":
                if "provider_failure" in matches[0]:
                    raise LiveTestError("succeeded Attempt unexpectedly carries provider_failure")
                entry["evidence"] = _validate_result(
                    api,
                    job_ref=entry["job_ref"],
                    lane=entry["lane"],
                    provider_ref=args.provider_ref,
                    attempt=matches[0],
                    expected_artifacts=state["expected_artifacts"],
                )
            elif len(matches) == 1 and matches[0].get("state") == "failed":
                raise LiveTestError(
                    f"positive {entry['lane']} Job failed: "
                    f"{_canonical(_failure_summary(matches[0]))}"
                )
            elif len(matches) == 1 and matches[0].get("state") != "in_progress":
                raise LiveTestError(
                    f"positive {entry['lane']} Job ended in state {matches[0].get('state')}"
                )
            else:
                complete = False
        _write_state(path, state)
        if complete:
            for entry in state["jobs"]:
                evidence = entry["evidence"]
                print(_canonical({
                    "lane": entry["lane"],
                    "job_ref": entry["job_ref"],
                    "execution_attempt_ref": evidence["execution_attempt_ref"],
                    "analysis_result_ref": evidence["analysis_result_ref"],
                    "candidate_count": evidence["candidate_count"],
                }))
            return
        if monotonic() >= deadline:
            raise LiveTestError("timed out waiting for both successful Analysis Results")
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
    parser.add_argument("--expected-hf-checkpoint", required=True)
    parser.add_argument("--expected-chf-checkpoint", required=True)
    parser.add_argument("--expected-hf-image-input", required=True)
    parser.add_argument("--expected-chf-image-input", required=True)
    parser.add_argument("--ca-certificate", type=Path)
    parser.add_argument("--confirm-persistent-jobs", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=900)
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
            _write_state(args.state, state)
        _assert_binding(
            state,
            api,
            args,
            {lane: _success_input_document(lane) for lane, _, _ in LANES},
        )
        if state.get("expected_artifacts") != _expected_artifacts(args):
            raise LiveTestError("state expected artifacts do not match this invocation")
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
        print(f"live success propagation test failed: {error}", file=sys.stderr)
        raise SystemExit(1)
