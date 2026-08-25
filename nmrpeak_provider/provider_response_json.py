"""Decode bounded provider response bodies as strict ordinary JSON objects."""

from __future__ import annotations

import json


def decode_provider_response_object(raw: bytes) -> dict[str, object]:
    """Decode unambiguous JSON and require an object response root."""

    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=_object_without_duplicates,
        parse_float=_reject_json_number,
        parse_constant=_reject_json_number,
    )
    if type(value) is not dict:
        raise TypeError("Provider response JSON root is not an object")
    return value


class _RejectedJson(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise _RejectedJson
        value[name] = item
    return value


def _reject_json_number(_value: str) -> None:
    raise _RejectedJson
