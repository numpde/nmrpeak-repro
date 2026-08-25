"""Own the exact text bounds shared by runner and Attempt failure messages."""

from __future__ import annotations

from typing import TypeGuard


MAX_FAILURE_MESSAGE_CHARS = 1_024


def is_failure_message(value: object, /) -> TypeGuard[str]:
    """Return whether text can cross the published Attempt failure boundary."""

    # Server A preserves provider diagnostics verbatim, including whitespace
    # and controls. Only its explicit NUL, surrogate, and character bounds are
    # enforced here; display sanitization belongs to authorized readers.
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_FAILURE_MESSAGE_CHARS
        or "\0" in value
    ):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True
