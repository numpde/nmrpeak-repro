"""Script API wire replies while the installed SDK owns signing and admission."""

import asyncio
from base64 import b64encode
from hashlib import sha256
import json
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpResponse


class SharedApi:
    origin = "https://api.example.test"
    provider_ref = "provider:nmrpeak"

    def __init__(self, inputs):
        self.inputs = inputs
        self.started = {}
        self.calls = []
        self.identity = SigningIdentity(self.origin, "credential:fixture", Ed25519PrivateKey.generate())

    def new_api(self):
        return ProviderAPI(self.identity, ApiTransport(self), expected_topology="web")

    def response(self, request):
        path = request.url.path
        query = parse_qs(request.url.query.decode("ascii"))
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        if path == "/provider/v1/hello":
            return {"schema_id": "nmr.provider.hello_response.v1", "provider_ref": self.provider_ref,
                    "accepted_at": "2026-10-29T00:00:00Z"}
        if path == "/provider/v1/jobs":
            kind = query["analysis_kind_ref"][0]
            jobs = [{"job_ref": job, "analysis_kind_ref": selected_kind,
                     "input_fingerprint": "sha256:" + sha256(raw).hexdigest(),
                     "input_byte_length": len(raw), "input_schema_id": "nmr.job.specification.text.v1",
                     "created_at": "2026-10-29T00:00:00Z"}
                    for job, (selected_kind, raw) in self.inputs.items()
                    if selected_kind == kind and job not in self.started]
            return {"schema_id": "nmr.provider.jobs.list.response.v1", "analysis_kind_ref": kind,
                    "has_provider_execution_attempt": False, "jobs": jobs, "next_cursor": None}
        if path.startswith("/provider/v1/jobs/"):
            job = path.split("/")[-2]
            _, raw = self.inputs[job]
            return {"schema_id": "nmr.provider.job_input.read.response.v1", "job_ref": job,
                    "input_schema_id": "nmr.job.specification.text.v1",
                    "input_fingerprint": "sha256:" + sha256(raw).hexdigest(),
                    "input_byte_length": len(raw), "canonical_input_base64": b64encode(raw).decode("ascii")}
        if path.endswith("/execution-attempts/start"):
            job = body["job_ref"]
            reference = "execution_attempt:sha256:" + sha256(job.encode()).hexdigest()
            self.started[job] = reference
            return {"schema_id": "nmr.provider.execution_attempt_start_response.v1",
                    "execution_attempt_ref": reference, "job_ref": job,
                    "analysis_kind_ref": self.inputs[job][0], "provider_ref": self.provider_ref,
                    "state": "in_progress", "started_at": "2026-10-29T00:00:00Z", "replayed": False}
        if path.endswith("/progress"):
            return {"schema_id": "nmr.provider.execution_attempt_progress_response.v1",
                    "execution_attempt_ref": path.split("/")[-2], "phase": body["phase"],
                    "condition_code": body["condition_code"], "updated_at": "2026-10-29T00:00:00Z"}
        if path.startswith("/provider/v1/execution-attempts/"):
            reference = path.rsplit("/", 1)[1]
            job = next(job for job, selected in self.started.items() if selected == reference)
            return {"schema_id": "nmr.provider.execution_attempt_read_response.v1",
                    "execution_attempt_ref": reference, "job_ref": job,
                    "state": "in_progress", "job_state": "open"}
        raise AssertionError("Unexpected API wire operation: " + path)


class ApiTransport:
    exchange_seconds = 0.5
    retirement_seconds = 0.2
    retirement_confirmed = True
    pending_owner = None

    def __init__(self, api):
        self.api = api

    async def exchange(self, request, *, maximum_bytes, owner=None):
        await asyncio.sleep(0)
        raw = json.dumps(self.api.response(request)).encode()
        if len(raw) > maximum_bytes:
            raise AssertionError("Fixture exceeded the SDK response allowance")
        return HttpResponse(200, ((b"content-type", b"application/json"),
                                  (b"cache-control", b"no-store"), (b"nmr-api-topology", b"web")), raw)

    async def retire(self, *, owner=None):
        pass
