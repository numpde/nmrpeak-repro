"""Peak's decisions around the shared acquisition owner."""

from pathlib import Path
import shutil
import subprocess
import unittest

from tests.repository import wheelhouse_acquisition_fixture as fixtures


class SharedAcquisitionAdapterTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WheelhouseFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        (fixture.repo / ".git/info/exclude").write_text("/bootstrap/\n/repository_checks/acquisition_bootstrap.py\n")
        (fixture.repo / "repository_checks/acquisition_bootstrap.py").write_text("# verified in dedicated bootstrap tests\n")
        owner = fixture.repo / "bootstrap/acquisition/acquisition_tool/owner.sh"
        owner.parent.mkdir(parents=True)
        owner.write_text('''
acquisition_scope() {
    local receipt
    while [[ "$1" != -- ]]; do
        if [[ "$1" == --retired-receipt ]]; then receipt=$2; fi
        shift 2
    done
    shift
    local result=0
    "$@" || result=$?
    [[ ${FAKE_NO_RECEIPT:-0} == 1 ]] || printf 'retired\\n' > "$receipt"
    return "$result"
}
acquisition_set_route() { printf 'route\\n' >> "$FAKE_ENGINE/owner-events"; }
acquisition_worker() {
    local mode=$1 destination=$2
    printf '%s\\n' "$mode" >> "$FAKE_ENGINE/owner-events"
    if [[ ${FAKE_OWNER_FAILURE:-0} != 0 ]]; then return "$FAKE_OWNER_FAILURE"; fi
    if [[ "$mode" == offline ]]; then
        local count=0
        [[ ! -f "$FAKE_ENGINE/offline-count" ]] || read -r count < "$FAKE_ENGINE/offline-count"
        count=$((count + 1))
        printf '%s\\n' "$count" > "$FAKE_ENGINE/offline-count"
        if (( count == 1 )); then printf -v "$destination" '%s' "$FAKE_INITIAL"; else printf -v "$destination" '%s' "$FAKE_FINAL"; fi
        printf '{}\\n'
    else
        printf -v "$destination" 0
        printf '{}\\n'
    fi
}
''')

    def test_unverified_bundle_never_runs_owner(self):
        (self.fixture.repo / "repository_checks/acquisition_bootstrap.py").write_text("raise SystemExit(1)\n")
        result = self.fixture.run_acquisition(0, 0, route=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot verify the shared acquisition bootstrap", result.stderr)
        self.assertFalse((self.fixture.engine / "owner-events").exists())

    def test_warm_hit_needs_neither_tool_nor_route(self):
        (self.fixture.pool / "tool.pyz").unlink()
        result = self.fixture.run_acquisition(0, 0, route=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.fixture.engine / "owner-events").read_text().splitlines(), ["offline"])
        self.assertIn("no acquisition is needed", result.stdout)

    def test_missing_retirement_receipt_preserves_captured_inputs(self):
        result = self.fixture.run_acquisition(0, 0, route=False, extra={"FAKE_NO_RECEIPT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retirement is unconfirmed", result.stderr)
        self.assertNotIn("no acquisition is needed", result.stdout)
        self.assertTrue(list(self.fixture.root.glob("tmp.*/report-input")))

    def test_owner_failure_does_not_trigger_network(self):
        result = self.fixture.run_acquisition(1, 0, extra={"FAKE_OWNER_FAILURE": "143"})
        self.assertEqual(result.returncode, 143)
        self.assertIn("wheel check lifecycle failed", result.stderr)
        self.assertEqual((self.fixture.engine / "owner-events").read_text().splitlines(), ["offline"])

    def test_failed_final_report_is_not_success(self):
        result = self.fixture.run_acquisition(1, 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked wheel selection still fails offline", result.stderr)
        self.assertNotIn("Acquired the locked", result.stdout)

    def test_cold_tool_uses_shared_online_worker(self):
        fixture = self.fixture
        shutil.copyfile(fixtures.ROOT / "scripts/wheelhouse-tool.sh", fixture.repo / "scripts/wheelhouse-tool.sh")
        (fixture.repo / "repository_checks/wheelhouse_tool_asset.py").write_text(
            "import sys\nif __name__ == '__main__': sys.exit(10)\n"
        )
        env = fixture.env | {"NMRPEAK_NETWORK_INTERFACE_INPUT": "system",
                             "NMRPEAK_NETWORK_INTERFACE_INPUT_ORIGIN": "command line"}
        result = subprocess.run(["bash", str(fixture.repo / "scripts/wheelhouse-tool.sh")],
                                cwd=fixture.repo, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((fixture.engine / "owner-events").read_text().splitlines(), ["route", "online"])

    def test_unproven_selection_runs_requested_acquisition_then_rechecks(self):
        result = self.fixture.run_acquisition(1, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.fixture.engine / "owner-events").read_text().splitlines(),
                         ["offline", "route", "online", "online", "online", "online", "offline"])
        self.assertIn("Offline selection was not established", result.stderr)



if __name__ == "__main__":
    unittest.main()
