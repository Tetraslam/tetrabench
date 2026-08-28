"""Typed planning foundations for tetrabench."""

from importlib.metadata import version

from tetrabench.canonical_json import (
    MAX_CANONICAL_JSON_BYTES,
    CanonicalJsonError,
    DuplicateKeyError,
    JsonValue,
    dumps_canonical_json,
    loads_canonical_json,
    sha256_hex,
    validate_json_value,
)

__version__ = version("tetrabench")

__all__ = [
    "MAX_CANONICAL_JSON_BYTES",
    "CanonicalJsonError",
    "DuplicateKeyError",
    "JsonValue",
    "__version__",
    "dumps_canonical_json",
    "loads_canonical_json",
    "sha256_hex",
    "validate_json_value",
]
