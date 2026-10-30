"""Project selected SDK grants without parsing bindings in host Python."""

from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import tempfile
import unittest

from nmrpeak_provider.deployment_runtime import main, project_provider_inputs
from nmrpeak_provider.journal_identity import journal_api_authority_id


DEFINITION = '''project = "shared-delivery"
uid = 65532
gid = 65532
request_seconds = 5
retry_initial_seconds = 1
retry_max_seconds = 30
[limits]
provider_items = 2
provider_bytes = 262144
total_items = 4
total_bytes = 524288
'''
for name, provider in (("first", "provider:nmrpeak"), ("second", "provider:other")):
    DEFINITION += f'''
[[bindings]]
name = "{name}"
socket_path = "/run/delivery/{name}/delivery.sock"
[bindings.api]
origin = "https://api.example.test"
profile = "run"
topology = "web"
provider_ref = "{provider}"
credential_file = "/run/credentials/{name}.json"
exchange_seconds = 5
retirement_seconds = 5
[bindings.route]
mode = "mapped"
interface = "lo"
log_level = "INFO"
addresses = ["api.example.test=127.0.0.1"]
'''


class DeploymentRuntimeTests(unittest.TestCase):
    def test_projects_shared_grants_without_reading_api_secrets(self):
        from tests.unit.test_runtime_config_v2 import TEMPLATE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = root / "provider.toml"
            provider.write_bytes(TEMPLATE)
            projection = project_provider_inputs(provider)
        self.assertEqual(projection["provider_ref"], "provider:nmrpeak")
        self.assertEqual(projection["input_paths"], ["/run/credentials/first.json"])
        grants = projection["shared"]
        self.assertEqual(grants["group_add"], ["1234"])
        self.assertEqual(set(grants["volumes"]), {"fixture-proxy-socket", "fixture-outbox-socket"})
        for mount in grants["mounts"]:
            self.assertTrue(mount["read_only"])
            self.assertTrue(mount["volume"]["nocopy"])
        self.assertEqual(projection["journal_authority_id"], journal_api_authority_id(
            origin="https://api.example.test", topology="web"))

    def test_invalid_sdk_grant_reports_failure(self):
        from tests.unit.test_runtime_config_v2 import TEMPLATE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = root / "provider.toml"
            provider.write_bytes(TEMPLATE.replace(b"gid = 1234", b"gid = 0"))
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                status = main(["project", "--provider", str(provider)])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("failure", json.loads(errors.getvalue()))
