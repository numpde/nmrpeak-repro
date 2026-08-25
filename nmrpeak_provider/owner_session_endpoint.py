"""Filesystem trust checks shared by both ends of a runner owner session."""

from __future__ import annotations

import os
import stat


def open_owner_session_directory(socket_path: object) -> tuple[int, str]:
    """Validate a Linux UDS path and return its held private parent directory.

    The caller owns the returned descriptor and holds its validated parent
    while operating on the leaf. Runtime composition protects the ancestor
    namespace identity.
    """

    if (
        type(socket_path) is not str
        or not socket_path
        or "\x00" in socket_path
        or not os.path.isabs(socket_path)
        or os.path.normpath(socket_path) != socket_path
    ):
        raise ValueError("owner-session socket path must be absolute and normalized")
    if len(os.fsencode(socket_path)) > 107:
        raise ValueError("owner-session socket path exceeds the Linux AF_UNIX limit")

    parent_path, socket_name = os.path.split(socket_path)
    if not socket_name or os.path.realpath(parent_path) != parent_path:
        raise ValueError("owner-session directory must not traverse symlinks")

    parent_fd = os.open(
        parent_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        parent_status = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or stat.S_IMODE(parent_status.st_mode) != 0o700
        ):
            raise PermissionError(
                "owner-session directory must be owned by the effective UID "
                "with mode 0700"
            )
        return parent_fd, socket_name
    except BaseException:
        os.close(parent_fd)
        raise
