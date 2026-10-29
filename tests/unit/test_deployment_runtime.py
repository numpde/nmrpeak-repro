"""Project selected SDK grants without parsing bindings in host Python."""

from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import tempfile
import unittest

from nmrpeak_provider.deployment_runtime import main, project_provider_binding
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
    def test_failed_projection_reports_the_original_sdk_error(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = Path(directory) / "bindings.toml"
            definition.write_text(DEFINITION)
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                status = main([
                    "project", "--definition", str(definition), "--binding", "missing",
                    "--image", "sha256:" + "a" * 64, "--namespace", "nmrpeak-fixture",
                ])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["failure"]["exception_type"], "BindingDefinitionRejected")

    def project(self, definition, name):
        return project_provider_binding(definition, name, image="sha256:" + "a" * 64,
                                        namespace="nmrpeak-fixture", input_sources={})

    def test_selected_binding_grants_exact_sdk_route_without_reading_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = Path(directory) / "bindings.toml"
            definition.write_text(DEFINITION)
            projection = self.project(definition, "first")
        self.assertEqual(projection["provider_ref"], "provider:nmrpeak")
        self.assertEqual(projection["input_paths"], ("/run/credentials/first.json",))
        self.assertEqual(projection["delivery"]["socket_path"], "/run/delivery/first/delivery.sock")
        route = projection["api_route"]
        self.assertEqual(set(route["compose"]["services"]), {"api", "api-init"})
        self.assertEqual(route["compose"]["services"]["api"]["user"], "65532:65532")
        self.assertTrue(route["client_mount"]["read_only"])
        self.assertEqual(route["client_depends_on"], {"api": {"condition": "service_healthy"}})
        self.assertNotIn("credentials", str(route["compose"]))

    def test_delivery_principal_and_historical_journal_namespace_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = Path(directory) / "bindings.toml"
            definition.write_text(DEFINITION)
            first, second = self.project(definition, "first"), self.project(definition, "second")
        expected = journal_api_authority_id(origin="https://api.example.test", topology="web")
        self.assertEqual(first["journal_authority_id"], expected)
        self.assertEqual(second["journal_authority_id"], expected)
        self.assertNotEqual(first["delivery"]["ipc_volume"], second["delivery"]["ipc_volume"])
        self.assertNotEqual(first["api_route"]["client_socket"], second["api_route"]["client_socket"])
