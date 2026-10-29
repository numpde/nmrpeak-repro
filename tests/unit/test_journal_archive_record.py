"""Keep historical closure evidence bound to its exact retained Attempt and Job."""

from base64 import b64encode
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import unittest

from nmrpeak_provider.attempt_journal import journal_record_bytes
from nmrpeak_provider.journal_archive_record import archived_original
from tests.fakes.retained_journal import failure_command, entered_record, retained_terminal
from tests.unit.test_attempt_journal import active_attempt


def archive_document():
    active = entered_record(active_attempt())
    record = retained_terminal(active, failure_command(
        execution_attempt_ref=active.execution_attempt_ref,
        failure_code="input_rejected", failure_message="The input is not supported.",
    ))
    record = replace(record, terminal_hold_action="do_not_resend", terminal_hold_description="Do not resend.",
                     terminal_observed_state="expired")
    raw = journal_record_bytes(record)
    digest = "sha256:" + sha256(raw).hexdigest()
    return record, digest, {
        "schema_id": "nmrpeak.closed_attempt_archive.v1", "record_digest": digest,
        "execution_attempt_ref": record.execution_attempt_ref,
        "owner": {"origin": "https://api.example.test", "topology": "web",
                  "authority_id": "sha256:" + "a" * 64, "provider_ref": "provider:nmrpeak"},
        "original_record_base64": b64encode(raw).decode("ascii"), "reason": "Investigated the retained failure.",
        "snapshot": {"execution_attempt_ref": record.execution_attempt_ref, "job_ref": record.job_ref,
                     "state": "expired", "job_state": "closed"}, "delivery": "unconfirmed",
    }


class JournalArchiveRecordTests(unittest.TestCase):
    def test_closed_snapshot_preserves_exact_record_without_claiming_delivery(self):
        record, digest, document = archive_document()
        for state in ("succeeded", "failed", "expired"):
            with self.subTest(state=state):
                document["snapshot"]["state"] = state
                self.assertEqual(archived_original(document, expected_digest=digest), record)

    def test_snapshot_job_must_be_the_original_job(self):
        _record, digest, document = archive_document()
        document["snapshot"]["job_ref"] = "job:other"
        with self.assertRaises(ValueError):
            archived_original(document, expected_digest=digest)

    def test_nonclosed_or_unknown_snapshot_and_delivery_claim_are_refused(self):
        _record, digest, document = archive_document()
        for state in ("in_progress", "invented", None, []):
            modified = deepcopy(document)
            modified["snapshot"]["state"] = state
            with self.subTest(state=state), self.assertRaises(ValueError):
                archived_original(modified, expected_digest=digest)
        document["delivery"] = "confirmed"
        with self.assertRaises(ValueError):
            archived_original(document, expected_digest=digest)
