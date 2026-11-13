"""Check operator mapping delegates to the selected-engine identity owner."""

import unittest
from unittest.mock import patch

from deployment import operator_container_uid


class OperatorContainerUidTests(unittest.TestCase):
    def test_requests_inverse_mapping_for_the_host_operator(self):
        with patch.object(operator_container_uid.sys, "argv", ["operator", "image"]), \
             patch.object(operator_container_uid, "docker_host", return_value="unix:///engine"), \
             patch.object(operator_container_uid, "project_uids", return_value=(0,)) as project, \
             patch("builtins.print") as output:
            operator_container_uid.main()
        project.assert_called_once_with("unix:///engine", "image", (operator_container_uid.os.geteuid(),), inverse=True)
        output.assert_called_once_with(0)
