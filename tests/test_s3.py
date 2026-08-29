from __future__ import annotations

import base64
import hashlib
import io
import os
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from tetrabench.canonical_json import sha256_hex
from tetrabench.models import (
    AwsStorageConfig,
    ResolvedContextFile,
    ResolvedPlan,
    TigrisStorageConfig,
)
from tetrabench.plan import canonical_model_bytes, plan_digest
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
    TerminalRecord,
    TerminalRunState,
    UnknownRunState,
    new_admission,
    transition_admission,
)
from tetrabench.s3 import (
    AdmissionRead,
    S3CasConflictError,
    S3ConflictError,
    S3IntegrityError,
    S3Store,
    create_s3_client,
)
from tetrabench.storage import admission_key, event_key, request_key, terminal_key


@dataclass
class _StoredObject:
    body: bytes
    content_type: str
    metadata: dict[str, str]
    checksum_sha256: str
    content_length: int | None = None

    @property
    def etag(self) -> str:
        return f'"{hashlib.sha256(self.body).hexdigest()[:32]}"'


class _ChunkTrackedBody(io.BytesIO):
    def __init__(self, data: bytes, reads: list[int]) -> None:
        super().__init__(data)
        self._reads = reads

    def read(self, size: int | None = -1) -> bytes:
        self._reads.append(-1 if size is None else size)
        return super().read(size)


class FakeS3Client:
    """One deterministic in-memory implementation of the used boto3 surface."""

    def __init__(self) -> None:
        self.provider = "aws"
        self.objects: dict[str, _StoredObject] = {}
        self.operations: list[tuple[str, str]] = []
        self.list_calls = 0
        self.page_size = 1000
        self.lag: dict[tuple[str, str], int] = {}
        self.calls: dict[tuple[str, str], int] = {}
        self.read_sizes: list[int] = []
        self.managed_uploads = 0
        self.skip_managed_body = False
        self.put_barrier: threading.Barrier | None = None
        self.put_error: ClientError | None = None
        self._lock = threading.Lock()

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]:
        if self.put_error is not None:
            raise self.put_error
        key = kwargs["Key"]
        body = kwargs["Body"]
        data = body if isinstance(body, bytes) else body.read()
        assert isinstance(data, bytes)
        assert len(data) == kwargs["ContentLength"]
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
        assert checksum == kwargs["ChecksumSHA256"]
        self.operations.append(("put", key))
        with self._lock:
            existing = self.objects.get(key)
            if kwargs.get("IfNoneMatch") == "*" and existing is not None:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            if "IfMatch" in kwargs and (
                existing is None or existing.etag != kwargs["IfMatch"]
            ):
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            self.objects[key] = _StoredObject(
                body=data,
                content_type=kwargs["ContentType"],
                metadata=dict(kwargs["Metadata"]),
                checksum_sha256=checksum,
            )
            etag = self.objects[key].etag
        conditional = {"IfNoneMatch", "IfMatch"} & kwargs.keys()
        if self.put_barrier is not None and not conditional:
            self.put_barrier.wait(timeout=5)
        return {"ETag": etag}

    def upload_fileobj(self, **kwargs: Any) -> None:
        key = kwargs["Key"]
        stream = kwargs["Fileobj"]
        self.operations.append(("upload_fileobj", key))
        self.managed_uploads += 1
        if self.skip_managed_body:
            data = b""
            content_length = os.fstat(stream.fileno()).st_size
        else:
            chunks: list[bytes] = []
            while chunk := stream.read(1024 * 1024):
                chunks.append(chunk)
            data = b"".join(chunks)
            content_length = len(data)
        extra = kwargs["ExtraArgs"]
        self.objects[key] = _StoredObject(
            body=data,
            content_type=extra["ContentType"],
            metadata=dict(extra["Metadata"]),
            checksum_sha256="multipart-composite-checksum",
            content_length=content_length,
        )

    def head_bucket(self, **kwargs: Any) -> Mapping[str, Any]:
        self.operations.append(("head_bucket", kwargs["Bucket"]))
        return {}

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]:
        key = kwargs["Key"]
        assert kwargs["ChecksumMode"] == "ENABLED"
        self.operations.append(("head", key))
        self._maybe_lag("head", key, "HeadObject")
        stored = self._get(key, "HeadObject")
        return {
            "ContentLength": stored.content_length
            if stored.content_length is not None
            else len(stored.body),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
            "ChecksumSHA256": stored.checksum_sha256,
            "ETag": stored.etag,
        }

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]:
        key = kwargs["Key"]
        assert kwargs["ChecksumMode"] == "ENABLED"
        self.operations.append(("get", key))
        self._maybe_lag("get", key, "GetObject")
        stored = self._get(key, "GetObject")
        return {
            "Body": _ChunkTrackedBody(stored.body, self.read_sizes),
            "ContentLength": stored.content_length
            if stored.content_length is not None
            else len(stored.body),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
            "ChecksumSHA256": stored.checksum_sha256,
            "ETag": stored.etag,
        }

    def list_objects_v2(self, **kwargs: Any) -> Mapping[str, Any]:
        prefix = kwargs["Prefix"]
        self.list_calls += 1
        self.operations.append(("list", prefix))
        with self._lock:
            object_keys = sorted(self.objects)
        keys = [
            key
            for key in object_keys
            if key.startswith(prefix) and self._visible("list", key)
        ]
        start = int(kwargs.get("ContinuationToken", "0"))
        page = keys[start : start + self.page_size]
        end = start + len(page)
        truncated = end < len(keys)
        response: dict[str, Any] = {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": truncated,
        }
        if truncated:
            response["NextContinuationToken"] = str(end)
        return response

    def seed(self, key: str, data: bytes, media_type: str = "application/json") -> None:
        digest = sha256_hex(data)
        self.objects[key] = _StoredObject(
            body=data,
            content_type=media_type,
            metadata={"sha256": digest},
            checksum_sha256=base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
        )

    def set_lag(self, operation: str, key: str, calls: int) -> None:
        self.lag[(operation, key)] = calls
        self.calls[(operation, key)] = 0

    def _visible(self, operation: str, key: str) -> bool:
        marker = (operation, key)
        self.calls[marker] = self.calls.get(marker, 0) + 1
        return self.calls[marker] > self.lag.get(marker, 0)

    def _maybe_lag(self, operation: str, key: str, api: str) -> None:
        if not self._visible(operation, key):
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "lagged"}}, api
            )

    def _get(self, key: str, operation: str) -> _StoredObject:
        try:
            return self.objects[key]
        except KeyError as error:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                operation,
            ) from error


@pytest.fixture(params=["aws", "tigris"])
def store(request: pytest.FixtureRequest) -> Iterator[tuple[S3Store, FakeS3Client]]:
    config = (
        AwsStorageConfig(
            provider="aws",
            bucket="bucket",
            region="us-west-2",
            prefix="tenant/v1",
        )
        if request.param == "aws"
        else TigrisStorageConfig(
            provider="tigris",
            bucket="bucket",
            prefix="tenant/v1",
        )
    )
    client = FakeS3Client()
    client.provider = request.param
    yield S3Store(config, client, sleep=lambda _seconds: None), client


def _plan(section: str = "systems-design") -> ResolvedPlan:
    return ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": section,
            "controller": {"kind": "local"},
            "execution": {"kind": "docker"},
            "storage": None,
            "selection": {},
            "context": (),
            "trials": (),
            "runnable": False,
            "not_runnable_reasons": ("empty",),
        }
    )


def _request(section: str = "systems-design") -> RequestRecord:
    plan = _plan(section)
    manifest = ContextManifest(schema_version=1, files=())
    return RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )


def _admission() -> AdmissionRecord:
    return new_admission(_request(), timestamp="2026-08-28T20:00:00Z")


def _terminal(
    request_sha256: str,
    artifacts: tuple[ArtifactInventoryEntry, ...],
    *,
    outcome: str = "succeeded",
) -> TerminalRecord:
    bindings = tuple(
        ArtifactBinding(logical_path=item.logical_path, sha256=item.content.sha256)
        for item in artifacts
    )
    return TerminalRecord.model_validate(
        {
            "schema_version": 1,
            "run_id": "run-1",
            "request_sha256": request_sha256,
            "winning_attempt_id": "attempt-1",
            "outcome": outcome,
            "harbor_version": "0.22.0",
            "artifacts": artifacts,
            "harbor_config": bindings[0] if bindings else None,
            "harbor_lock": bindings[1] if bindings else None,
            "harbor_result": bindings[2] if bindings else None,
            "evidence": (),
            "warnings": (),
        }
    )


def _publish_artifacts(store: S3Store) -> tuple[ArtifactInventoryEntry, ...]:
    return tuple(
        ArtifactInventoryEntry(
            logical_path=f"job/{name}.json",
            content=store.publish_content(name.encode(), media_type="application/json"),
        )
        for name in ("config", "lock", "result")
    )


def _publish_terminal_fixture(
    store: S3Store,
) -> tuple[str, TerminalRecord, tuple[ArtifactInventoryEntry, ...]]:
    request_sha256 = store.publish_request(_request())
    artifacts = _publish_artifacts(store)
    terminal = _terminal(request_sha256, artifacts)
    digest = store.publish_terminal(terminal)
    return digest, terminal, artifacts


def test_client_construction_uses_provider_specific_botocore_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    sentinel = FakeS3Client()

    def fake_client(service: str, **kwargs: Any) -> FakeS3Client:
        calls.append((service, kwargs))
        return sentinel

    monkeypatch.setattr("tetrabench.s3.boto3.client", fake_client)
    aws = AwsStorageConfig(provider="aws", bucket="aws", region="eu-west-1")
    tigris = TigrisStorageConfig(provider="tigris", bucket="tigris")

    assert create_s3_client(aws) is sentinel
    assert create_s3_client(tigris) is sentinel
    assert calls[0] == ("s3", {"region_name": "eu-west-1"})
    service, options = calls[1]
    assert service == "s3"
    assert options["region_name"] == "auto"
    assert options["endpoint_url"] == "https://t3.storage.dev"
    assert options["config"].s3 == {"addressing_style": "virtual"}
    assert not {"aws_access_key_id", "aws_secret_access_key"} & options.keys()


def test_content_bytes_are_checksummed_verified_and_idempotent(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    descriptor = s3.publish_content(b"payload", media_type="text/plain")
    stored = client.objects[descriptor.key]

    assert stored.body == b"payload"
    assert stored.metadata == {"sha256": descriptor.sha256}
    assert stored.checksum_sha256 == base64.b64encode(
        bytes.fromhex(descriptor.sha256)
    ).decode("ascii")
    assert [operation for operation in client.operations if operation[0] == "put"] == [
        ("put", descriptor.key)
    ]

    assert s3.publish_content(b"payload", media_type="text/plain") == descriptor
    assert [operation for operation in client.operations if operation[0] == "put"] == [
        ("put", descriptor.key)
    ]


def test_existing_object_mismatch_is_a_conflict(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    descriptor = s3.publish_content(b"payload")
    client.objects[descriptor.key].metadata["sha256"] = "0" * 64

    with pytest.raises(S3ConflictError, match="existing immutable object"):
        s3.publish_content(b"payload")
    with pytest.raises(S3IntegrityError, match="metadata"):
        s3.verify_content(descriptor)

    foreign = ContentObject.model_validate(
        descriptor.model_dump() | {"key": f"other/objects/sha256/{descriptor.sha256}"}
    )
    with pytest.raises(S3IntegrityError, match="configured namespace"):
        s3.verify_content(foreign)


def test_content_file_is_streamed_with_the_same_contract(
    store: tuple[S3Store, FakeS3Client],
    tmp_path: Path,
) -> None:
    s3, client = store
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"abcdefgh")

    descriptor = s3.publish_content_file(path, chunk_size=3)

    assert descriptor.size == 8
    assert client.objects[descriptor.key].body == b"abcdefgh"


def test_request_publication_is_idempotent_and_conflicts_by_run(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    digest = s3.publish_request(_request())

    assert s3.publish_request(_request()) == digest
    with pytest.raises(S3ConflictError, match="multiple request digests"):
        s3.publish_request(_request("github-workflow"))
    assert (
        len([operation for operation in client.operations if operation[0] == "put"])
        == 2
    )


def test_admission_create_read_and_stale_etag_conflict(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    prepared = _admission()
    created = s3.create_admission(prepared)

    assert s3.read_admission("run-1") == created
    key = admission_key("run-1", prefix="tenant/v1")
    assert client.operations[0] == ("put", key)
    with pytest.raises(S3CasConflictError):
        s3.create_admission(prepared)

    running = transition_admission(
        prepared,
        "running",
        timestamp="2026-08-28T20:00:01Z",
        owner_function_call_id="fc-1",
    )
    stale = AdmissionRead(record=prepared, etag='"stale"')
    with pytest.raises(S3CasConflictError):
        s3.update_admission(stale, running)
    updated = s3.update_admission(created, running)
    assert updated.record == running
    assert updated.etag != created.etag
    assert s3.read_admission("run-1") == updated
    rewritten = running.model_copy(update={"request_sha256": "f" * 64})
    with pytest.raises(ValueError, match="request_sha256"):
        s3.update_admission(created, rewritten)


def test_admission_create_does_not_misclassify_missing_bucket_as_cas(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    client.put_error = ClientError(
        {
            "Error": {"Code": "NoSuchBucket"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "PutObject",
    )
    with pytest.raises(ClientError, match="NoSuchBucket"):
        s3.create_admission(_admission())


def test_same_etag_concurrent_admission_updates_have_one_winner(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, _client = store
    prepared = _admission()
    created = s3.create_admission(prepared)
    candidates = (
        transition_admission(
            prepared,
            "running",
            timestamp="2026-08-28T20:00:01Z",
            owner_function_call_id="fc-1",
        ),
        transition_admission(prepared, "cancelled", timestamp="2026-08-28T20:00:01Z"),
    )

    def update(candidate: AdmissionRecord) -> str:
        try:
            return s3.update_admission(created, candidate).record.state
        except S3CasConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(update, candidates))
    assert outcomes.count("conflict") == 1
    observed = s3.read_admission("run-1")
    assert observed is not None
    assert observed.record.state in {"running", "cancelled"}


def test_request_postwrite_merges_identity_hidden_by_independent_list_lag(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    visible = _request()
    visible_data = canonical_model_bytes(visible)
    visible_digest = sha256_hex(visible_data)
    client.seed(request_key("run-1", visible_digest, prefix="tenant/v1"), visible_data)
    lagged = _request("github-workflow")
    lagged_data = canonical_model_bytes(lagged)
    lagged_digest = sha256_hex(lagged_data)
    lagged_key = request_key("run-1", lagged_digest, prefix="tenant/v1")
    client.set_lag("list", lagged_key, 3)

    with pytest.raises(S3ConflictError, match="multiple request digests"):
        s3.publish_request(lagged)

    assert lagged_key in client.objects
    assert client.calls[("list", lagged_key)] == 3


def test_request_preflights_context_before_writing_record(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    content = s3.publish_content(b"context")
    context = ResolvedContextFile(
        destination="input.txt",
        mode=420,
        size=content.size,
        sha256=content.sha256,
    )
    plan = _plan().model_copy(update={"context": (context,)})
    manifest = ContextManifest(
        schema_version=1,
        files=(
            ContextManifestFile(
                destination="input.txt",
                mode=420,
                content=content,
            ),
        ),
    )
    record = RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )
    del client.objects[content.key]
    client.operations.clear()

    with pytest.raises(ClientError):
        s3.publish_request(record)
    assert not any(operation[0] == "put" for operation in client.operations)


def test_event_publication_is_idempotent_and_conflicts_by_attempt_sequence(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    event = AttemptEvent(
        schema_version=1,
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=7,
        type="started",
        payload={"value": 1},
    )
    digest = s3.publish_event(event)

    assert s3.publish_event(event) == digest
    with pytest.raises(S3ConflictError, match="multiple event hashes"):
        s3.publish_event(event.model_copy(update={"payload": {"value": 2}}))
    assert (
        len([operation for operation in client.operations if operation[0] == "put"])
        == 2
    )


def test_event_postwrite_merges_identity_hidden_by_independent_list_lag(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    visible = AttemptEvent(
        schema_version=1,
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=7,
        type="started",
        payload={"writer": 1},
    )
    visible_data = canonical_model_bytes(visible)
    visible_digest = sha256_hex(visible_data)
    client.seed(
        event_key("run-1", "attempt-1", 7, visible_digest, prefix="tenant/v1"),
        visible_data,
    )
    lagged = visible.model_copy(update={"payload": {"writer": 2}})
    lagged_data = canonical_model_bytes(lagged)
    lagged_digest = sha256_hex(lagged_data)
    lagged_key = event_key("run-1", "attempt-1", 7, lagged_digest, prefix="tenant/v1")
    client.set_lag("list", lagged_key, 3)

    with pytest.raises(S3ConflictError, match="multiple event hashes"):
        s3.publish_event(lagged)

    assert lagged_key in client.objects
    assert client.calls[("list", lagged_key)] == 3


def test_terminal_preflights_dependencies_and_writes_terminal_last(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    request_sha256 = s3.publish_request(_request())
    artifacts = _publish_artifacts(s3)
    terminal = _terminal(request_sha256, artifacts)
    client.operations.clear()

    digest = s3.publish_terminal(terminal)
    key = terminal_key("run-1", digest, prefix="tenant/v1")

    terminal_put = client.operations.index(("put", key))
    assert client.operations[terminal_put + 1] == ("head", key)
    preflight_gets = {
        operation
        for operation in client.operations[:terminal_put]
        if operation[0] == "get"
    }
    assert ("get", request_key("run-1", request_sha256, prefix="tenant/v1")) in (
        preflight_gets
    )
    assert {("get", item.content.key) for item in artifacts} <= preflight_gets


def test_terminal_is_not_written_when_preflight_fails(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    request_sha256 = s3.publish_request(_request())
    artifacts = _publish_artifacts(s3)
    missing = artifacts[-1].content.key
    del client.objects[missing]
    terminal = _terminal(request_sha256, artifacts)
    client.operations.clear()

    with pytest.raises(ClientError):
        s3.publish_terminal(terminal)
    assert not any(operation[0] == "put" for operation in client.operations)


def test_terminal_preflight_merges_get_visible_request_hidden_by_list_lag(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    referenced = _request()
    referenced_data = canonical_model_bytes(referenced)
    referenced_digest = sha256_hex(referenced_data)
    referenced_key = request_key("run-1", referenced_digest, prefix="tenant/v1")
    client.seed(referenced_key, referenced_data)
    conflicting = _request("github-workflow")
    conflicting_data = canonical_model_bytes(conflicting)
    conflicting_digest = sha256_hex(conflicting_data)
    client.seed(
        request_key("run-1", conflicting_digest, prefix="tenant/v1"),
        conflicting_data,
    )
    client.set_lag("list", referenced_key, 3)
    artifacts = _publish_artifacts(s3)
    terminal = _terminal(referenced_digest, artifacts)
    terminal_digest = sha256_hex(canonical_model_bytes(terminal))

    with pytest.raises(S3ConflictError, match="multiple request digests"):
        s3.publish_terminal(terminal)

    assert client.calls[("list", referenced_key)] == 3
    assert ("get", referenced_key) in client.operations
    assert terminal_key("run-1", terminal_digest, prefix="tenant/v1") not in (
        client.objects
    )


def test_terminal_publication_is_idempotent_and_rejects_visible_conflict(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    digest, terminal, _artifacts = _publish_terminal_fixture(s3)

    assert s3.publish_terminal(terminal) == digest
    conflicting = terminal.model_copy(update={"outcome": "failed"})
    with pytest.raises(S3ConflictError, match="different terminal"):
        s3.publish_terminal(conflicting)
    terminal_puts = [
        operation
        for operation in client.operations
        if operation == ("put", terminal_key("run-1", digest, prefix="tenant/v1"))
    ]
    assert len(terminal_puts) == 1


def test_terminal_postwrite_merges_identity_hidden_by_independent_list_lag(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    request_digest = s3.publish_request(_request())
    artifacts = _publish_artifacts(s3)
    terminal = _terminal(request_digest, artifacts)
    terminal_data = canonical_model_bytes(terminal)
    terminal_digest = sha256_hex(terminal_data)
    terminal_object_key = terminal_key("run-1", terminal_digest, prefix="tenant/v1")
    conflicting = terminal.model_copy(update={"outcome": "failed"})
    conflicting_data = canonical_model_bytes(conflicting)
    conflicting_digest = sha256_hex(conflicting_data)
    conflicting_key = terminal_key("run-1", conflicting_digest, prefix="tenant/v1")
    client.seed(conflicting_key, conflicting_data)
    client.set_lag("list", conflicting_key, 3)
    client.set_lag("list", terminal_object_key, 3)

    with pytest.raises(S3ConflictError, match="multiple valid terminal records"):
        s3.publish_terminal(terminal)

    assert terminal_object_key in client.objects
    assert client.calls[("list", conflicting_key)] == 6
    assert client.calls[("list", terminal_object_key)] == 3


def test_read_state_retries_zero_visibility_and_returns_unknown(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store

    state = s3.read_run_state("run-1", attempts=3, delay_seconds=0)

    assert isinstance(state, UnknownRunState)
    assert client.list_calls == 9


def test_read_state_waits_for_delayed_terminal_visibility(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    digest, terminal, _artifacts = _publish_terminal_fixture(s3)
    key = terminal_key("run-1", digest, prefix="tenant/v1")
    client.list_calls = 0
    client.set_lag("list", key, 2)

    state = s3.read_run_state("run-1", attempts=3, delay_seconds=0)

    assert isinstance(state, TerminalRunState)
    assert state.terminal == terminal
    assert client.list_calls == 9


def test_read_state_validates_many_terminals_as_conflict(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    first_digest, terminal, _artifacts = _publish_terminal_fixture(s3)
    other = terminal.model_copy(update={"outcome": "failed"})
    other_data = canonical_model_bytes(other)
    other_digest = sha256_hex(other_data)
    client.seed(terminal_key("run-1", other_digest, prefix="tenant/v1"), other_data)

    state = s3.read_run_state("run-1", attempts=3, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert state.terminal_sha256s == tuple(sorted((first_digest, other_digest)))


def test_read_state_turns_invalid_visible_terminal_into_conflict(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    data = b"{}"
    digest = sha256_hex(data)
    client.seed(terminal_key("run-1", digest, prefix="tenant/v1"), data)

    state = s3.read_run_state("run-1", attempts=3, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert state.terminal_sha256s == (digest,)
    assert "invalid immutable record" in state.reasons[0]


def test_read_state_rejects_terminal_with_unavailable_dependency(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    digest, _terminal_record, artifacts = _publish_terminal_fixture(s3)
    del client.objects[artifacts[0].content.key]

    state = s3.read_run_state("run-1", attempts=2, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert state.terminal_sha256s == (digest,)
    assert "remained unavailable" in state.reasons[0]


def test_lagged_request_conflict_can_surface_on_a_later_read(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    first = _request()
    s3.publish_request(first)
    second = _request("github-workflow")
    second_data = canonical_model_bytes(second)
    second_digest = sha256_hex(second_data)
    second_key = request_key("run-1", second_digest, prefix="tenant/v1")
    client.seed(second_key, second_data)
    client.set_lag("list", second_key, 1)

    assert isinstance(
        s3.read_run_state("run-1", attempts=1, delay_seconds=0), UnknownRunState
    )
    state = s3.read_run_state("run-1", attempts=1, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert "multiple request digests" in state.reasons[0]


def test_terminal_read_merges_direct_request_with_list_visible_conflict(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    _terminal_digest, terminal, _artifacts = _publish_terminal_fixture(s3)
    referenced_key = request_key("run-1", terminal.request_sha256, prefix="tenant/v1")
    conflicting = _request("github-workflow")
    conflicting_data = canonical_model_bytes(conflicting)
    conflicting_digest = sha256_hex(conflicting_data)
    client.seed(
        request_key("run-1", conflicting_digest, prefix="tenant/v1"),
        conflicting_data,
    )
    client.set_lag("list", referenced_key, 1)

    state = s3.read_run_state("run-1", attempts=1, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert any("multiple request digests" in reason for reason in state.reasons)


@pytest.mark.parametrize("conflict_kind", ["request", "event"])
def test_terminal_is_blocked_by_visible_request_or_event_conflict(
    store: tuple[S3Store, FakeS3Client],
    conflict_kind: str,
) -> None:
    s3, _client = store
    request_digest = s3.publish_request(_request())
    artifacts = _publish_artifacts(s3)
    if conflict_kind == "request":
        with pytest.raises(S3ConflictError):
            s3.publish_request(_request("github-workflow"))
        message = "multiple request digests"
    else:
        first = AttemptEvent(
            schema_version=1,
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=1,
            type="started",
            payload={"writer": 1},
        )
        s3.publish_event(first)
        with pytest.raises(S3ConflictError):
            s3.publish_event(first.model_copy(update={"payload": {"writer": 2}}))
        message = "multiple event hashes"

    with pytest.raises(S3ConflictError, match=message):
        s3.publish_terminal(_terminal(request_digest, artifacts))


@pytest.mark.parametrize("record_kind", ["request", "event"])
def test_malformed_visible_request_or_event_becomes_conflict_state(
    store: tuple[S3Store, FakeS3Client],
    record_kind: str,
) -> None:
    s3, client = store
    data = b"{}"
    digest = sha256_hex(data)
    if record_kind == "request":
        key = request_key("run-1", digest, prefix="tenant/v1")
    else:
        key = event_key("run-1", "attempt-1", 1, digest, prefix="tenant/v1")
    client.seed(key, data)

    state = s3.read_run_state("run-1", attempts=1, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert "invalid immutable record" in state.reasons[0]


def test_corrupt_second_request_still_counts_as_a_digest_conflict(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    s3.publish_request(_request())
    data = b"{}"
    digest = sha256_hex(data)
    client.seed(request_key("run-1", digest, prefix="tenant/v1"), data)

    state = s3.read_run_state("run-1", attempts=1, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert any("invalid immutable record" in reason for reason in state.reasons)
    assert any("multiple request digests" in reason for reason in state.reasons)


def test_corrupt_terminal_dependency_becomes_conflict_state(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    _digest, _terminal_record, artifacts = _publish_terminal_fixture(s3)
    content = client.objects[artifacts[0].content.key]
    content.body = b"corrupt"
    content.metadata["sha256"] = "f" * 64

    state = s3.read_run_state("run-1", attempts=1, delay_seconds=0)

    assert isinstance(state, ConflictRunState)
    assert "invalid immutable record" in state.reasons[0]


def test_existing_content_is_fully_rehashed_with_bounded_reads(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    data = b"x" * (2 * 1024 * 1024 + 17)
    descriptor = s3.publish_content(data)
    client.read_sizes.clear()

    assert s3.publish_content(data) == descriptor
    positive_reads = [size for size in client.read_sizes if size > 0]
    assert len(positive_reads) >= 3
    assert max(positive_reads) <= 1024 * 1024


def test_post_put_head_lag_is_retried_with_injected_backoff(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    data = b"head-lag"
    digest = sha256_hex(data)
    key = f"tenant/v1/objects/sha256/{digest}"
    client.set_lag("head", key, 2)

    descriptor = s3.publish_content(data)

    assert descriptor.key == key
    assert client.calls[("head", key)] == 3


def test_existing_get_lag_is_retried_without_duplicate_put(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    descriptor = s3.publish_content(b"get-lag")
    client.set_lag("get", descriptor.key, 2)
    puts_before = sum(operation[0] == "put" for operation in client.operations)

    assert s3.publish_content(b"get-lag") == descriptor
    assert client.calls[("get", descriptor.key)] == 3
    assert sum(operation[0] == "put" for operation in client.operations) == puts_before


def test_paginated_run_listing_covers_every_record_prefix(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    s3.publish_request(_request())
    for sequence in (1, 2):
        s3.publish_event(
            AttemptEvent(
                schema_version=1,
                run_id="run-1",
                attempt_id="attempt-1",
                sequence=sequence,
                type="progress",
                payload={"sequence": sequence},
            )
        )
    client.page_size = 1
    client.list_calls = 0

    state = s3.read_run_state("run-1", attempts=1, delay_seconds=0)

    assert isinstance(state, UnknownRunState)
    assert client.list_calls == 4


def test_managed_multipart_ignores_composite_checksum_but_verifies_bytes(
    store: tuple[S3Store, FakeS3Client],
    tmp_path: Path,
) -> None:
    _store, client = store
    config = (
        AwsStorageConfig(
            provider="aws",
            bucket="bucket",
            region="us-west-2",
            prefix="tenant/v1",
        )
        if client.provider == "aws"
        else TigrisStorageConfig(
            provider="tigris",
            bucket="bucket",
            prefix="tenant/v1",
        )
    )
    s3 = S3Store(
        config,
        client,
        sleep=lambda _seconds: None,
        multipart_threshold=4,
        multipart_chunk_size=4,
    )
    path = tmp_path / "multipart.bin"
    path.write_bytes(b"multipart")

    descriptor = s3.publish_content_file(path, chunk_size=3)
    s3.verify_content(descriptor)

    assert client.managed_uploads == 1
    assert client.objects[descriptor.key].checksum_sha256 == (
        "multipart-composite-checksum"
    )


def test_over_5_gib_stat_selects_managed_upload_without_allocating(
    store: tuple[S3Store, FakeS3Client],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3, client = store
    path = tmp_path / "huge.bin"
    path.write_bytes(b"placeholder")
    huge_size = 5 * 1024**3 + 1
    fake_stat = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_mode=0o100644,
        st_size=huge_size,
        st_ctime_ns=3,
        st_mtime_ns=4,
    )
    client.skip_managed_body = True
    monkeypatch.setattr("tetrabench.s3.os.fstat", lambda _fd: fake_stat)
    monkeypatch.setattr(
        "tetrabench.s3._hash_stream", lambda _stream, _chunk: ("a" * 64, huge_size)
    )

    descriptor = s3.publish_content_file(path)

    assert descriptor.size == huge_size
    assert client.managed_uploads == 1


def test_concurrent_conflicting_request_writers_leave_both_records_visible(
    store: tuple[S3Store, FakeS3Client],
) -> None:
    s3, client = store
    client.put_barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def publish(record: RequestRecord) -> None:
        try:
            s3.publish_request(record)
        except Exception as error:
            errors.append(error)

    writers = [
        threading.Thread(target=publish, args=(record,))
        for record in (_request(), _request("github-workflow"))
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=5)

    assert all(not writer.is_alive() for writer in writers)
    assert len(errors) == 2
    assert all(isinstance(error, S3ConflictError) for error in errors)
    request_keys = [key for key in client.objects if "/requests/" in key]
    assert len(request_keys) == 2
