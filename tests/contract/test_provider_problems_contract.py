"""Exercise migrated authorization and remaining historical Problem profiles."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from nmrpeak_provider.provider_http_contract import (
    load_provider_http_contract_release,
)
from nmrpeak_provider.provider_https import ProviderHttpResponse, ProviderOperation
from nmrpeak_provider.provider_problems import ProviderProblem, parse_provider_problem


CONTRACT_ROOT = Path(__file__).parents[2] / "contracts/upstream/nmr_api_v1"


class ProviderProblemContractTests(unittest.TestCase):
    def test_migrated_authorization_and_remaining_profiles_are_admitted(self) -> None:
        release = load_provider_http_contract_release(CONTRACT_ROOT)
        for path_item in release.openapi["paths"].values():
            for operation_document in path_item.values():
                operation = ProviderOperation(operation_document["operationId"])
                for status_text, response_document in operation_document[
                    "responses"
                ].items():
                    if status_text == "200":
                        continue
                    status = int(status_text)
                    schema = response_document["content"][
                        "application/problem+json"
                    ]["schema"]
                    document = {
                        "type": schema["properties"]["type"]["const"],
                        "title": schema["properties"]["title"]["const"],
                        "status": status,
                        "instance": "/provider/v1/problems/contract",
                        "request_id": "body-contract-request",
                    }
                    if "code" in schema["properties"]:
                        document["code"] = schema["properties"]["code"]["enum"][0]
                        document["detail"] = "Correct the provider request."
                    if status == 403:
                        # Authorization migrated to API1708; the archived release
                        # remains evidence only for statuses still awaiting migration.
                        document.update(code="authorization_denied",
                                        detail="Check the provider account permissions.",
                                        instance="urn:nmr-api:request:body-contract-request")
                    response = ProviderHttpResponse(
                        status=status,
                        topology="dev-local",
                        content_type="application/problem+json",
                        request_id=("body-contract-request" if status == 403 else "header-contract-request"),
                        body=json.dumps(document).encode("utf-8"),
                    )
                    with self.subTest(operation=operation.value, status=status):
                        self.assertIs(
                            type(parse_provider_problem(operation, response)),
                            ProviderProblem,
                        )


if __name__ == "__main__":
    unittest.main()
