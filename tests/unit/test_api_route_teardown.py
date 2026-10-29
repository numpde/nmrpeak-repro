"""Admit original SDK mount evidence, including opaque long-namespace volumes."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deployment import provider_deployment as d
from tests.unit.test_deployment_topology import binding_projection


class ApiRouteTeardownTests(unittest.TestCase):
    def owners(self):
        deployment = "a" * 64
        namespace = "nmrpeak-" + deployment
        projection = binding_projection(namespace=namespace)
        route = projection["api_route"]
        volume_key = route["client_mount"]["source"]
        name = route["compose"]["volumes"][volume_key]["name"]
        services = {}
        for role in ("api", "api-init"):
            services[role] = {
                "Id": ("a" if role == "api" else "b") * 64,
                "Config": {"Labels": deepcopy(route["compose"]["services"][role]["labels"])},
                "Mounts": [{"Type": "volume", "Name": name, "Destination": "/route", "RW": True}],
            }
        services["provider"] = {"Id": "c" * 64}
        return deployment, name, services

    def inspect_resources(self, deployment, name, services, *, attachments=(), original_names=None):
        labels = {"com.docker.compose.project": "nmrpeak-" + deployment,
                  "com.docker.compose.volume": name}
        outputs = [name.encode() + b"\n", json.dumps([{
            "Name": name, "Driver": "local", "Options": None, "Labels": labels,
        }]).encode(), b"\n".join(item.encode() for item in attachments), b""]
        with patch.object(d, "_docker_command", side_effect=[SimpleNamespace(stdout=raw) for raw in outputs]):
            return d._inspect_project_resources(Path("/docker"), deployment, services,
                                                route_volumes=(name,) if original_names is None else original_names)

    def test_actual_sdk_mount_grants_own_the_volume_without_rebuilding_its_name(self):
        deployment, name, services = self.owners()
        self.assertEqual(d._api_route_volumes(deployment, services), (name,))
        self.assertEqual(self.inspect_resources(deployment, name, services,
                                               attachments=[services["api"]["Id"], services["provider"]["Id"]]), (name,))

    def test_missing_or_changed_sdk_ownership_and_mismatched_mounts_are_refused(self):
        for change in ("label", "mount", "binding", "malformed"):
            deployment, name, services = self.owners()
            if change == "label":
                services["api"]["Config"]["Labels"]["org.nmr-provider-tools.route"] = "foreign"
            elif change == "mount":
                services["api"]["Mounts"][0]["Name"] = "different-original-volume"
            elif change == "binding":
                services["api"]["Config"]["Labels"]["org.nmr-provider-tools.binding"] = "different-binding"
            else:
                services["api"]["Mounts"] = [None]
            with self.subTest(change=change), self.assertRaises(d.DeploymentOperationRejected):
                d._api_route_volumes(deployment, services)

    def test_a_scientific_lane_is_a_foreign_route_attachment(self):
        deployment, name, services = self.owners()
        services["hf-runner"] = {"Id": "d" * 64}
        with self.assertRaises(d.DeploymentOperationRejected):
            self.inspect_resources(deployment, name, services, attachments=[services["hf-runner"]["Id"]])

    def test_a_labeled_volume_without_original_role_evidence_is_not_admitted(self):
        deployment, name, _services = self.owners()
        with self.assertRaises(d.DeploymentOperationRejected):
            self.inspect_resources(deployment, name, {}, original_names=())

    def test_teardown_consumes_only_original_route_grants_without_orphan_removal(self):
        deployment, name, _services = self.owners()
        effects = []

        def run(_docker, arguments, *, timeout):
            effects.append(arguments)
            if "down" in arguments:
                at = [index for index, item in enumerate(arguments) if item == "--file"][-1]
                overlay = json.loads(Path(arguments[at+1]).read_bytes())
                self.assertEqual(overlay["volumes"], {name: {"name": name}})
                self.assertEqual(set(overlay["services"]), {"api", "api-init"})
                for role in overlay["services"].values():
                    self.assertEqual(role["volumes"], [{"type": "volume", "source": name, "target": "/route"}])
                self.assertNotIn("--remove-orphans", arguments)
            return SimpleNamespace(stdout=b"")

        source = Path(__file__).parents[2] / "compose/provider-teardown.yml"
        with patch.object(d, "_read_committed_template", return_value=source.read_bytes()), patch.object(d, "_docker_command", side_effect=run):
            d._run_compose_down(Path("/docker"), Path("/checkout"), deployment, route_volumes=(name,))
        self.assertEqual(len(effects), 3)
        self.assertEqual(effects[0][-4:], ("stop", "--timeout", "600", "provider"))
