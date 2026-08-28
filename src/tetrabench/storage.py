"""Pure key construction and validation for the immutable object layout."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PREFIX_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_S3_KEY_BYTES = 1024
MAX_LOGICAL_COMPONENT_BYTES = 255
MAX_LOGICAL_PATH_BYTES = 4095


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("digest must be 64 lowercase hexadecimal characters")
    return value


def validate_identifier(value: str, *, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is not a safe identifier")
    return value


def validate_logical_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("logical path must be a relative POSIX path")
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("logical path must be a normalized relative POSIX path")
    encoded_parts = [part.encode("utf-8") for part in path.parts]
    if any(len(part) > MAX_LOGICAL_COMPONENT_BYTES for part in encoded_parts):
        raise ValueError("logical path component exceeds 255 UTF-8 bytes")
    if len(value.encode("utf-8")) > MAX_LOGICAL_PATH_BYTES:
        raise ValueError("logical path exceeds 4095 UTF-8 bytes")
    return value


def validate_s3_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("S3 key must be a non-empty string")
    if len(value.encode("utf-8")) > MAX_S3_KEY_BYTES:
        raise ValueError("S3 key exceeds 1024 UTF-8 bytes")
    return value


def validate_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise ValueError("prefix must be a string")
    if not prefix:
        return prefix
    validate_logical_path(prefix)
    if any(_PREFIX_SEGMENT_RE.fullmatch(part) is None for part in prefix.split("/")):
        raise ValueError("prefix contains an unsafe segment")
    if prefix.endswith("/"):
        raise ValueError("prefix must not end with a slash")
    return prefix


def _with_prefix(prefix: str, key: str) -> str:
    validate_prefix(prefix)
    return validate_s3_key(f"{prefix}/{key}" if prefix else key)


def content_object_key(sha256: str, *, prefix: str = "") -> str:
    return _with_prefix(prefix, f"objects/sha256/{validate_sha256(sha256)}")


def request_key(run_id: str, request_sha256: str, *, prefix: str = "") -> str:
    run_id = validate_identifier(run_id, name="run_id")
    digest = validate_sha256(request_sha256)
    return _with_prefix(prefix, f"runs/{run_id}/requests/{digest}.json")


def event_key(
    run_id: str,
    attempt_id: str,
    sequence: int,
    event_sha256: str,
    *,
    prefix: str = "",
) -> str:
    run_id = validate_identifier(run_id, name="run_id")
    attempt_id = validate_identifier(attempt_id, name="attempt_id")
    if type(sequence) is not int or not 0 <= sequence <= (1 << 53) - 1:
        raise ValueError("sequence must be a non-negative safe integer")
    digest = validate_sha256(event_sha256)
    return _with_prefix(
        prefix,
        f"runs/{run_id}/events/{attempt_id}/{sequence:016d}-{digest}.json",
    )


def terminal_key(run_id: str, terminal_sha256: str, *, prefix: str = "") -> str:
    run_id = validate_identifier(run_id, name="run_id")
    digest = validate_sha256(terminal_sha256)
    return _with_prefix(prefix, f"runs/{run_id}/terminals/{digest}.json")


def validate_content_object_key(key: str, sha256: str) -> str:
    validate_s3_key(key)
    digest = validate_sha256(sha256)
    suffix = f"objects/sha256/{digest}"
    if key == suffix:
        return key
    marker = f"/{suffix}"
    if not key.endswith(marker):
        raise ValueError("content object key does not match sha256")
    validate_prefix(key[: -len(marker)])
    return key


def verify_content_object(
    data: bytes,
    *,
    sha256: str,
    size: int,
) -> None:
    """Verify local bytes against a content descriptor's identity fields."""
    import hashlib

    if not isinstance(data, bytes):
        raise TypeError("content object data must be bytes")
    if len(data) != size:
        raise ValueError("content object size does not match bytes")
    if hashlib.sha256(data).hexdigest() != validate_sha256(sha256):
        raise ValueError("content object sha256 does not match bytes")
