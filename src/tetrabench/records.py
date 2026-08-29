"""Canonical immutable run records and their cross-field invariants."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from tetrabench.canonical_json import JsonValue, dumps_canonical_json, sha256_hex
from tetrabench.models import (
    FrozenRecord,
    NonEmptyString,
    RecordIdentifier,
    ResolvedPlan,
    SchemaVersion,
    Sha256,
)
from tetrabench.storage import validate_content_object_key, validate_logical_path

RunId = RecordIdentifier
AttemptId = RecordIdentifier
MediaType = Annotated[
    str,
    Field(
        min_length=3,
        max_length=127,
        pattern=r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$",
    ),
]


def _validate_timestamp(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


Timestamp = Annotated[
    str,
    Field(
        pattern=(
            r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
            r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
        )
    ),
    AfterValidator(_validate_timestamp),
]
AdmissionState = Literal[
    "prepared", "running", "cancelling", "cancelled", "terminal", "failed"
]


def utc_now_timestamp() -> str:
    """Return an integer-second RFC 3339 timestamp for canonical records."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_digest(model: FrozenRecord) -> str:
    return sha256_hex(
        dumps_canonical_json(model.model_dump(mode="json", by_alias=True))
    )


class ContentObject(FrozenRecord):
    """A content-addressed immutable byte object."""

    sha256: Sha256
    key: NonEmptyString
    size: Annotated[int, Field(ge=0, le=(1 << 53) - 1)]
    media_type: MediaType

    @model_validator(mode="after")
    def validate_key_digest(self) -> ContentObject:
        validate_content_object_key(self.key, self.sha256)
        return self


class ContextManifestFile(FrozenRecord):
    destination: NonEmptyString
    mode: Literal[420, 493]
    content: ContentObject

    @model_validator(mode="after")
    def validate_destination(self) -> ContextManifestFile:
        validate_logical_path(self.destination)
        return self


class ContextManifest(FrozenRecord):
    schema_version: SchemaVersion
    files: tuple[ContextManifestFile, ...]

    @model_validator(mode="after")
    def validate_files(self) -> ContextManifest:
        if len(self.files) > 256:
            raise ValueError("context manifest contains more than 256 files")
        destinations = [item.destination for item in self.files]
        if len(destinations) != len(set(destinations)):
            raise ValueError("context manifest destinations must be unique")
        if any(item.content.size > 16 * 1024 * 1024 for item in self.files):
            raise ValueError("context manifest file exceeds 16 MiB")
        if sum(item.content.size for item in self.files) > 128 * 1024 * 1024:
            raise ValueError("context manifest exceeds 128 MiB")
        return self


class RequestRecord(FrozenRecord):
    schema_version: SchemaVersion
    run_id: RunId
    plan_sha256: Sha256
    plan: ResolvedPlan
    context_manifest_sha256: Sha256
    context_manifest: ContextManifest

    @model_validator(mode="after")
    def validate_embedded_digests(self) -> RequestRecord:
        if _canonical_digest(self.plan) != self.plan_sha256:
            raise ValueError("plan_sha256 does not match embedded plan")
        if _canonical_digest(self.context_manifest) != self.context_manifest_sha256:
            raise ValueError(
                "context_manifest_sha256 does not match embedded context manifest"
            )
        plan_files = tuple(
            (item.destination, item.mode, item.size, item.sha256)
            for item in self.plan.context
        )
        manifest_files = tuple(
            (
                item.destination,
                item.mode,
                item.content.size,
                item.content.sha256,
            )
            for item in self.context_manifest.files
        )
        if plan_files != manifest_files:
            raise ValueError("embedded plan and context manifest disagree")
        return self


class AdmissionRevision(FrozenRecord):
    """One immutable entry retained inside the mutable admission record."""

    revision: Annotated[int, Field(ge=0, le=(1 << 53) - 1)]
    state: AdmissionState
    timestamp: Timestamp
    owner_function_call_id: NonEmptyString | None = None
    terminal_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_state_fields(self) -> AdmissionRevision:
        if self.state == "prepared":
            if self.owner_function_call_id is not None:
                raise ValueError("prepared admission cannot have an owner")
        elif self.state == "cancelled" and self.revision == 1:
            # prepared -> cancelled has no controller owner.
            pass
        elif self.owner_function_call_id is None:
            raise ValueError(f"{self.state} admission revision requires an owner")
        if (self.state == "terminal") != (self.terminal_sha256 is not None):
            raise ValueError("only terminal admission revisions bind a terminal digest")
        return self


class AdmissionRecord(FrozenRecord):
    """Canonical CAS coordination state at one fixed per-run key.

    The complete revision history is carried forward on every overwrite. It is
    coordination evidence only; an immutable terminal object remains the proof
    of a terminal run.
    """

    schema_version: SchemaVersion
    revision: Annotated[int, Field(ge=0, le=(1 << 53) - 1)]
    run_id: RunId
    request_sha256: Sha256
    plan_sha256: Sha256
    state: AdmissionState
    owner_function_call_id: NonEmptyString | None = None
    terminal_sha256: Sha256 | None = None
    created_at: Timestamp
    updated_at: Timestamp
    history: tuple[AdmissionRevision, ...]

    @model_validator(mode="after")
    def validate_history(self) -> AdmissionRecord:
        if not self.history:
            raise ValueError("admission history cannot be empty")
        if tuple(item.revision for item in self.history) != tuple(
            range(len(self.history))
        ):
            raise ValueError("admission revisions must be contiguous from zero")
        first = self.history[0]
        if (
            first.state != "prepared"
            or first.owner_function_call_id is not None
            or first.terminal_sha256 is not None
        ):
            raise ValueError("admission history must begin prepared and unowned")
        if self.revision != len(self.history) - 1:
            raise ValueError("admission revision must match its history")
        latest = self.history[-1]
        if (
            self.state,
            self.owner_function_call_id,
            self.terminal_sha256,
            self.updated_at,
        ) != (
            latest.state,
            latest.owner_function_call_id,
            latest.terminal_sha256,
            latest.timestamp,
        ):
            raise ValueError("admission fields must match the latest revision")
        if self.created_at != first.timestamp:
            raise ValueError("admission created_at must match revision zero")
        timestamps = tuple(item.timestamp for item in self.history)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("admission timestamps must not move backwards")
        self._validate_transitions()
        return self

    def _validate_transitions(self) -> None:
        allowed: dict[str, set[str]] = {
            "prepared": {"running", "cancelled"},
            "running": {"cancelling", "terminal", "failed"},
            "cancelling": {"cancelled", "terminal", "failed"},
            "failed": {"terminal"},
            "cancelled": {"terminal"},
            "terminal": set(),
        }
        owner: str | None = None
        for previous, current in zip(self.history, self.history[1:], strict=False):
            if current.state not in allowed[previous.state]:
                raise ValueError(
                    f"invalid admission transition {previous.state}->{current.state}"
                )
            if previous.state == "prepared" and current.state == "cancelled":
                if current.owner_function_call_id is not None:
                    raise ValueError("unclaimed cancellation cannot introduce an owner")
            elif (
                previous.state == "cancelled"
                and previous.owner_function_call_id is None
            ):
                raise ValueError(
                    "an unclaimed cancelled admission cannot become terminal"
                )
            else:
                current_owner = current.owner_function_call_id
                if owner is None:
                    owner = current_owner
                if current_owner != owner:
                    raise ValueError("admission owner cannot change across revisions")


def new_admission(
    request: RequestRecord,
    *,
    timestamp: str,
) -> AdmissionRecord:
    revision = AdmissionRevision(
        revision=0,
        state="prepared",
        timestamp=timestamp,
    )
    return AdmissionRecord(
        schema_version=1,
        revision=0,
        run_id=request.run_id,
        request_sha256=_canonical_digest(request),
        plan_sha256=request.plan_sha256,
        state="prepared",
        created_at=timestamp,
        updated_at=timestamp,
        history=(revision,),
    )


def transition_admission(
    admission: AdmissionRecord,
    state: AdmissionState,
    *,
    timestamp: str,
    owner_function_call_id: str | None = None,
    terminal_sha256: str | None = None,
) -> AdmissionRecord:
    """Build and validate the next admission revision."""
    owner = owner_function_call_id
    if admission.owner_function_call_id is not None:
        if owner is not None and owner != admission.owner_function_call_id:
            raise ValueError("admission owner cannot change")
        owner = admission.owner_function_call_id
    revision = AdmissionRevision(
        revision=admission.revision + 1,
        state=state,
        timestamp=timestamp,
        owner_function_call_id=owner,
        terminal_sha256=terminal_sha256,
    )
    return AdmissionRecord.model_validate(
        admission.model_dump()
        | {
            "revision": revision.revision,
            "state": revision.state,
            "owner_function_call_id": revision.owner_function_call_id,
            "terminal_sha256": revision.terminal_sha256,
            "updated_at": revision.timestamp,
            "history": (*admission.history, revision),
        }
    )


def _freeze_json(value: JsonValue) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise TypeError("immutable payload contains a non-JSON value")


class AttemptEvent(FrozenRecord):
    schema_version: SchemaVersion
    run_id: RunId
    attempt_id: AttemptId
    sequence: Annotated[int, Field(ge=0, le=(1 << 53) - 1)]
    type: Annotated[
        str,
        Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]*$"),
    ]
    payload: object

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> object:
        from tetrabench.canonical_json import validate_json_value

        return _freeze_json(validate_json_value(value))

    @field_serializer("payload")
    def serialize_payload(self, value: object) -> JsonValue:
        return _thaw_json(value)


def validate_monotonic_events(events: Sequence[AttemptEvent]) -> None:
    """Require one run and a monotonically increasing sequence per attempt."""
    run_id = events[0].run_id if events else None
    previous_by_attempt: dict[str, int] = {}
    for event in events:
        if event.run_id != run_id:
            raise ValueError("events must belong to one run")
        previous = previous_by_attempt.get(event.attempt_id, -1)
        if event.sequence <= previous:
            raise ValueError("event sequence must increase monotonically per attempt")
        previous_by_attempt[event.attempt_id] = event.sequence


class ArtifactInventoryEntry(FrozenRecord):
    logical_path: NonEmptyString
    content: ContentObject

    @model_validator(mode="after")
    def validate_path(self) -> ArtifactInventoryEntry:
        validate_logical_path(self.logical_path)
        return self


class TerminalEvidence(FrozenRecord):
    type: Annotated[
        str,
        Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]*$"),
    ]
    message: NonEmptyString


class ArtifactBinding(FrozenRecord):
    logical_path: NonEmptyString
    sha256: Sha256

    @model_validator(mode="after")
    def validate_path(self) -> ArtifactBinding:
        validate_logical_path(self.logical_path)
        return self


class TerminalRecord(FrozenRecord):
    schema_version: SchemaVersion
    run_id: RunId
    request_sha256: Sha256
    winning_attempt_id: AttemptId
    outcome: Literal["succeeded", "failed", "cancelled"]
    harbor_version: Literal["0.22.0"]
    artifacts: tuple[ArtifactInventoryEntry, ...]
    harbor_config: ArtifactBinding | None = None
    harbor_lock: ArtifactBinding | None = None
    harbor_result: ArtifactBinding | None = None
    evidence: tuple[TerminalEvidence, ...]
    warnings: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_inventory(self) -> TerminalRecord:
        paths = [item.logical_path for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact logical paths must be unique")
        bindings = (self.harbor_config, self.harbor_lock, self.harbor_result)
        if self.outcome == "succeeded" and any(binding is None for binding in bindings):
            raise ValueError(
                "successful terminal requires config, lock, and result bindings"
            )
        inventory = {
            (item.logical_path, item.content.sha256) for item in self.artifacts
        }
        if any(
            binding is not None
            and (binding.logical_path, binding.sha256) not in inventory
            for binding in bindings
        ):
            raise ValueError("terminal artifact binding must match the inventory")
        return self


class UnknownRunState(FrozenRecord):
    state: Literal["unknown_or_nonterminal"] = "unknown_or_nonterminal"
    run_id: RunId


class TerminalRunState(FrozenRecord):
    state: Literal["terminal"] = "terminal"
    run_id: RunId
    terminal_sha256: Sha256
    terminal: TerminalRecord

    @model_validator(mode="after")
    def validate_terminal(self) -> TerminalRunState:
        if self.terminal.run_id != self.run_id:
            raise ValueError("terminal run ID does not match read state")
        if _canonical_digest(self.terminal) != self.terminal_sha256:
            raise ValueError("terminal_sha256 does not match terminal")
        return self


class ConflictRunState(FrozenRecord):
    state: Literal["conflict"] = "conflict"
    run_id: RunId
    terminal_sha256s: tuple[Sha256, ...]
    reasons: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_conflict(self) -> ConflictRunState:
        if len(set(self.terminal_sha256s)) < 2 and not self.reasons:
            raise ValueError("conflict requires multiple terminals or a reason")
        if len(self.terminal_sha256s) != len(set(self.terminal_sha256s)):
            raise ValueError("conflict terminal digests must be unique")
        return self


RunReadState = Annotated[
    UnknownRunState | TerminalRunState | ConflictRunState,
    Field(discriminator="state"),
]


def validate_run_id(value: str) -> str:
    return UnknownRunState(run_id=value).run_id


def validate_attempt_id(value: str) -> str:
    from pydantic import TypeAdapter

    return TypeAdapter(AttemptId).validate_python(value, strict=True)


def interpret_terminal_records(
    run_id: str,
    terminals: Sequence[tuple[str, TerminalRecord]],
) -> UnknownRunState | TerminalRunState | ConflictRunState:
    """Interpret already validated visible terminals without claiming incompletion."""
    validated_run_id = UnknownRunState(run_id=run_id).run_id
    if not terminals:
        return UnknownRunState(run_id=validated_run_id)
    digests: list[str] = []
    for digest, terminal in terminals:
        state = TerminalRunState(
            run_id=validated_run_id,
            terminal_sha256=digest,
            terminal=terminal,
        )
        digests.append(state.terminal_sha256)
    if len(digests) == 1:
        digest, terminal = terminals[0]
        return TerminalRunState(
            run_id=validated_run_id,
            terminal_sha256=digest,
            terminal=terminal,
        )
    return ConflictRunState(
        run_id=validated_run_id,
        terminal_sha256s=tuple(digests),
        reasons=("multiple valid terminal records are visible",),
    )
