"""Explicit operator admission identity for one analysis and Job time window."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from hashlib import sha256
import re

from .canonical_json import JsonValue, canonical_json_bytes


_FINGERPRINT_DOMAIN = b"nmrpeak.run_generation.v1\0"
_PROVIDER_REF = re.compile(r"provider:[A-Za-z0-9_.-]{1,119}")
_ANALYSIS_KIND_REF = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_GENERATION_ID = re.compile(r"(?:[a-z0-9]|[a-z0-9][a-z0-9._-]{0,62}[a-z0-9])")
_UTC_TIMESTAMP = re.compile(
    r"(?!0000)[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.(?!000000)[0-9]{6})?Z"
)


@dataclass(frozen=True, slots=True)
class CreatedAtWindow:
    """Inclusive lower and optional exclusive upper Job creation boundary."""

    not_before: datetime
    not_after: datetime | None = None

    def __post_init__(self) -> None:
        _require_utc_datetime(self.not_before, "not_before")
        if self.not_after is not None:
            _require_utc_datetime(self.not_after, "not_after")
            if self.not_before >= self.not_after:
                raise ValueError(
                    "Run generation not_after must be later than not_before"
                )

    def contains(self, created_at: datetime) -> bool:
        """Apply the window's inclusive-start and exclusive-end semantics."""

        _require_utc_datetime(created_at, "created_at")
        if created_at < self.not_before:
            return False
        return self.not_after is None or created_at < self.not_after


@dataclass(frozen=True, slots=True)
class RunGenerationIdentity:
    """The policy facts whose equality permits one logical provider run."""

    provider_ref: str
    analysis_kind_ref: str
    generation_id: str
    scope: CreatedAtWindow

    def __post_init__(self) -> None:
        _require_string_match(
            self.provider_ref,
            _PROVIDER_REF,
            "provider_ref",
        )
        _require_string_match(
            self.analysis_kind_ref,
            _ANALYSIS_KIND_REF,
            "analysis_kind_ref",
        )
        if len(self.analysis_kind_ref) > 128:
            raise ValueError("Run generation analysis_kind_ref exceeds 128 characters")
        _require_string_match(
            self.generation_id,
            _GENERATION_ID,
            "generation_id",
        )
        if type(self.scope) is not CreatedAtWindow:
            raise TypeError("Run generation scope must be a CreatedAtWindow")


def run_generation_material(identity: RunGenerationIdentity) -> dict[str, JsonValue]:
    """Render the exact canonical facts admitted under one generation."""

    if type(identity) is not RunGenerationIdentity:
        raise TypeError("Run generation material requires its owned identity")
    return {
        "v": 1,
        "provider_ref": identity.provider_ref,
        "analysis_kind_ref": identity.analysis_kind_ref,
        "generation_id": identity.generation_id,
        "scope": {
            "kind": "created_at_window",
            "not_before": canonical_utc_timestamp(identity.scope.not_before),
            "not_after": (
                canonical_utc_timestamp(identity.scope.not_after)
                if identity.scope.not_after is not None
                else None
            ),
        },
    }


def run_generation_fingerprint(identity: RunGenerationIdentity) -> str:
    """Hash the domain-separated canonical generation material."""

    material = canonical_json_bytes(run_generation_material(identity))
    return f"sha256:{sha256(_FINGERPRINT_DOMAIN + material).hexdigest()}"


def parse_canonical_utc_timestamp(value: object) -> datetime:
    """Parse only the UTC spelling used by NMR API Job timestamps."""

    _require_string_match(value, _UTC_TIMESTAMP, "timestamp")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError("Run generation timestamp is not a calendar date") from error


def canonical_utc_timestamp(value: datetime) -> str:
    """Render UTC with seconds or exactly six fractional digits."""

    _require_utc_datetime(value, "timestamp")
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).removesuffix("+00:00") + "Z"


def _require_utc_datetime(value: object, field: str) -> None:
    if type(value) is not datetime:
        raise TypeError(f"Run generation {field} must be a datetime")
    if value.tzinfo is not UTC:
        raise ValueError(f"Run generation {field} must use the UTC timezone")


def _require_string_match(
    value: object,
    pattern: re.Pattern[str],
    field: str,
) -> None:
    if type(value) is not str:
        raise TypeError(f"Run generation {field} must be a string")
    if pattern.fullmatch(value) is None:
        raise ValueError(f"Run generation {field} has an invalid format")
