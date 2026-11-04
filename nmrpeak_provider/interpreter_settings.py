"""Parse provider-owned endpoint settings and their captured file references."""

from __future__ import annotations

import re
import tomllib


_LEAF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_REQUIRED = frozenset({"url", "model", "key_file"})
_OPTIONAL = frozenset({"ca_file", "reasoning_effort"})


def endpoint_file_reference(value: object) -> str:
    """Admit one sibling file, never a host or parent-directory path."""
    if type(value) is not str or _LEAF.fullmatch(value) is None or value.endswith(".toml"):
        raise ValueError("Interpreter key and CA references must name a sibling file.")
    return value


def parse_endpoint_settings(raw: bytes) -> dict[str, object]:
    """Read the thin endpoint format; callers own the referenced file snapshots."""
    document = _parse_document(raw)
    if document is None:
        raise ValueError("Invalid interpreter endpoint configuration.")
    if not _REQUIRED <= document.keys() or document.keys() - (_REQUIRED | _OPTIONAL):
        raise ValueError("Invalid interpreter endpoint configuration fields.")
    endpoint_file_reference(document["key_file"])
    if "ca_file" in document:
        endpoint_file_reference(document["ca_file"])
    return document


def _parse_document(raw: bytes):
    try:
        return tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        return None
