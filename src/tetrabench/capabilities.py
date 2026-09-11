"""Immutable reasoning metadata. These records never assert inference success."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tetrabench.canonical_json import (
    dumps_canonical_json,
    loads_canonical_json,
    sha256_hex,
)

MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f]+$")]
ChoiceName = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
]
Status = Literal["supported", "unsupported", "unknown", "unavailable"]
EvidenceKind = Literal["explicit", "native-heuristic", "user-assertion"]
_SENSITIVE = re.compile(
    r"api.?key|secret|password|credential|authorization|access.?token|refresh.?token|"
    r"^headers$|^env$|^key$|^token$|cookie",
    re.I,
)


class MetadataError(ValueError):
    """Safe-to-display error, never containing upstream error text or credentials."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MetadataError("duplicate metadata member")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise MetadataError("non-finite metadata number")


def parse_metadata(data: str | bytes) -> Any:
    """Bounded native JSON, allowing finite floats *only inside JSON text*."""
    if not isinstance(data, (str, bytes)):
        raise MetadataError("metadata must be JSON text")
    try:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if len(raw) > MAX_METADATA_BYTES:
            raise MetadataError("metadata exceeds 2 MiB")
        result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        stack = [(result, 0)]
        count = 0
        while stack:
            item, depth = stack.pop()
            count += 1
            if depth > 32 or count > 50000:
                raise MetadataError("metadata nesting or entry limit exceeded")
            if isinstance(item, float) and not math.isfinite(item):
                raise MetadataError("non-finite metadata number")
            if isinstance(item, dict):
                stack.extend((value, depth + 1) for value in item.values())
            elif isinstance(item, list):
                stack.extend((value, depth + 1) for value in item)
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError):
        raise MetadataError("malformed native JSON metadata") from None


def metadata_text(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
    except (ValueError, TypeError, RecursionError):
        raise MetadataError("malformed native metadata") from None
    parse_metadata(text)
    return text


def check_public_metadata(value: Any) -> None:
    """Fail closed on credential-bearing containers rather than redact guesses."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(_SENSITIVE.search(key) for key in item):
                raise MetadataError("credential-bearing metadata is not snapshot-safe")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            if re.search(r"(?:Bearer |sk-[A-Za-z0-9]|-----BEGIN .*PRIVATE KEY)", item):
                raise MetadataError("credential-bearing metadata is not snapshot-safe")


def safe_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        raise MetadataError(
            "expected an endpoint without credentials or query"
        ) from None
    return value


class FrozenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CapabilityIdentity(FrozenRecord):
    """Bound native provider/config identity, not a promise of server acceptance.

    Native-default routes can be bound without a disclosed physical endpoint.
    Snapshot/report unverified_fields preserve that distinction explicitly.
    """

    harness: Name
    harness_version: Name
    native_adapter_version: Name
    requested_model: Name
    resolved_model: Name
    provider_id: Name
    route_id: Name
    protocol: Name
    endpoints: tuple[Name, ...] = Field(max_length=32)
    fallback_policy_json: str = Field(max_length=4096)
    auth_mode: Name
    profile_ref: Name | None = None
    config_digest: Digest
    route_status: Literal["bound", "multiple", "unknown"] = "unknown"

    @field_validator("endpoints")
    @classmethod
    def endpoints_are_public(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            safe_url(value)
        return values

    @field_validator("fallback_policy_json")
    @classmethod
    def policy_is_public(cls, value: str) -> str:
        check_public_metadata(parse_metadata(value))
        return value


class Evidence(FrozenRecord):
    kind: EvidenceKind
    source_url: str = Field(max_length=2048)
    source_sha256: Digest
    hash_scope: Literal["captured-payload", "source-document"] = "captured-payload"
    observed_at: Annotated[
        str, Field(pattern=r"^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$")
    ]
    method: str = Field(min_length=1, max_length=256)

    @field_validator("observed_at")
    @classmethod
    def absolute_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError
        except ValueError:
            raise MetadataError("evidence requires an absolute ISO timestamp") from None
        return value

    @field_validator("source_url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return safe_url(value)


class Binding(FrozenRecord):
    """A native setting location, never a provider-name dispatch table."""

    surface: Literal["options", "native-json", "native-toml"]
    path: tuple[Name, ...] = Field(min_length=1, max_length=16)

    @field_validator("path")
    @classmethod
    def no_secret_binding(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_SENSITIVE.search(component) for component in value):
            raise MetadataError("reasoning bindings cannot target credentials")
        return value


class Setting(FrozenRecord):
    binding: Binding
    value_json: str = Field(max_length=8192)
    remove: bool = False

    @field_validator("value_json")
    @classmethod
    def public_value(cls, value: str) -> str:
        check_public_metadata(parse_metadata(value))
        return value


class Choice(FrozenRecord):
    name: ChoiceName
    settings: tuple[Setting, ...] = Field(default=(), max_length=16)
    normalization: Literal["identity", "changed", "unknown"] = "unknown"
    effective_choice: ChoiceName | None = None
    # The variant's actual merged options or the SDK's mapped value, not its name.
    native_value_json: str | None = Field(default=None, max_length=16384)

    @field_validator("native_value_json")
    @classmethod
    def public_value(cls, value: str | None) -> str | None:
        if value is not None:
            check_public_metadata(parse_metadata(value))
        return value


class Budget(FrozenRecord):
    minimum: int | None = Field(default=None, ge=0, le=(1 << 53) - 1)
    maximum: int | None = Field(default=None, ge=0, le=(1 << 53) - 1)
    binding: Binding | None = None
    requires: tuple[Setting, ...] = Field(default=(), max_length=16)
    output_limit: int | None = Field(default=None, gt=0, le=(1 << 53) - 1)
    less_than_output: bool = False

    @model_validator(mode="after")
    def ordered(self) -> Budget:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise MetadataError("inverted budget bounds")
        return self


class Control(FrozenRecord):
    name: Name
    kind: Literal["choices", "budget", "adaptive", "off", "default", "visibility"]
    status: Status
    choices: tuple[Choice, ...] = Field(default=(), max_length=128)
    budget: Budget | None = None
    # Omitted, null (unrestricted native allowlist), and empty are distinct.
    choices_presence: Literal["omitted", "null", "present"] = "omitted"
    default_json: str | None = Field(default=None, max_length=8192)
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def coherent(self) -> Control:
        names = [item.name for item in self.choices]
        if len(names) != len(set(names)):
            raise MetadataError("duplicate native choice")
        if self.budget is not None and self.kind != "budget":
            raise MetadataError("budget on a non-budget control")
        if self.default_json is not None:
            check_public_metadata(parse_metadata(self.default_json))
        return self


class CapabilitySnapshot(FrozenRecord):
    schema_version: Literal[1] = 1
    identity: CapabilityIdentity
    status: Status
    controls: tuple[Control, ...] = Field(default=(), max_length=32)
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=16)
    metadata_json: str = Field(default="{}", max_length=65536)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)
    adopted_from_digest: Digest | None = None
    capture_identity: CapabilityIdentity | None = None
    verification_policy: Literal["native-startup", "strict-startup"] = Field(
        default="native-startup", exclude_if=lambda value: value == "native-startup"
    )
    unverified_fields: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def bounded_public_snapshot(self) -> CapabilitySnapshot:
        check_public_metadata(parse_metadata(self.metadata_json))
        names = [control.name for control in self.controls]
        if len(names) != len(set(names)):
            raise MetadataError("duplicate control")
        if self.status == "supported" and self.identity.route_status != "bound":
            raise MetadataError("unknown or multiple routes cannot be supported")
        if len(self.to_bytes()) > MAX_SNAPSHOT_BYTES:
            raise MetadataError("capability snapshot exceeds 128 KiB")
        return self

    def to_bytes(self) -> bytes:
        data = dumps_canonical_json(self.model_dump(mode="json"))
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise MetadataError("capability snapshot exceeds 128 KiB")
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> CapabilitySnapshot:
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise MetadataError("capability snapshot exceeds 128 KiB")
        loads_canonical_json(data)
        return cls.model_validate_json(data)

    @property
    def digest(self) -> str:
        return sha256_hex(self.to_bytes())

    def require_current(self, identity: CapabilityIdentity) -> None:
        if self.identity != identity:
            raise MetadataError(
                "capability identity drift; inspect again before adoption"
            )


class CapabilitySnapshotRef(FrozenRecord):
    """Optional inline content-addressed spec field; exclude it from config digest."""

    sha256: Digest
    snapshot_json: str = Field(max_length=MAX_SNAPSHOT_BYTES)

    @model_validator(mode="after")
    def valid_snapshot(self) -> CapabilitySnapshotRef:
        snapshot = CapabilitySnapshot.from_bytes(self.snapshot_json.encode())
        if snapshot.digest != self.sha256:
            raise MetadataError("capability snapshot reference digest mismatch")
        return self

    @classmethod
    def from_snapshot(cls, snapshot: CapabilitySnapshot) -> CapabilitySnapshotRef:
        return cls(sha256=snapshot.digest, snapshot_json=snapshot.to_bytes().decode())

    def snapshot(self) -> CapabilitySnapshot:
        return CapabilitySnapshot.from_bytes(self.snapshot_json.encode())


def make_evidence(
    source_url: str,
    data: str | bytes,
    observed_at: str,
    method: str,
    kind: EvidenceKind = "explicit",
) -> Evidence:
    return Evidence(
        kind=kind,
        source_url=source_url,
        source_sha256=sha256_hex(data.encode() if isinstance(data, str) else data),
        observed_at=observed_at,
        method=method,
    )
