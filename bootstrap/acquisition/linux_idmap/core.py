"""Read Linux UID/GID maps and apply explicitly chosen affine mappings."""

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path


class Identifier(int):
    """An integer UID or GID from 0 through 4294967294."""

    def __new__(cls, value: int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("identifier is not a usable Linux ID")
        # An int subclass can override conversion; check the value we will store.
        value = int(value)
        if not 0 <= value < 2**32 - 1:
            raise ValueError("identifier is not a usable Linux ID")
        return super().__new__(cls, value)

    @classmethod
    def of(cls, text: str) -> "Identifier":
        """Parse 1 to 10 ASCII decimal digits; invalid syntax/range raises ValueError."""
        if not isinstance(text, str) or re.fullmatch(r"[0-9]{1,10}", text) is None:
            raise ValueError("expected a decimal Linux ID")
        return cls(int(text, 10))


@dataclass(frozen=True)
class IdRange:
    """One /proc map row: inside start, outside start, and count.

    4294967295 is Linux's unmapped-ID marker; keep it in raw rows.
    """

    inside: int
    outside: int
    length: int

    @classmethod
    def of(cls, line: bytes) -> "IdRange":
        """Parse exactly three integer columns; malformed rows raise ValueError."""
        inside, outside, length = map(int, line.split())
        return cls(inside, outside, length)


@dataclass
class IdMap:
    """Translate using caller-supplied non-overlapping affine ranges.

    For example, IdRange(0, 1000, 2) maps IDs 0 and 1 to 1000 and 1001.
    Copies the input iterable into its own editable list of rows.
    """

    rows: list[IdRange]

    def __init__(self, rows: Iterable[IdRange]):
        self.rows = list(rows)

    def translate(self, identifier: int) -> Identifier:
        """Return the outside ID; invalid or unmapped IDs raise ValueError."""
        identifier = Identifier(identifier)
        for row in self.rows:
            if row.inside <= identifier < row.inside + row.length:
                return Identifier(row.outside + identifier - row.inside)
        raise ValueError("identifier is not mapped")

    def translate_inverse(self, identifier: int) -> Identifier:
        """Return the unique inside ID for an outside ID, or raise ValueError."""
        identifier = Identifier(identifier)
        inside = None
        for row in self.rows:
            if row.outside <= identifier < row.outside + row.length:
                if inside is not None:
                    raise ValueError(f"Cannot translate outside ID {identifier}: multiple ranges contain it")
                inside = row.inside + identifier - row.outside
        if inside is None:
            raise ValueError(f"Cannot translate outside ID {identifier}: it is not mapped")
        return Identifier(inside)


@dataclass(frozen=True)
class MapRows:
    """UID/GID rows, supplied as one-pass streams by read_maps."""

    uid_rows: Iterable[IdRange]
    gid_rows: Iterable[IdRange]


def parse_map(raw: bytes) -> Iterator[IdRange]:
    """Yield reported rows in order; malformed-row errors wait until consumption."""
    return (IdRange.of(line) for line in raw.splitlines())


def read_maps(read_file: Callable[[str], bytes]) -> MapRows:
    """Capture both files now; decode their captured bytes during row iteration."""
    return MapRows(parse_map(read_file("uid_map")), parse_map(read_file("gid_map")))


def observe_process(pid: int) -> MapRows:
    """Read both map files for a PID visible in the caller's /proc."""
    if not (type(pid) is int and pid > 0):
        raise ValueError("pid must be a positive integer")
    directory = Path("/proc") / str(pid)
    return read_maps(lambda name: (directory / name).read_bytes())
