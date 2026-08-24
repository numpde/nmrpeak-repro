"""Keep the HTTPS operation table equal to the authenticated API release."""

from __future__ import annotations

from pathlib import Path
import unittest

from nmrpeak_provider.provider_http_contract import (
    load_provider_http_contract_release,
)
from nmrpeak_provider.provider_https import ProviderOperation, _PROFILES


CONTRACT_ROOT = Path(__file__).parents[2] / "contracts/upstream/nmr_api_v1"


class ProviderHttpsContractTests(unittest.TestCase):
    def test_operation_methods_caps_and_statuses_equal_the_release(self) -> None:
        release = load_provider_http_contract_release(CONTRACT_ROOT)
        expected = {}
        for path, path_item in release.openapi["paths"].items():
            for method, operation in path_item.items():
                operation_id = operation["operationId"]
                expected[ProviderOperation(operation_id)] = {
                    "method": method.upper(),
                    "request_body_limit": operation["x-nmr-max-request-body-bytes"],
                    "response_body_limit": operation["x-nmr-max-response-bytes"],
                    "statuses": frozenset(int(status) for status in operation["responses"]),
                }
        actual = {
            operation: {
                "method": profile.method,
                "request_body_limit": profile.request_body_limit,
                "response_body_limit": profile.response_body_limit,
                "statuses": profile.statuses,
            }
            for operation, profile in _PROFILES.items()
        }
        self.assertEqual(expected, actual)

    def test_operation_paths_equal_the_released_templates(self) -> None:
        release = load_provider_http_contract_release(CONTRACT_ROOT)
        samples = {
            "execution_attempt_ref": "execution_attempt:sha256:" + "1" * 64,
            "job_ref": "job:test",
        }
        for operation_id, (method, path_template) in release.routes.items():
            operation = ProviderOperation(operation_id)
            path = path_template.format(**samples)
            with self.subTest(operation=operation_id):
                self.assertEqual(method, _PROFILES[operation].method)
                self.assertIsNotNone(_PROFILES[operation].path.fullmatch(path))


if __name__ == "__main__":
    unittest.main()
