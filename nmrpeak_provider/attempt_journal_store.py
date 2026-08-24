"""Single-writer durable storage for independent Attempt journal records."""

from __future__ import annotations

from contextlib import AbstractContextManager
import os
from pathlib import Path
import re
import stat
from threading import Lock

from .attempt_journal import (
    MAX_JOURNAL_RECORD_BYTES,
    AttemptJournalRecord,
    StartPending,
    journal_record_bytes,
    journal_record_name,
    parse_journal_record,
)


_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.json")
_STAGING_NAME = re.compile(r"\.([0-9a-f]{64}\.json)\.pending")
_DIRECTORY_MODE = 0o700
_RECORD_MODE = 0o600


class AttemptJournalStateRejected(RuntimeError):
    """Persisted journal state is unsafe, corrupt, or unsupported."""


class AttemptJournalConflict(RuntimeError):
    """A caller tried to replace or retire a different durable obligation."""


class AttemptJournalWriteFailed(RuntimeError):
    """A journal mutation did not reach a confirmed durable outcome."""


class AttemptJournalStore(AbstractContextManager["AttemptJournalStore"]):
    """Serialize one provider process under the deployment's provider lock."""

    def __init__(self, root: Path, *, maximum_records: int) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise TypeError("Attempt journal root must be an absolute Path")
        if type(maximum_records) is not int or maximum_records < 1:
            raise ValueError("Attempt journal maximum record count must be positive")
        self._root = root
        self._maximum_records = maximum_records
        self._lock = Lock()
        self._directory_fd = -1
        self._poisoned = False
        self._open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release this process's directory descriptor without changing records."""

        with self._lock:
            if self._directory_fd >= 0:
                os.close(self._directory_fd)
                self._directory_fd = -1

    def records(self) -> tuple[AttemptJournalRecord, ...]:
        """Load the complete admitted record inventory in stable filename order."""

        with self._lock:
            self._require_usable()
            return tuple(
                self._read_record(name)
                for name in sorted(self._record_names())
            )

    def admit(self, record: StartPending) -> StartPending:
        """Durably add one new start obligation without replacing any record."""

        if type(record) is not StartPending:
            raise TypeError("Attempt journal admission requires pending start facts")
        with self._lock:
            self._require_usable()
            names = self._record_names()
            target = journal_record_name(record)
            if target in names:
                raise AttemptJournalConflict(
                    "Attempt journal already contains this provider Attempt key"
                )
            if len(names) >= self._maximum_records:
                raise AttemptJournalConflict("Attempt journal record slots are exhausted")
            self._replace_record(target, journal_record_bytes(record))
            return record

    def replace(
        self,
        expected: AttemptJournalRecord,
        replacement: AttemptJournalRecord,
    ) -> AttemptJournalRecord:
        """Durably replace one exact record without touching any other Attempt."""

        expected_name = journal_record_name(expected)
        if journal_record_name(replacement) != expected_name:
            raise ValueError("Attempt journal replacement changed the Attempt key")
        with self._lock:
            self._require_usable()
            self._require_current(expected_name, expected)
            self._replace_record(expected_name, journal_record_bytes(replacement))
            return replacement

    def retire(self, expected: AttemptJournalRecord) -> None:
        """Durably remove only the exact obligation the caller reconciled."""

        target = journal_record_name(expected)
        with self._lock:
            self._require_usable()
            self._require_current(target, expected)
            try:
                os.unlink(target, dir_fd=self._directory_fd)
                os.fsync(self._directory_fd)
            except OSError as error:
                self._poisoned = True
                raise AttemptJournalWriteFailed(
                    "Attempt journal retirement durability is unconfirmed"
                ) from error

    def _open(self) -> None:
        try:
            root_status = os.lstat(self._root)
            self._validate_directory(root_status)
            self._directory_fd = os.open(
                self._root,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
            )
            self._validate_directory(os.fstat(self._directory_fd))
            removed_staging = self._admit_persisted_state()
            if removed_staging:
                os.fsync(self._directory_fd)
        except (OSError, ValueError, AttemptJournalStateRejected) as error:
            if self._directory_fd >= 0:
                os.close(self._directory_fd)
                self._directory_fd = -1
            if isinstance(error, AttemptJournalStateRejected):
                raise
            raise AttemptJournalStateRejected(
                "Attempt journal directory cannot be admitted"
            ) from error

    def _admit_persisted_state(self) -> bool:
        """Validate every obligation before removing safe pre-effect staging."""

        record_names: list[str] = []
        staging_names: list[str] = []
        for name in os.listdir(self._directory_fd):
            if _RECORD_NAME.fullmatch(name) is not None:
                record_names.append(name)
            elif _STAGING_NAME.fullmatch(name) is not None:
                staging_names.append(name)
            else:
                raise AttemptJournalStateRejected(
                    "Attempt journal contains an unsupported entry"
                )
        if len(record_names) > self._maximum_records:
            raise AttemptJournalStateRejected(
                "Attempt journal contains more records than configured slots"
            )
        for name in record_names:
            self._read_record(name)
        for name in staging_names:
            descriptor = self._open_record(name)
            os.close(descriptor)
        for name in staging_names:
            os.unlink(name, dir_fd=self._directory_fd)
        return bool(staging_names)

    def _record_names(self) -> set[str]:
        names: set[str] = set()
        for name in os.listdir(self._directory_fd):
            if _RECORD_NAME.fullmatch(name) is None:
                if _STAGING_NAME.fullmatch(name) is not None:
                    raise AttemptJournalStateRejected(
                        "Attempt journal contains unrecovered staging state"
                    )
                raise AttemptJournalStateRejected(
                    "Attempt journal contains an unsupported entry"
                )
            names.add(name)
        if len(names) > self._maximum_records:
            raise AttemptJournalStateRejected(
                "Attempt journal contains more records than configured slots"
            )
        return names

    def _read_record(self, name: str) -> AttemptJournalRecord:
        descriptor = self._open_record(name)
        try:
            chunks: list[bytes] = []
            total = 0
            while total <= MAX_JOURNAL_RECORD_BYTES:
                chunk = os.read(
                    descriptor,
                    min(65_536, MAX_JOURNAL_RECORD_BYTES + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            os.close(descriptor)
        if total > MAX_JOURNAL_RECORD_BYTES:
            raise AttemptJournalStateRejected(
                "Attempt journal record exceeds its durable size limit"
            )
        try:
            record = parse_journal_record(b"".join(chunks))
        except (TypeError, ValueError) as error:
            raise AttemptJournalStateRejected(
                "Attempt journal contains an invalid record"
            ) from error
        if journal_record_name(record) != name:
            raise AttemptJournalStateRejected(
                "Attempt journal filename does not match its record identity"
            )
        return record

    def _open_record(self, name: str) -> int:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=self._directory_fd,
            )
        except OSError as error:
            raise AttemptJournalStateRejected(
                "Attempt journal entry cannot be opened safely"
            ) from error
        try:
            self._validate_record_file(os.fstat(descriptor))
        except AttemptJournalStateRejected:
            os.close(descriptor)
            raise
        return descriptor

    def _require_current(
        self,
        name: str,
        expected: AttemptJournalRecord,
    ) -> None:
        if name not in self._record_names() or self._read_record(name) != expected:
            self._poisoned = True
            raise AttemptJournalConflict(
                "Attempt journal record changed before the requested mutation"
            )

    def _replace_record(self, target: str, raw: bytes) -> None:
        staging = f".{target}.pending"
        descriptor = -1
        try:
            descriptor = os.open(
                staging,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                _RECORD_MODE,
                dir_fd=self._directory_fd,
            )
            os.fchmod(descriptor, _RECORD_MODE)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("Attempt journal write made no progress")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                staging,
                target,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            os.fsync(self._directory_fd)
        except OSError as error:
            self._poisoned = True
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(staging, dir_fd=self._directory_fd)
            except OSError:
                pass
            raise AttemptJournalWriteFailed(
                "Attempt journal replacement durability is unconfirmed"
            ) from error

    def _require_usable(self) -> None:
        if self._directory_fd < 0:
            raise AttemptJournalStateRejected("Attempt journal store is closed")
        if self._poisoned:
            raise AttemptJournalStateRejected(
                "Attempt journal store is unusable after an uncertain mutation"
            )
        self._validate_directory(os.fstat(self._directory_fd))

    @staticmethod
    def _validate_directory(status: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE
            or status.st_uid != os.geteuid()
            or status.st_gid != os.getegid()
        ):
            raise AttemptJournalStateRejected(
                "Attempt journal root must be an owner-only directory"
            )

    @staticmethod
    def _validate_record_file(status: os.stat_result) -> None:
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != _RECORD_MODE
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or status.st_gid != os.getegid()
        ):
            raise AttemptJournalStateRejected(
                "Attempt journal entry must be one owner-only regular file"
            )
