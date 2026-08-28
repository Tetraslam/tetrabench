"""Bounded RFC 8785 JSON with a strict, no-float value profile."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

import rfc8785
from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

MAX_CANONICAL_JSON_BYTES = 2 * 1024 * 1024
MAX_SAFE_INTEGER = (1 << 53) - 1

type _SafeInteger = Annotated[
    StrictInt,
    Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER),
]
type JsonValue = (
    StrictBool
    | _SafeInteger
    | StrictStr
    | list["JsonValue"]
    | dict[StrictStr, "JsonValue"]
    | None
)

_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class CanonicalJsonError(ValueError):
    """The value or document violates the tetrabench JSON profile."""


class DuplicateKeyError(CanonicalJsonError):
    """A JSON object contains the same decoded member name more than once."""


def validate_json_value(value: object) -> JsonValue:
    """Validate a value without coercion against the recursive JSON profile."""
    try:
        validated = _JSON_VALUE_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise CanonicalJsonError("value is not strict no-float JSON") from error
    return validated


def dumps_canonical_json(value: object) -> bytes:
    """Return bounded RFC 8785 bytes for a strict no-float JSON value."""
    validated = validate_json_value(value)
    try:
        encoded = rfc8785.dumps(validated)
    except rfc8785.CanonicalizationError as error:
        raise CanonicalJsonError("value cannot be encoded as RFC 8785 JSON") from error
    if len(encoded) > MAX_CANONICAL_JSON_BYTES:
        raise CanonicalJsonError("canonical JSON exceeds 2 MiB")
    return encoded


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _reject_float(_value: str) -> object:
    raise CanonicalJsonError("floating-point JSON values are not allowed")


def loads_canonical_json(data: bytes) -> JsonValue:
    """Parse canonical UTF-8 JSON, rejecting duplicates, floats, and oversize input."""
    if not isinstance(data, bytes):
        raise TypeError("canonical JSON input must be bytes")
    if len(data) > MAX_CANONICAL_JSON_BYTES:
        raise CanonicalJsonError("canonical JSON exceeds 2 MiB")
    try:
        text = data.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanonicalJsonError("input is not valid UTF-8 JSON") from error

    validated = validate_json_value(parsed)
    if dumps_canonical_json(validated) != data:
        raise CanonicalJsonError("input is valid JSON but not RFC 8785 canonical JSON")
    return validated


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of bytes."""
    if not isinstance(data, bytes):
        raise TypeError("SHA-256 input must be bytes")
    return hashlib.sha256(data).hexdigest()
