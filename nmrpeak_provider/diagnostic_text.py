"""Keep useful diagnostic text while removing credentials before display bounds."""

import re


_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", re.S)
_CREDENTIAL = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|password|passwd|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|signature|token|secret|sig|credential|cookie|set-cookie|"
    r"x-amz-(?:signature|credential|security-token))\b[\"']?\s*[:=]\s*)"
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&<>]+)'''
)
_AUTH = re.compile(r"(?i)\b(?:Bearer|Basic|Negotiate)\s+[^\s\"'<>;,]+|\bsk-[A-Za-z0-9_*.-]+")
_AUTH_HEADER = re.compile(r"(?im)(\bauthorization[ \t]*[:=][ \t]*)(?![ \t]|\[redacted\])[^\r\n]+")
_COOKIE = re.compile(r"(?im)(\b(?:set-cookie|cookie)[ \t]*:[ \t]*)(?![ \t]|\[redacted\])[^\r\n]+")
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.I)


def _redact_credential(match: re.Match[str]) -> str:
    value = match[0][len(match[1]):]
    if value.rstrip(".!?") == "[redacted]":
        return match[0]
    return match[1] + "[redacted]"


def diagnostic_text(value: str, *, secrets: tuple[str, ...] = (), limit: int = 4096) -> str:
    """Redact known secrets and credential syntax, then bound UTF-8 output.

    This is not a classifier for arbitrary confidential content. Callers pass
    diagnostic explanations, not request/header/environment containers.
    """
    if type(value) is not str or type(limit) is not int or limit < 32:
        raise TypeError("Diagnostic text requires text and a byte limit of at least 32")
    if secrets:
        pattern = "|".join(re.escape(secret) for secret in sorted(set(secrets), key=len, reverse=True) if secret)
        if pattern:
            value = re.sub(pattern, "[redacted]", value)
    value = _PRIVATE_KEY.sub("[redacted private key]", value)
    value = _AUTH_HEADER.sub(lambda match: match[1] + "[redacted]", value)
    value = _AUTH.sub("[redacted]", value)
    value = _COOKIE.sub(lambda match: match[1] + "[redacted]", value)
    value = _CREDENTIAL.sub(_redact_credential, value)
    value = _URL_PASSWORD.sub(r"\1[redacted]@", value)
    value = " ".join("".join(character if character.isprintable() else " " for character in value).split())
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        suffix = " [truncated]"
        value = encoded[:limit - len(suffix)].decode("utf-8", errors="ignore") + suffix
    return value


def exception_diagnostic(error: BaseException, *, secrets: tuple[str, ...] = (), limit: int = 4096) -> str:
    """Preserve messages, causal chains and cleanup notes without traceback locals."""
    parts: list[str] = []
    seen: set[int] = set()

    def visit(current: BaseException, relationship: str = "") -> None:
        if id(current) in seen or len(seen) >= 16:
            parts.append(relationship + "[exception chain truncated]")
            return
        seen.add(id(current))
        try:
            message = str(current)
        except Exception:
            message = "[exception message unavailable]"
        parts.append(f"{relationship}{type(current).__name__}: {message}")
        parts.extend("Note: " + note for note in getattr(current, "__notes__", ()) if type(note) is str)
        if isinstance(current, BaseExceptionGroup):
            for member in current.exceptions[:16]:
                visit(member, "Member: ")
        cause = current.__cause__ if current.__cause__ is not None else current.__context__
        if cause is not None:
            visit(cause, "Caused by: " if current.__cause__ is not None else "Earlier failure: ")

    visit(error)
    return diagnostic_text("\n".join(parts), secrets=secrets, limit=limit)
