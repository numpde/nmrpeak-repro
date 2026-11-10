"""Qualify the deployed adapter against the installed shared identity package."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from linux_idmap import IdMap, IdRange
from linux_idmap.probe import ProbeFailure, ProbeResult, Problem, ResourceState, Retirement
from deployment import provider_deployment as deployment


IMAGE = "sha256:" + "a" * 64
ROOTLESS = IdMap((IdRange(0, 1000, 1), IdRange(1, 100000, 65536)))


class SharedIdentityAdapterTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NMRPEAK_DOCKER_HOST": "unix:///test.sock"}))

    def test_request_preserves_engine_interpreter_and_existing_caps(self):
        result = ProbeResult(ROOTLESS, ROOTLESS, "c" * 64)
        with patch("linux_idmap.probe.probe_identity", return_value=result) as probe:
            observed = deployment._container_uid_map(Path("/usr/bin/docker"), IMAGE)
        self.assertIs(observed, ROOTLESS)
        request = probe.call_args.args[0]
        self.assertEqual(("unix:///test.sock", IMAGE, "/usr/local/bin/python3.12"),
                         (request.endpoint, request.image, request.interpreter))
        self.assertEqual((65532, 65532), (request.user_uid, request.user_gid))
        self.assertEqual((32 * 1024**2, 4, 250_000_000),
                         (request.resources.memory_bytes, request.resources.pids_limit,
                          request.resources.nano_cpus))
        self.assertEqual("/usr/bin/docker", probe.call_args.kwargs["executable"])
        self.assertEqual(165531, deployment._mapped_host_uid(65532, observed))

    def test_uncertain_retirement_stops_deployment_before_acl_changes(self):
        failure = ProbeFailure(Problem("engine", "Docker reply uncertain"),
                               Retirement(ResourceState.UNRESOLVED))
        with patch("linux_idmap.probe.probe_identity", side_effect=failure), \
             self.assertRaises(deployment.MaintenanceInvocationUnsettled):
            deployment._container_uid_map(Path("/usr/bin/docker"), IMAGE)

    def test_settled_probe_failure_and_unmapped_provider_identity_are_rejected(self):
        failure = ProbeFailure(Problem("process-identity", "probe changed"),
                               Retirement(ResourceState.RETIRED))
        with patch("linux_idmap.probe.probe_identity", side_effect=failure), \
             self.assertRaises(deployment.DeploymentOperationRejected):
            deployment._container_uid_map(Path("/usr/bin/docker"), IMAGE)
        with self.assertRaises(deployment.DeploymentOperationRejected):
            deployment._mapped_host_uid(999999, ROOTLESS)


if __name__ == "__main__":
    unittest.main()
