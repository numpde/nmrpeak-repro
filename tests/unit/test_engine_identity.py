"""Verify the shared identity owner's receipt and output admission boundary."""

from pathlib import Path
from contextlib import redirect_stderr
import io
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deployment.engine_identity import IdentityProbeError, IdentityProbeUnsettled, _shared_owner, project_uids


class EngineIdentityTests(unittest.TestCase):
    def test_rejects_preloaded_ambient_submodule(self):
        foreign = SimpleNamespace(__file__="/tmp/foreign/linux_idmap/probe.py")
        with patch.dict(sys.modules, {"linux_idmap.probe": foreign}):
            with self.assertRaisesRegex(IdentityProbeError, "loaded before bundle admission|ambient"):
                _shared_owner()

    def test_rejects_same_path_preload_before_first_admission(self):
        path = Path(__file__).resolve().parents[2] / "bootstrap/acquisition/linux_idmap/probe.py"
        preloaded = SimpleNamespace(__file__=str(path))
        with patch("deployment.engine_identity._OWNER", None), \
             patch.dict(sys.modules, {"linux_idmap.probe": preloaded}):
            with self.assertRaisesRegex(IdentityProbeError, "loaded before bundle admission"):
                _shared_owner()

    def test_rejects_bundle_change_between_admissions(self):
        bundle = Path(__file__).resolve().parents[2] / "bootstrap/acquisition"
        verifier = SimpleNamespace(__file__=str(bundle / "acquisition_tool/source_update.py"),
                                   verify=lambda _path: {"version": "new"})
        with patch("deployment.engine_identity._OWNER", object()), \
             patch("deployment.engine_identity._SNAPSHOT", '{"version":"old"}'), \
             patch("deployment.engine_identity.importlib.import_module", return_value=verifier):
            with self.assertRaisesRegex(IdentityProbeError, "bundle changed"):
                _shared_owner()

    def test_one_retired_probe_projects_all_requested_uids(self):
        calls = []
        def invoke(arguments):
            calls.append(arguments)
            Path(arguments[arguments.index("--retired-receipt") + 1]).write_text("retired\n")
            print("165531 100999")
            return 0
        with patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=invoke)):
            self.assertEqual(project_uids("unix:///engine", "image", (65532, 1000)), (165531, 100999))
        self.assertEqual(len(calls), 1)
        self.assertIn("unix:///engine", calls[0])
        self.assertIn("33554432", calls[0])
        self.assertEqual(calls[0].count("--map"), 2)

    def test_missing_receipt_takes_precedence_over_cancellation_and_retains_evidence(self):
        with TemporaryDirectory() as parent:
            evidence = Path(parent) / "evidence"
            evidence.mkdir()
            with patch("deployment.engine_identity.mkdtemp", return_value=str(evidence)), \
                 patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=lambda _args: 143)):
                with self.assertRaisesRegex(IdentityProbeUnsettled, "retirement is unconfirmed; status 143"):
                    project_uids("unix:///engine", "image", (65532,))
            self.assertTrue(evidence.is_dir())

    def test_raised_cancellation_cannot_hide_an_unconfirmed_probe(self):
        with TemporaryDirectory() as parent:
            evidence = Path(parent) / "evidence"
            evidence.mkdir()
            def invoke(_arguments):
                raise SystemExit(143)
            with patch("deployment.engine_identity.mkdtemp", return_value=str(evidence)), \
                 patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=invoke)):
                with self.assertRaises(IdentityProbeUnsettled) as failure:
                    project_uids("unix:///engine", "image", (65532,))
            self.assertIsInstance(failure.exception.__cause__, SystemExit)
            self.assertTrue(evidence.is_dir())

    def test_success_cannot_admit_a_nonexact_receipt(self):
        with TemporaryDirectory() as parent:
            evidence = Path(parent) / "evidence"
            evidence.mkdir()
            def invoke(arguments):
                Path(arguments[arguments.index("--retired-receipt") + 1]).write_text("retired\nextra")
                print("165531")
                return 0
            with patch("deployment.engine_identity.mkdtemp", return_value=str(evidence)), \
                 patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=invoke)):
                with self.assertRaises(IdentityProbeUnsettled):
                    project_uids("unix:///engine", "image", (65532,))
            self.assertTrue(evidence.is_dir())

    def test_confirmed_retirement_propagates_cancellation_status(self):
        for status in (129, 130, 143):
            with self.subTest(status=status):
                def invoke(arguments):
                    Path(arguments[arguments.index("--retired-receipt") + 1]).write_text("retired\n")
                    print("165531")
                    return status
                with patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=invoke)):
                    with self.assertRaises(SystemExit) as cancellation:
                        project_uids("unix:///engine", "image", (65532,))
                self.assertEqual(cancellation.exception.code, status)

    def test_cleanup_failure_does_not_replace_settled_cancellation(self):
        for raised in (False, True):
            with self.subTest(raised=raised), TemporaryDirectory() as parent:
                def invoke(arguments):
                    Path(arguments[arguments.index("--retired-receipt") + 1]).write_text("retired\n")
                    if raised:
                        raise SystemExit(143)
                    return 143
                evidence = Path(parent) / "evidence"
                evidence.mkdir()
                diagnostic = io.StringIO()
                with patch("deployment.engine_identity.mkdtemp", return_value=str(evidence)), \
                     patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=invoke)), \
                     patch("deployment.engine_identity.shutil.rmtree", side_effect=OSError("cleanup denied")), \
                     redirect_stderr(diagnostic):
                    with self.assertRaises(SystemExit) as cancellation:
                        project_uids("unix:///engine", "image", (65532,))
                self.assertEqual(cancellation.exception.code, 143)
                self.assertIn("cleanup denied", " ".join(cancellation.exception.__notes__))
                self.assertIn("cleanup denied", diagnostic.getvalue())

    def test_failed_owner_output_is_not_admitted_after_confirmed_retirement(self):
        def invoke(arguments):
            Path(arguments[arguments.index("--retired-receipt") + 1]).write_text("retired\n")
            print("165531")
            print("original probe failure", file=sys.stderr)
            return 2
        with patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=invoke)):
            with self.assertRaisesRegex(IdentityProbeError, "original probe failure"):
                project_uids("unix:///engine", "image", (65532,))

    def test_success_requires_exact_receipt_and_complete_output(self):
        def invoke(arguments):
            Path(arguments[arguments.index("--retired-receipt") + 1]).write_text("retired\n")
            print("165531 extra")
            return 0
        with patch("deployment.engine_identity._shared_owner", return_value=SimpleNamespace(main=invoke)):
            with self.assertRaisesRegex(IdentityProbeError, "malformed"):
                project_uids("unix:///engine", "image", (65532,))
