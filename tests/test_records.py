from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest
from pydantic import ValidationError

from tetrabench.canonical_json import sha256_hex
from tetrabench.models import ResolvedPlan
from tetrabench.plan import canonical_model_bytes, parse_canonical_model, plan_digest
from tetrabench.records import (
    AdmissionRecord,
    ArtifactBinding,
    ArtifactInventoryEntry,
    AttemptEvent,
    ConflictRunState,
    ContentObject,
    ContextManifest,
    ContextManifestFile,
    RequestRecord,
    TerminalEvidence,
    TerminalRecord,
    TerminalRunState,
    UnknownRunState,
    interpret_terminal_records,
    new_admission,
    transition_admission,
    validate_attempt_id,
    validate_monotonic_events,
    validate_run_id,
)
from tetrabench.storage import (
    admission_key,
    content_object_key,
    event_key,
    request_key,
    terminal_key,
    validate_logical_path,
    validate_s3_key,
    verify_content_object,
)

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _plan(*, context: tuple[dict[str, object], ...] = ()) -> ResolvedPlan:
    return ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "systems-design",
            "controller": {"kind": "local"},
            "execution": {"kind": "docker"},
            "storage": None,
            "selection": {},
            "harbor": {},
            "context": context,
            "trials": (),
            "runnable": False,
            "not_runnable_reasons": ("empty",),
        }
    )


def _content(data: bytes, name: str = "") -> ContentObject:
    digest = sha256_hex(data)
    return ContentObject(
        sha256=digest,
        key=content_object_key(digest, prefix=name),
        size=len(data),
        media_type="application/json",
    )


def _terminal() -> TerminalRecord:
    config = _content(b"config", "bench")
    lock = _content(b"lock", "bench")
    result = _content(b"result", "bench")
    return TerminalRecord(
        schema_version=1,
        run_id="run-1",
        request_sha256="1" * 64,
        winning_attempt_id="attempt-1",
        outcome="succeeded",
        harbor_version="0.22.0",
        artifacts=(
            ArtifactInventoryEntry(logical_path="job/config.json", content=config),
            ArtifactInventoryEntry(logical_path="job/lock.json", content=lock),
            ArtifactInventoryEntry(logical_path="job/result.json", content=result),
        ),
        harbor_config=ArtifactBinding(
            logical_path="job/config.json", sha256=config.sha256
        ),
        harbor_lock=ArtifactBinding(logical_path="job/lock.json", sha256=lock.sha256),
        harbor_result=ArtifactBinding(
            logical_path="job/result.json", sha256=result.sha256
        ),
        evidence=(TerminalEvidence(type="controller", message="published last"),),
        warnings=(),
    )


def test_validated_identifiers_and_key_builders() -> None:
    digest = "a" * 64
    assert validate_run_id("run-1") == "run-1"
    assert validate_attempt_id("attempt.1") == "attempt.1"
    assert content_object_key(digest, prefix="tenant/v1") == (
        f"tenant/v1/objects/sha256/{digest}"
    )
    assert request_key("run-1", digest) == f"runs/run-1/requests/{digest}.json"
    assert event_key("run-1", "attempt-1", 12, digest) == (
        f"runs/run-1/events/attempt-1/0000000000000012-{digest}.json"
    )
    assert terminal_key("run-1", digest) == f"runs/run-1/terminals/{digest}.json"
    assert admission_key("run-1") == "runs/run-1/admission.json"


def test_admission_record_has_canonical_history_and_one_owner() -> None:
    plan = _plan()
    manifest = ContextManifest(schema_version=1, files=())
    request = RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )
    prepared = new_admission(request, timestamp="2026-08-28T20:00:00Z")
    running = transition_admission(
        prepared,
        "running",
        timestamp="2026-08-28T20:00:01Z",
        owner_function_call_id="fc-1",
    )
    cancelling = transition_admission(
        running, "cancelling", timestamp="2026-08-28T20:00:02Z"
    )
    cancelled = transition_admission(
        cancelling, "cancelled", timestamp="2026-08-28T20:00:03Z"
    )

    assert cancelled.revision == 3
    assert tuple(item.state for item in cancelled.history) == (
        "prepared",
        "running",
        "cancelling",
        "cancelled",
    )
    assert {item.owner_function_call_id for item in cancelled.history[1:]} == {"fc-1"}
    assert b"." not in canonical_model_bytes(cancelled).split(b"timestamp", 1)[0]
    with pytest.raises(ValidationError):
        AdmissionRecord.model_validate(cancelled.model_dump() | {"extra": True})
    assert cancelled.model_config["frozen"] is True
    with pytest.raises(ValueError, match="owner cannot change"):
        transition_admission(
            running,
            "terminal",
            timestamp="2026-08-28T20:00:02Z",
            owner_function_call_id="fc-other",
            terminal_sha256="a" * 64,
        )


def test_admission_rejects_invalid_transitions_and_terminal_without_digest() -> None:
    plan = _plan()
    manifest = ContextManifest(schema_version=1, files=())
    request = RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )
    prepared = new_admission(request, timestamp="2026-08-28T20:00:00Z")
    with pytest.raises(ValidationError, match="invalid admission transition"):
        transition_admission(
            prepared,
            "terminal",
            timestamp="2026-08-28T20:00:01Z",
            owner_function_call_id="fc-1",
            terminal_sha256="a" * 64,
        )
    running = transition_admission(
        prepared,
        "running",
        timestamp="2026-08-28T20:00:01Z",
        owner_function_call_id="fc-1",
    )
    with pytest.raises(ValidationError, match="terminal digest"):
        transition_admission(running, "terminal", timestamp="2026-08-28T20:00:02Z")
    with pytest.raises(ValidationError, match="move backwards"):
        transition_admission(running, "failed", timestamp="2026-08-28T19:59:59Z")
    unowned_cancelled = transition_admission(
        prepared, "cancelled", timestamp="2026-08-28T20:00:01Z"
    )
    with pytest.raises(ValidationError, match="unclaimed cancelled"):
        transition_admission(
            unowned_cancelled,
            "terminal",
            timestamp="2026-08-28T20:00:02Z",
            owner_function_call_id="fc-1",
            terminal_sha256="a" * 64,
        )


@pytest.mark.parametrize(
    "value",
    ["", "UPPER", "../run", "run/name", "-run", "run ", "a" * 65],
)
def test_identifiers_reject_unsafe_values(value: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        validate_run_id(value)
    with pytest.raises((ValueError, ValidationError)):
        validate_attempt_id(value)


@pytest.mark.parametrize("prefix", ["/root", "../root", "root/", "root//x", "root bad"])
def test_key_builders_reject_unsafe_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError, match=r"prefix|logical path"):
        content_object_key("a" * 64, prefix=prefix)


def test_s3_key_and_logical_path_utf8_byte_limits() -> None:
    assert validate_s3_key("é" * 512) == "é" * 512
    with pytest.raises(ValueError, match="1024 UTF-8 bytes"):
        validate_s3_key("é" * 513)

    component = "é" * 127
    assert validate_logical_path(component) == component
    with pytest.raises(ValueError, match="component exceeds 255"):
        validate_logical_path("é" * 128)

    longest = "/".join("a" * 255 for _ in range(16))
    assert len(longest.encode()) == 4095
    assert validate_logical_path(longest) == longest
    with pytest.raises(ValueError, match="path exceeds 4095"):
        validate_logical_path(f"{longest}/x")

    with pytest.raises(ValueError, match="S3 key exceeds 1024"):
        content_object_key("a" * 64, prefix="/".join(["p" * 240] * 4))


def test_content_descriptor_binds_key_size_and_bytes() -> None:
    descriptor = _content(b"payload")
    verify_content_object(b"payload", sha256=descriptor.sha256, size=descriptor.size)
    with pytest.raises(ValidationError, match="key does not match"):
        ContentObject.model_validate(descriptor.model_dump() | {"sha256": "0" * 64})
    with pytest.raises(ValueError, match="size"):
        verify_content_object(b"payload", sha256=descriptor.sha256, size=999)
    with pytest.raises(ValueError, match="sha256"):
        verify_content_object(b"tampered", sha256=descriptor.sha256, size=8)


def test_context_manifest_golden_bytes_and_digest() -> None:
    content = _content(b"hello\n")
    manifest = ContextManifest(
        schema_version=1,
        files=(
            ContextManifestFile(
                destination="docs/input.txt", mode=420, content=content
            ),
        ),
    )
    encoded = canonical_model_bytes(manifest)
    assert encoded == (
        b'{"files":[{"content":{"key":"objects/sha256/5891b5b522d5df086d0ff0b'
        b'110fbd9d21bb4fc7163af34d08286a2e846f6be03","media_type":"application/json"'
        b',"sha256":"5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6'
        b'be03","size":6},"destination":"docs/input.txt","mode":420}],"schema_version":1}'
    )
    assert sha256_hex(encoded) == (
        "cfff7c33d925f6c6d91890785eef80052b3b01c08b817599d80ffbe9aceceb9a"
    )


def test_context_manifest_enforces_absolute_limits() -> None:
    content = ContentObject(
        sha256=EMPTY_SHA,
        key=content_object_key(EMPTY_SHA),
        size=16 * 1024 * 1024 + 1,
        media_type="application/octet-stream",
    )
    with pytest.raises(ValidationError, match="16 MiB"):
        ContextManifest(
            schema_version=1,
            files=(ContextManifestFile(destination="x", mode=420, content=content),),
        )

    small = _content(b"")
    with pytest.raises(ValidationError, match="256 files"):
        ContextManifest(
            schema_version=1,
            files=tuple(
                ContextManifestFile(
                    destination=f"files/{index}", mode=420, content=small
                )
                for index in range(257)
            ),
        )

    at_file_limit = content.model_copy(update={"size": 16 * 1024 * 1024})
    with pytest.raises(ValidationError, match="128 MiB"):
        ContextManifest(
            schema_version=1,
            files=tuple(
                ContextManifestFile(
                    destination=f"large/{index}", mode=420, content=at_file_limit
                )
                for index in range(9)
            ),
        )


def test_request_golden_bytes_digest_and_direct_deserialization() -> None:
    plan = _plan()
    manifest = ContextManifest(schema_version=1, files=())
    request = RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )
    encoded = canonical_model_bytes(request)
    assert encoded == (
        b'{"context_manifest":{"files":[],"schema_version":1},"context_manifest_sha256"'
        b':"5446897477634347b30b8a2357fe5306f398dbd42bca89ce4971d2a90164140e","p'
        b'lan":{"context":[],"controller":{"kind":"local"},"execution":{"kind":"docker"'
        b'},"harbor":{"agent_name":"oracle","attempts":1,"concurrency":1,"model_name"'
        b':null},"not_runnable_reasons":["empty"],"runnable":false,"schema_version":1,"se'
        b'ction":"systems-design","selection":{"exclude":[],"include":[]},"storage":null'
        b',"trials":[]},"plan_sha256":"83a27422bf5a7c90b5e3dede14a6644912a81b449e5'
        b'3424a23b0c5dcfb316e28","run_id":"run-1","schema_version":1}'
    )
    assert sha256_hex(encoded) == (
        "d272e9fd8d2e840dc2f7244430814ca90ee5389695616026387dcb7e6d79f7ba"
    )
    assert parse_canonical_model(encoded, RequestRecord) == request
    with pytest.raises(ValidationError, match="plan_sha256"):
        RequestRecord.model_validate(request.model_dump() | {"plan_sha256": "0" * 64})


def test_request_rejects_plan_context_manifest_disagreement() -> None:
    content = _content(b"")
    manifest = ContextManifest(
        schema_version=1,
        files=(ContextManifestFile(destination="x", mode=420, content=content),),
    )
    plan = _plan()
    with pytest.raises(ValidationError, match="disagree"):
        RequestRecord(
            schema_version=1,
            run_id="run",
            plan_sha256=plan_digest(plan),
            plan=plan,
            context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
            context_manifest=manifest,
        )


def test_event_golden_bytes_digest_and_deep_immutability() -> None:
    event = AttemptEvent(
        schema_version=1,
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=7,
        type="harbor.started",
        payload={"child": {"ids": ["sb-1"]}, "ready": True},
    )
    encoded = canonical_model_bytes(event)
    assert encoded == (
        b'{"attempt_id":"attempt-1","payload":{"child":{"ids":["sb-1"]},"ready":true'
        b'},"run_id":"run-1","schema_version":1,"sequence":7,"type":"harbor.started"}'
    )
    assert sha256_hex(encoded) == (
        "26bee5526415f09728a7e808f00cf41ed3c7a1be563b6ddc60c5af247d72428d"
    )
    assert parse_canonical_model(encoded, AttemptEvent) == event
    assert isinstance(event.payload, Mapping)
    payload = cast(dict[str, object], event.payload)
    with pytest.raises(TypeError):
        payload["new"] = "value"
    nested = cast(dict[str, object], payload["child"])
    with pytest.raises(TypeError):
        nested["ids"] = ()
    with pytest.raises(ValidationError):
        AttemptEvent(
            schema_version=1,
            run_id="run",
            attempt_id="attempt",
            sequence=0,
            type="bad",
            payload={"float": 1.0},
        )


def test_event_sequence_is_monotonic_within_one_attempt() -> None:
    def event(sequence: int, attempt: str = "a") -> AttemptEvent:
        return AttemptEvent(
            schema_version=1,
            run_id="run",
            attempt_id=attempt,
            sequence=sequence,
            type="step",
            payload={},
        )

    validate_monotonic_events((event(0), event(0, "b"), event(2), event(1, "b")))
    with pytest.raises(ValueError, match="increase"):
        validate_monotonic_events((event(1), event(1)))


def test_event_keys_scope_sequence_reuse_by_attempt() -> None:
    digest = "a" * 64
    first = event_key("run", "attempt-a", 0, digest)
    second = event_key("run", "attempt-b", 0, digest)
    assert first != second
    assert "/events/attempt-a/0000000000000000-" in first
    assert "/events/attempt-b/0000000000000000-" in second


def test_terminal_golden_bytes_digest_and_inventory_invariants() -> None:
    terminal = _terminal()
    encoded = canonical_model_bytes(terminal)
    assert encoded == (
        b'{"artifacts":[{"content":{"key":"bench/objects/sha256/b79606fb3afea5bd160'
        b'9ed40b622142f1c98125abcfe89a76a661b0e8e343910","media_type":"application/js'
        b'on","sha256":"b79606fb3afea5bd1609ed40b622142f1c98125abcfe89a76a661b0e8e3'
        b'43910","size":6},"logical_path":"job/config.json"},{"content":{"key":"bench/ob'
        b"jects/sha256/0c030586945fe504b604ecc2e875c38ede400cd5cd73da9730302162e6b02c"
        b'6f","media_type":"application/json","sha256":"0c030586945fe504b604ecc2e875c38ed'
        b'e400cd5cd73da9730302162e6b02c6f","size":4},"logical_path":"job/lock.json"'
        b'},{"content":{"key":"bench/objects/sha256/f6a214f7a5fcda0c2cee9660b7fc29f5'
        b'649e3c68aad48e20e950137c98913a68","media_type":"application/json","sha256":"f'
        b'6a214f7a5fcda0c2cee9660b7fc29f5649e3c68aad48e20e950137c98913a68","size":6'
        b'},"logical_path":"job/result.json"}],"evidence":[{"message":"published '
        b'last","type":"controller"}],"harbor_config":{"logical_path":"job/config.json"'
        b',"sha256":"b79606fb3afea5bd1609ed40b622142f1c98125abcfe89a76a661b0e8e343'
        b'910"},"harbor_lock":{"logical_path":"job/lock.json","sha256":"0c030586945fe504'
        b'b604ecc2e875c38ede400cd5cd73da9730302162e6b02c6f"},"harbor_result":{"logi'
        b'cal_path":"job/result.json","sha256":"f6a214f7a5fcda0c2cee9660b7fc29f5649e3'
        b'c68aad48e20e950137c98913a68"},"harbor_v'
        b'ersion":"0.22.0","outcome":"succeeded","request_sha256":"'
        + b"1"
        * 64
        + b'","run_id":"run-1","schema'
        b'_version":1,"warnings":[],"winning_attempt_id":"attempt-1"}'
    )
    assert sha256_hex(encoded) == (
        "567b23ed4cb3d0bbf69da29a7975047789cf3fb1ee4fa3c9b89c895769fb8337"
    )
    assert parse_canonical_model(encoded, TerminalRecord) == terminal

    duplicate = terminal.artifacts[0]
    with pytest.raises(ValidationError, match="logical paths"):
        TerminalRecord.model_validate(
            terminal.model_dump() | {"artifacts": (duplicate, duplicate)}
        )
    with pytest.raises(ValidationError, match="must match the inventory"):
        TerminalRecord.model_validate(
            terminal.model_dump()
            | {
                "harbor_result": {
                    "logical_path": "job/result.json",
                    "sha256": "0" * 64,
                }
            }
        )


def test_terminal_success_and_failure_artifact_bindings() -> None:
    terminal = _terminal()
    with pytest.raises(ValidationError, match="successful terminal requires"):
        TerminalRecord.model_validate(terminal.model_dump() | {"harbor_result": None})

    failed = TerminalRecord.model_validate(
        terminal.model_dump()
        | {
            "outcome": "failed",
            "artifacts": (),
            "harbor_config": None,
            "harbor_lock": None,
            "harbor_result": None,
        }
    )
    assert failed.harbor_config is None

    shared = _content(b"same", "bench")
    succeeded = TerminalRecord.model_validate(
        terminal.model_dump()
        | {
            "artifacts": tuple(
                ArtifactInventoryEntry(logical_path=path, content=shared)
                for path in ("job/config.json", "job/lock.json", "job/result.json")
            ),
            "harbor_config": {
                "logical_path": "job/config.json",
                "sha256": shared.sha256,
            },
            "harbor_lock": {
                "logical_path": "job/lock.json",
                "sha256": shared.sha256,
            },
            "harbor_result": {
                "logical_path": "job/result.json",
                "sha256": shared.sha256,
            },
        }
    )
    assert succeeded.harbor_config is not None
    assert succeeded.harbor_lock is not None
    assert succeeded.harbor_config.sha256 == succeeded.harbor_lock.sha256


def test_terminal_read_state_zero_one_many() -> None:
    terminal = _terminal()
    digest = sha256_hex(canonical_model_bytes(terminal))
    assert isinstance(interpret_terminal_records("run-1", ()), UnknownRunState)

    one = interpret_terminal_records("run-1", ((digest, terminal),))
    assert isinstance(one, TerminalRunState)
    assert one.terminal is terminal

    other = terminal.model_copy(update={"outcome": "failed"})
    other_digest = sha256_hex(canonical_model_bytes(other))
    many = interpret_terminal_records(
        "run-1", ((digest, terminal), (other_digest, other))
    )
    assert isinstance(many, ConflictRunState)
    assert many.terminal_sha256s == (digest, other_digest)


@pytest.mark.parametrize(
    "path", ["", ".", "/x", "../x", "a/../x", "a//x", "a\\x", "a\nx"]
)
def test_logical_artifact_paths_are_safe(path: str) -> None:
    with pytest.raises(ValidationError, match=r"logical_path|logical path"):
        ArtifactInventoryEntry(logical_path=path, content=_content(b"x"))
