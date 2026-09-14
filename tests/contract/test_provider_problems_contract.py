"""Check the adapter admits every generated current API Problem variant.

The deterministic projection manifest owns provenance; this test checks adapter
coverage without preserving a second historical wire or recovery catalogue.
"""
from __future__ import annotations

import json
import unittest

from nmrpeak_provider._nmr_api_failure_contract import OPERATIONS, PROFILES, RECOVERY, SEND_EFFECTS
from nmrpeak_provider.provider_https import ProviderHttpResponse, ProviderOperation
from nmrpeak_provider.provider_problems import ProviderProblem, parse_provider_problem


class ProviderProblemContractTests(unittest.TestCase):
    def test_all_projected_operation_cause_variants_retain_shared_facts(self):
        for admitted_operation in ProviderOperation:
            operation = admitted_operation.value
            statuses = OPERATIONS[operation]
            for status, indices in statuses.items():
                for index in indices:
                    profile = PROFILES[index]
                    properties = profile["properties"]
                    for code in properties["code"]["enum"]:
                        document = {
                            "type": properties["type"]["const"],
                            "title": properties["title"]["const"],
                            "status": status, "code": code,
                            "detail": profile["fixed_details"].get(code, "Disclosed API cause."),
                            "instance": "urn:nmr-api:request:contract-request",
                            "request_id": "contract-request",
                        }
                        if code in profile["upload_codes"]:
                            document["upload_ref"] = "upload:sha256:" + "a" * 64
                        response = ProviderHttpResponse(
                            status=status, topology="dev-local",
                            content_type="application/problem+json",
                            request_id="contract-request", body=json.dumps(document).encode(),
                        )
                        with self.subTest(operation=operation, status=status, code=code):
                            parsed = parse_provider_problem(ProviderOperation(operation), response)
                            self.assertIs(type(parsed), ProviderProblem)
                            self.assertEqual(parsed.code, code)
                            self.assertEqual(parsed.upload_ref, document.get("upload_ref"))
                            self.assertEqual(parsed.current_send_effect,
                                             SEND_EFFECTS[operation][status][document["type"]][code])
                            self.assertEqual(parsed.recovery_mode, RECOVERY[operation]["mode"])
                            self.assertEqual(parsed.recovery_description, RECOVERY[operation]["description"])
