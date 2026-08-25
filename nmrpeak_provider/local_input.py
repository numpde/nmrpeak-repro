"""Snapshot bounded local files without interpreting their contents."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import stat
from typing import BinaryIO, Callable


class LocalInputFailureReason(Enum):
    UNREADABLE = "unreadable"
    NOT_REGULAR = "not_regular"
    TOO_LARGE = "too_large"
    DIRECTORY_UNREADABLE = "directory_unreadable"
    TOO_MANY_DIRECTORY_ENTRIES = "too_many_directory_entries"
    TOO_MANY_SELECTED_FILES = "too_many_selected_files"
    INVALID_SELECTED_FILE = "invalid_selected_file"


class LocalInputSnapshotError(ValueError):
    """Report a path-free mechanical failure to snapshot local input."""

    __slots__ = ("reason",)

    def __init__(self, reason: LocalInputFailureReason) -> None:
        if type(reason) is not LocalInputFailureReason:
            raise TypeError("reason must be an exact LocalInputFailureReason")
        self.reason = reason
        super().__init__("local input snapshot failed")


@dataclass(frozen=True, slots=True)
class LocalDocumentSpec:
    """Name one bounded regular-file input without interpreting its bytes."""

    path: Path
    maximum_bytes: int

    def __post_init__(self) -> None:
        _require_request(path=self.path, maximum_bytes=self.maximum_bytes)
        if not self.path.is_absolute():
            raise ValueError("local document path must be absolute")


def read_bounded_regular_file(
    path: Path,
    /,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read one leaf without following it or blocking on a special file."""

    _require_request(path=path, maximum_bytes=maximum_bytes)
    raw = _read_open_file(_open_file(path), maximum_bytes=maximum_bytes)
    if len(raw) > maximum_bytes:
        raise LocalInputSnapshotError(LocalInputFailureReason.TOO_LARGE)
    return raw

def read_ordered_bounded_regular_files(
    directory: Path,
    /,
    *,
    filename_suffix: str,
    maximum_directory_entries: int,
    maximum_files: int,
    maximum_file_bytes: int,
) -> tuple[bytes, ...]:
    """Read selected files through one held directory descriptor.

    Names are enumerated once, then leaves are opened sequentially relative to
    the descriptor. A regular replacement may win before its leaf opens, so
    the returned bytes are not an atomic multi-file generation.
    """

    _require_directory_request(
        directory=directory,
        filename_suffix=filename_suffix,
        maximum_directory_entries=maximum_directory_entries,
        maximum_files=maximum_files,
        maximum_file_bytes=maximum_file_bytes,
    )
    return _read_directory_snapshot(
        _open_directory(directory),
        filename_suffix,
        maximum_entries=maximum_directory_entries,
        maximum_files=maximum_files,
        maximum_file_bytes=maximum_file_bytes,
    )


def _read_directory_snapshot(
    descriptor: int,
    suffix: str,
    *,
    maximum_entries: int,
    maximum_files: int,
    maximum_file_bytes: int,
) -> tuple[bytes, ...]:
    operation_succeeded = False
    try:
        snapshot = _snapshot_selected_files(
            descriptor,
            suffix,
            maximum_entries=maximum_entries,
            maximum_files=maximum_files,
            maximum_file_bytes=maximum_file_bytes,
        )
        operation_succeeded = True
        return snapshot
    finally:
        _close_owned(
            lambda: os.close(descriptor),
            preserve_failure=not operation_succeeded,
            failure_reason=LocalInputFailureReason.DIRECTORY_UNREADABLE,
        )


def _snapshot_selected_files(
    descriptor: int,
    suffix: str,
    *,
    maximum_entries: int,
    maximum_files: int,
    maximum_file_bytes: int,
) -> tuple[bytes, ...]:
    names = _read_selected_names(
        descriptor,
        suffix,
        maximum_entries=maximum_entries,
        maximum_files=maximum_files,
    )
    return tuple(
        _read_directory_file(descriptor, name, maximum_bytes=maximum_file_bytes)
        for name in names
    )


def _require_request(*, path: object, maximum_bytes: object) -> None:
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    if type(maximum_bytes) is not int:
        raise TypeError("maximum_bytes must be an exact int")
    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")


def _require_directory_request(
    *,
    directory: object,
    filename_suffix: object,
    maximum_directory_entries: object,
    maximum_files: object,
    maximum_file_bytes: object,
) -> None:
    _require_request(path=directory, maximum_bytes=maximum_file_bytes)
    if type(filename_suffix) is not str or not filename_suffix:
        raise ValueError("filename_suffix must be non-empty text")
    _require_positive_limit("maximum_directory_entries", maximum_directory_entries)
    _require_positive_limit("maximum_files", maximum_files)


def _require_positive_limit(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact int")


def _read_open_file(descriptor: int, *, maximum_bytes: int) -> bytes:
    source = _open_regular_source(descriptor)
    read_succeeded = False
    try:
        raw = source.read(maximum_bytes + 1)
    except OSError:
        raise LocalInputSnapshotError(LocalInputFailureReason.UNREADABLE) from None
    else:
        read_succeeded = True
    finally:
        _close_owned(source.close, preserve_failure=not read_succeeded)
    return raw


def _open_regular_source(descriptor: int) -> BinaryIO:
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LocalInputSnapshotError(LocalInputFailureReason.NOT_REGULAR)
        source = os.fdopen(descriptor, "rb")
        descriptor = -1
        return source
    except OSError:
        raise LocalInputSnapshotError(LocalInputFailureReason.UNREADABLE) from None
    finally:
        if descriptor >= 0:
            _close_owned(lambda: os.close(descriptor), preserve_failure=True)


def _close_owned(
    close: Callable[[], object],
    *,
    preserve_failure: bool,
    failure_reason: LocalInputFailureReason = LocalInputFailureReason.UNREADABLE,
) -> None:
    try:
        close()
    except OSError:
        if not preserve_failure:
            raise LocalInputSnapshotError(failure_reason) from None


def _open_file(path: Path) -> int:
    try:
        return os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError:
        raise LocalInputSnapshotError(LocalInputFailureReason.UNREADABLE) from None


def _open_directory(path: Path) -> int:
    try:
        return os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        raise LocalInputSnapshotError(
            LocalInputFailureReason.DIRECTORY_UNREADABLE
        ) from None


def _read_selected_names(
    descriptor: int,
    suffix: str,
    *,
    maximum_entries: int,
    maximum_files: int,
) -> tuple[str, ...]:
    try:
        entries = os.scandir(descriptor)
    except OSError:
        raise LocalInputSnapshotError(
            LocalInputFailureReason.DIRECTORY_UNREADABLE
        ) from None
    return _select_and_close(
        entries,
        suffix,
        maximum_entries=maximum_entries,
        maximum_files=maximum_files,
    )


def _select_and_close(
    entries: os.ScandirIterator[str],
    suffix: str,
    *,
    maximum_entries: int,
    maximum_files: int,
) -> tuple[str, ...]:
    scan_succeeded = False
    try:
        names = _select_names_or_unreadable(
            entries,
            suffix,
            maximum_entries=maximum_entries,
            maximum_files=maximum_files,
        )
        scan_succeeded = True
    finally:
        _close_owned(
            entries.close,
            preserve_failure=not scan_succeeded,
            failure_reason=LocalInputFailureReason.DIRECTORY_UNREADABLE,
        )
    return tuple(sorted(names))


def _select_names_or_unreadable(
    entries: Iterable[os.DirEntry[str]],
    suffix: str,
    *,
    maximum_entries: int,
    maximum_files: int,
) -> list[str]:
    try:
        return _select_names(
            entries,
            suffix,
            maximum_entries=maximum_entries,
            maximum_files=maximum_files,
        )
    except OSError:
        raise LocalInputSnapshotError(
            LocalInputFailureReason.DIRECTORY_UNREADABLE
        ) from None


def _select_names(
    entries: Iterable[os.DirEntry[str]],
    suffix: str,
    *,
    maximum_entries: int,
    maximum_files: int,
) -> list[str]:
    selected: list[str] = []
    for count, entry in enumerate(entries, start=1):
        if count > maximum_entries:
            raise LocalInputSnapshotError(
                LocalInputFailureReason.TOO_MANY_DIRECTORY_ENTRIES
            )
        if entry.name.endswith(suffix):
            selected.append(entry.name)
    if len(selected) > maximum_files:
        raise LocalInputSnapshotError(LocalInputFailureReason.TOO_MANY_SELECTED_FILES)
    return selected


def _read_directory_file(
    descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
) -> bytes:
    try:
        file_descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        raw = _read_open_file(file_descriptor, maximum_bytes=maximum_bytes)
    except (OSError, LocalInputSnapshotError):
        raise LocalInputSnapshotError(
            LocalInputFailureReason.INVALID_SELECTED_FILE
        ) from None
    if len(raw) > maximum_bytes:
        raise LocalInputSnapshotError(
            LocalInputFailureReason.INVALID_SELECTED_FILE
        )
    return raw
