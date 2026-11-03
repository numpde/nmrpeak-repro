"""Filesystem identities shared by procfs and the selected Docker socket."""

from dataclasses import dataclass
from os import stat_result


@dataclass(frozen=True)
class FileIdentity:
    """A file's device and inode, compared together."""

    device: int
    inode: int

    @classmethod
    def of(cls, metadata: stat_result) -> "FileIdentity":
        return cls(metadata.st_dev, metadata.st_ino)
