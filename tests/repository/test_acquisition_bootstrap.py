"""The checked-in acquisition closure uses its independent reviewed source pin."""

from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[2]


class AcquisitionSourceTests(unittest.TestCase):
    def test_checked_in_source_matches_the_reviewed_version(self):
        bundle = ROOT / 'bootstrap/acquisition'
        updater = runpy.run_path(str(bundle / 'acquisition_tool/source_update.py'))
        manifest = updater['verify'](bundle)
        pin = updater['read_pin'](ROOT / 'requirements.provider-tools-source.json')
        self.assertEqual(manifest['release_version'], pin['version'])
