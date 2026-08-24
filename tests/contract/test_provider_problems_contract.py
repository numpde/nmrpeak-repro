"""Exercise every released operation/problem pair through the explicit parser."""

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
    def test_every_released_problem_shape_is_admitted_for_its_operation(self) -> None:
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
                    response = ProviderHttpResponse(
                        status=status,
                        topology="dev-local",
                        content_type="application/problem+json",
                        request_id="header-contract-request",
                        body=json.dumps(document).encode("utf-8"),
                    )
                    with self.subTest(operation=operation.value, status=status):
                        self.assertIs(
                            type(parse_provider_problem(operation, response)),
                            ProviderProblem,
                        )


if __name__ == "__main__":
    unittest.main()
