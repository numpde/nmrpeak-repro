"""Retain uncertain journal closure without touching a reused descriptor."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nmrpeak_provider.attempt_journal_store import AttemptJournalStore


class JournalDescriptorLifetimeTests(unittest.TestCase):
    def test_failed_close_is_sticky_and_never_closes_a_reused_descriptor(self):
        failure = OSError("Directory close acknowledgement failed")
        close = os.close
        replacement = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            root.mkdir(mode=0o700)
            store = AttemptJournalStore(root, maximum_records=1)

            def close_then_reuse(descriptor):
                close(descriptor)
                reopened = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                if reopened != descriptor:
                    os.dup2(reopened, descriptor)
                    close(reopened)
                replacement.append(descriptor)
                raise failure

            try:
                with patch("nmrpeak_provider.attempt_journal_store.os.close", side_effect=close_then_reuse) as closing:
                    for _ in range(2):
                        with self.assertRaises(OSError) as caught:
                            store.close()
                        self.assertIs(caught.exception, failure)
                    closing.assert_called_once()
                os.fstat(replacement[0])
            finally:
                for descriptor in set(replacement):
                    close(descriptor)

    def test_context_retains_primary_and_independent_directory_close_failure(self):
        primary = RuntimeError("Journal operation failed")
        cleanup = OSError("Journal directory close failed")
        close = os.close
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            root.mkdir(mode=0o700)
            store = AttemptJournalStore(root, maximum_records=1)

            def failed_close(descriptor):
                close(descriptor)
                raise cleanup

            with patch("nmrpeak_provider.attempt_journal_store.os.close", side_effect=failed_close):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    with store:
                        raise primary
            self.assertEqual(caught.exception.exceptions, (primary, cleanup))

    def test_admission_refusal_keeps_its_independent_close_failure(self):
        primary = ValueError("Opened directory posture changed")
        cleanup = OSError("Refused directory close failed")
        close = os.close
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal"
            root.mkdir(mode=0o700)

            def failed_close(descriptor):
                close(descriptor)
                raise cleanup

            with (
                patch.object(AttemptJournalStore, "_validate_directory", side_effect=[None, primary]),
                patch("nmrpeak_provider.attempt_journal_store.os.close", side_effect=failed_close),
                self.assertRaises(BaseExceptionGroup) as caught,
            ):
                AttemptJournalStore(root, maximum_records=1)
            self.assertEqual(caught.exception.exceptions, (primary, cleanup))
