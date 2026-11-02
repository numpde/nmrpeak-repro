"""Check host-to-container UID translation for wheelhouse operations."""

import unittest

from deployment.operator_container_uid import container_uid
from deployment.provider_deployment import DeploymentOperationRejected


class OperatorContainerUidTests(unittest.TestCase):
    def test_translates_rootful_and_rootless_mappings(self):
        self.assertEqual(container_uid(1000, ((0, 0, 65536),)), 1000)
        self.assertEqual(container_uid(1000, ((0, 1000, 1), (1, 100000, 65536))), 0)

    def test_rejects_unmapped_or_ambiguous_host_identity(self):
        for mappings in (((0, 0, 100),), ((0, 1000, 1), (100, 1000, 1))):
            with self.subTest(mappings=mappings), self.assertRaises(DeploymentOperationRejected):
                container_uid(1000, mappings)
