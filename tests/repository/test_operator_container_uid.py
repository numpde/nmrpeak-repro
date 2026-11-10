"""Check the provider's host-to-container projection boundary."""

import unittest
from unittest.mock import Mock

from deployment.operator_container_uid import container_uid
from deployment.provider_deployment import DeploymentOperationRejected


class OperatorContainerUidTests(unittest.TestCase):
    def test_uses_the_selected_map_inverse(self):
        mapping = Mock()
        mapping.translate_inverse.return_value = 17
        self.assertEqual(17, container_uid(1000, mapping))
        mapping.translate_inverse.assert_called_once_with(1000)

    def test_unmapped_or_ambiguous_identity_is_rejected(self):
        mapping = Mock()
        mapping.translate_inverse.side_effect = ValueError("not mapped uniquely")
        with self.assertRaises(DeploymentOperationRejected):
            container_uid(1000, mapping)
