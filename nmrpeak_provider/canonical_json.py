"""Canonical JSON for provider identities and persisted wire facts."""

from __future__ import annotations

import json
from typing import TypeAlias, cast


JsonScalar: TypeAlias = None | bool | int | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class CanonicalJsonError(ValueError):
    """Input is not the provider's one canonical JSON representation."""


def canonical_json_bytes(value: JsonValue) -> bytes:
    """Encode the provider's closed, compact, key-sorted JSON domain."""

    _validate_json_value(value, active_containers=set())
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def parse_canonical_json_bytes(raw: bytes) -> JsonValue:
    """Decode only exact canonical bytes, rejecting ambiguous JSON spellings."""

    if type(raw) is not bytes:
        raise TypeError("canonical JSON input must be bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_fractional_number,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _RejectedJsonToken) as error:
        raise CanonicalJsonError("input is not unambiguous UTF-8 JSON") from error
    try:
        rendered = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise CanonicalJsonError("input contains an unsupported JSON value") from error
    if rendered != raw:
        raise CanonicalJsonError("input is not in canonical JSON form")
    return cast(JsonValue, value)


class _RejectedJsonToken(ValueError):
    pass


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _RejectedJsonToken("duplicate object member")
        value[key] = item
    return value


def _reject_fractional_number(_value: str) -> None:
    raise _RejectedJsonToken("fractional number")


def _reject_non_finite_number(_value: str) -> None:
    raise _RejectedJsonToken("non-finite number")


def _validate_json_value(
    value: object,
    *,
    active_containers: set[int],
) -> None:
    value_type = type(value)
    if value is None or value_type in {bool, int}:
        return
    if value_type is str:
        value.encode("utf-8", errors="strict")
        return
    if value_type not in {list, dict}:
        raise TypeError(f"unsupported canonical JSON type: {value_type.__name__}")
    identity = id(value)
    if identity in active_containers:
        raise ValueError("canonical JSON value contains a container cycle")
    active_containers.add(identity)
    try:
        if value_type is list:
            for item in value:
                _validate_json_value(item, active_containers=active_containers)
        else:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("canonical JSON object names must be strings")
                key.encode("utf-8", errors="strict")
                _validate_json_value(item, active_containers=active_containers)
    finally:
        active_containers.remove(identity)
