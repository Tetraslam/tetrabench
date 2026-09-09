from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from test_remote import _binary_summary, _content, _request, _terminal
from test_s3 import FakeS3Client

from tetrabench.artifact_policy import ArtifactLimits
from tetrabench.artifacts import ArtifactPullService
from tetrabench.canonical_json import MAX_CANONICAL_JSON_BYTES, sha256_hex
from tetrabench.controller import FakeDetachedController
from tetrabench.integrity import ArtifactVerificationService
from tetrabench.lifecycle import (
    CancellationService,
    FakeChildCleanupObserver,
    RecoveryService,
    StatusService,
)
from tetrabench.models import ResolvedContextFile
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.receipts import ReceiptStore
from tetrabench.records import (
    ArtifactInventoryEntry,
    AttemptEvent,
    ConflictRunState,
    ContextManifest,
    ContextManifestFile,
    TerminalRunState,
    new_admission,
)
from tetrabench.remote import RemoteResultService
from tetrabench.rewards import ControllerResultV2
from tetrabench.s3 import ListingLimits, S3ConflictError, S3IntegrityError, S3Store
from tetrabench.storage import admission_key, event_key, request_key, terminal_key
from tetrabench.submission import SubmissionService


def _run(*, artifacts: int = 4, declared_size: int | None = None):
    client = FakeS3Client()
    request = _request("binary")
    payload = b"sealed input bytes"
    content = _content(payload, "application/octet-stream")
    manifest = ContextManifest(
        schema_version=1,
        files=tuple(
            ContextManifestFile(destination=name, mode=420, content=content)
            for name in ("task/input", "context/input")
        ),
    )
    plan = request.plan.model_copy(
        update={
            "context": tuple(
                ResolvedContextFile(
                    destination=item.destination,
                    mode=item.mode,
                    size=content.size,
                    sha256=content.sha256,
                )
                for item in manifest.files
            )
        }
    )
    request = request.model_copy(
        update={
            "plan": plan,
            "plan_sha256": plan_digest(plan),
            "context_manifest": manifest,
            "context_manifest_sha256": sha256_hex(canonical_model_bytes(manifest)),
        }
    )
    digest = sha256_hex(canonical_model_bytes(request))
    summary = canonical_model_bytes(
        ControllerResultV2(
            schema_version=2,
            run_id="run-1",
            attempt_id="attempt-1",
            outcome="succeeded",
            request_sha256=digest,
            plan_sha256=request.plan_sha256,
            harbor_version="0.22.0",
            modal_version="1.5.4",
            tetrabench_version="0.1.0",
            summary=_binary_summary(),
        )
    )
    terminal = _terminal(request=request, controller_result=summary)
    entries = list(terminal.artifacts)
    for item in entries:
        data = (
            summary
            if item.logical_path.endswith("controller-result.json")
            else Path(item.logical_path).stem.encode()
        )
        client.seed(item.content.key, data, item.content.media_type)
    for index in range(artifacts - 4):
        data = f"native payload {index}".encode()
        descriptor = _content(data)
        if declared_size is not None:
            descriptor = descriptor.model_copy(update={"size": declared_size})
        entries.append(
            ArtifactInventoryEntry(
                logical_path=f"native/file-{index}", content=descriptor
            )
        )
        client.seed(descriptor.key, data)
    terminal = terminal.model_copy(update={"artifacts": tuple(entries)})
    client.seed(content.key, payload, content.media_type)
    client.seed(request_key("run-1", digest), canonical_model_bytes(request))
    client.seed(
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )
    client.seed(
        admission_key("run-1"),
        canonical_model_bytes(new_admission(request, timestamp="2026-09-05T00:00:00Z")),
    )
    event = AttemptEvent(
        schema_version=1,
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=0,
        type="started",
        payload={},
    )
    client.seed(
        event_key("run-1", "attempt-1", 0, sha256_hex(canonical_model_bytes(event))),
        canonical_model_bytes(event),
    )
    assert plan.storage is not None
    store = S3Store(plan.storage, client, sleep=lambda _: None)
    return store, client, request, terminal


def _content_reads(client):
    return [
        (operation, key)
        for operation, key in client.operations
        if key.startswith("objects/")
    ]


@pytest.mark.parametrize("shape", [(4, None), (47, 60_000), (1000, 64 * 1024 * 1024)])
@pytest.mark.parametrize(
    "operation", ["read", "request", "events", "status", "result", "runs"]
)
def test_routine_reads_never_fetch_input_or_native_payloads(shape, operation, tmp_path):
    store, client, request, terminal = _run(artifacts=shape[0], declared_size=shape[1])
    summary = next(
        item.content
        for item in terminal.artifacts
        if item.logical_path.endswith("controller-result.json")
    )
    # Remove every other object. Even the availability of inputs is unchecked.
    for key in tuple(client.objects):
        if key.startswith("objects/") and key != summary.key:
            del client.objects[key]
    if operation == "read":
        assert isinstance(store.read_run_state("run-1"), TerminalRunState)
    elif operation == "request":
        digest = terminal.request_sha256
        assert (
            store.read_request("run-1", digest, request_key("run-1", digest)) == request
        )
    elif operation == "events":
        assert len(store.read_attempt_events("run-1")) == 1
    elif operation == "status":
        status = StatusService(
            store, ReceiptStore(tmp_path / "receipts"), FakeDetachedController()
        ).status("run-1")
        assert status.state == "terminal"
    else:
        service = RemoteResultService(store)
        result = (
            service.result("run-1") if operation == "result" else service.runs().runs[0]
        )
        assert result.state == "terminal"
        assert result.reward == "1"
        assert result.payload_integrity == "unchecked"
        assert result.verification_level == "summary"
    assert _content_reads(client) == (
        [("get", summary.key)] if operation in {"result", "runs"} else []
    )
    assert not any(op == "head" for op, _key in client.operations)
    if operation == "read":
        assert Counter(op for op, _key in client.operations) == {"list": 9, "get": 3}
        assert (
            client.operations.count(
                ("get", request_key("run-1", terminal.request_sha256))
            )
            == 1
        )


def test_deep_audit_deduplicates_inputs_and_artifacts_and_never_mutates():
    store, client, request, terminal = _run(artifacts=47)
    # Same bytes can have different logical roles and media types.
    shared = ArtifactInventoryEntry(
        logical_path="copied-input",
        content=request.context_manifest.files[0].content.model_copy(
            update={"media_type": "text/plain"}
        ),
    )
    old_key = terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    del client.objects[old_key]
    terminal = terminal.model_copy(update={"artifacts": (*terminal.artifacts, shared)})
    client.seed(
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )

    report = ArtifactVerificationService(store).verify("run-1")

    assert report.state == "verified"
    assert report.input_references == 2
    assert report.artifact_references == 48
    assert report.objects_total == report.objects_verified == 48
    unique = {item.content.key: item.content for item in terminal.artifacts}
    assert (
        report.bytes_total
        == report.bytes_verified
        == sum(item.size for item in unique.values())
    )
    assert len(_content_reads(client)) == 48
    assert all(count == 1 for count in Counter(_content_reads(client)).values())
    assert {op for op, _key in client.operations} == {"get", "list"}
    assert report.missing == report.corrupt == ()
    assert max(client.read_sizes) <= MAX_CANONICAL_JSON_BYTES + 1


@pytest.mark.parametrize("kind", ["input", "artifact"])
@pytest.mark.parametrize("damage", ["missing", "hash", "length", "metadata"])
def test_audit_reports_payload_damage_while_routine_records_stay_valid(kind, damage):
    store, client, request, terminal = _run()
    descriptor = (
        request.context_manifest.files[0].content
        if kind == "input"
        else terminal.artifacts[0].content
    )
    if damage == "missing":
        del client.objects[descriptor.key]
    elif damage == "hash":
        client.objects[descriptor.key].body = b"x" * descriptor.size
    elif damage == "length":
        client.objects[descriptor.key].body += b"x"
    else:
        client.objects[descriptor.key].metadata["sha256"] = "0" * 64
    assert isinstance(store.read_run_state("run-1"), TerminalRunState)
    assert _content_reads(client) == []
    assert RemoteResultService(store).result("run-1").state == "terminal"

    report = ArtifactVerificationService(store).verify("run-1")

    assert report.state == "failed"
    assert (report.missing if damage == "missing" else report.corrupt) == (descriptor,)
    assert report.objects_verified == report.objects_total - 1
    assert report.bytes_verified == report.bytes_total - descriptor.size


@pytest.mark.parametrize("kind", ["request", "event", "terminal"])
@pytest.mark.parametrize("damage", ["hash", "schema"])
def test_control_corruption_refuses_audit_before_any_payload_get(kind, damage):
    store, client, _request_record, _terminal_record = _run()
    key = next(key for key in client.objects if f"/{kind}s/" in key)
    if damage == "hash":
        client.objects[key].body = b"x" * len(client.objects[key].body)
    else:
        # A correctly hashed record still has to pass schema and binding checks.
        del client.objects[key]
        data = b"{}"
        key = (
            key.rsplit("/", 1)[0]
            + "/"
            + ("0000000000000000-" if kind == "event" else "")
            + sha256_hex(data)
            + ".json"
        )
        client.seed(key, data)
    assert isinstance(store.read_run_state("run-1"), ConflictRunState)
    report = ArtifactVerificationService(store).verify("run-1")
    assert report.state == "refused"
    assert report.objects_verified == 0
    assert _content_reads(client) == []


def test_audit_refuses_conflict_appearing_during_streaming(monkeypatch):
    store, client, _request_record, terminal = _run()
    original = store.verify_content

    def verify(descriptor):
        original(descriptor)
        other = terminal.model_copy(update={"warnings": ("competing writer",)})
        client.seed(
            terminal_key("run-1", sha256_hex(canonical_model_bytes(other))),
            canonical_model_bytes(other),
        )

    monkeypatch.setattr(store, "verify_content", verify)
    report = ArtifactVerificationService(store).verify("run-1")
    assert report.state == "refused"
    assert report.objects_verified == report.objects_total
    assert report.reasons == ("run has conflicting immutable records",)


@pytest.mark.parametrize("damage", ["input", "artifact"])
def test_terminal_publication_still_verifies_every_dependency_before_writing(damage):
    store, client, request, terminal = _run()
    key = terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    del client.objects[key]
    descriptor = (
        request.context_manifest.files[0].content
        if damage == "input"
        else terminal.artifacts[0].content
    )
    client.objects[descriptor.key].body = b"x" * descriptor.size
    with pytest.raises(S3IntegrityError):
        store.publish_terminal(terminal)
    assert key not in client.objects
    assert not any(op == "put" for op, _key in client.operations)


def test_pull_verifies_during_streaming_without_a_redundant_pre_scan(tmp_path):
    store, client, _request_record, terminal = _run()
    descriptor = terminal.artifacts[0].content
    client.objects[descriptor.key].body = b"x" * descriptor.size
    with pytest.raises(S3IntegrityError):
        ArtifactPullService(store).pull("run-1", tmp_path / "download")
    assert all(op == "get" for op, _key in _content_reads(client))
    assert client.operations.count(("get", descriptor.key)) == 1


def test_audit_inventory_limit_refuses_before_content_get():
    store, client, _request_record, _terminal_record = _run(artifacts=47)
    report = ArtifactVerificationService(
        store, limits=ArtifactLimits(max_files=10)
    ).verify("run-1")
    assert report.state == "refused"
    assert _content_reads(client) == []


@pytest.mark.parametrize("failure", ["denied", "bucket", "connection"])
def test_audit_preserves_provider_error_distinctions(monkeypatch, failure):
    store, _client, _request_record, _terminal_record = _run()
    error = (
        EndpointConnectionError(endpoint_url="https://secret.invalid")
        if failure == "connection"
        else ClientError(
            {
                "Error": {
                    "Code": "AccessDenied" if failure == "denied" else "NoSuchBucket",
                    "Message": "secret",
                }
            },
            "GetObject",
        )
    )

    def verify(_descriptor):
        raise error

    monkeypatch.setattr(store, "verify_content", verify)
    with pytest.raises(type(error)) as caught:
        ArtifactVerificationService(store).verify("run-1")
    assert caught.value is error


def test_missing_summary_is_unavailable_without_losing_terminal_authority():
    store, client, _request_record, terminal = _run()
    descriptor = terminal.artifacts[-1].content
    del client.objects[descriptor.key]
    report = RemoteResultService(store).result("run-1")
    assert report.state == "terminal"
    assert report.summary_status == "unavailable"
    assert report.reward is None
    assert report.verification_level == "records"
    assert report.payload_integrity == "unchecked"
    assert {key for _op, key in _content_reads(client)} == {descriptor.key}


def test_oversized_summary_is_never_downloaded():
    store, client, _request_record, terminal = _run()
    key = terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    del client.objects[key]
    item = terminal.artifacts[-1]
    item = item.model_copy(
        update={
            "content": item.content.model_copy(
                update={"size": MAX_CANONICAL_JSON_BYTES + 1}
            )
        }
    )
    terminal = terminal.model_copy(
        update={"artifacts": (*terminal.artifacts[:-1], item)}
    )
    client.seed(
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )
    report = RemoteResultService(store).result("run-1")
    assert report.state == "terminal"
    assert report.summary_status == "unavailable"
    assert report.verification_level == "records"
    assert _content_reads(client) == []


def test_prepared_cancellation_checks_identity_without_input_payloads():
    store, client, request, terminal = _run()
    del client.objects[
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    ]
    del client.objects[request.context_manifest.files[0].content.key]
    service = CancellationService(
        store, FakeDetachedController(), FakeChildCleanupObserver()
    )
    result = service.cancel("run-1")
    assert result.state == "cancelled"
    assert _content_reads(client) == []


def test_terminal_recovery_cleanup_does_not_scan_payloads(tmp_path):
    store, client, request, _terminal_record = _run()
    del client.objects[request.context_manifest.files[0].content.key]
    controller = FakeDetachedController()
    service = RecoveryService(
        store,
        controller,
        FakeChildCleanupObserver(),
        SubmissionService(store, controller, ReceiptStore(tmp_path / "receipts")),
        sleep=lambda _: None,
    )
    report = service.recover("run-1")
    assert report.state == "terminal"
    assert report.cleanup_complete
    assert report.successor_function_call_id is None
    assert _content_reads(client) == []


def test_audit_refuses_inconsistent_alias_sizes_before_reading_content():
    store, client, _request_record, terminal = _run()
    key = terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    del client.objects[key]
    alias = terminal.artifacts[0].model_copy(
        update={
            "logical_path": "alias",
            "content": terminal.artifacts[0].content.model_copy(update={"size": 99}),
        }
    )
    terminal = terminal.model_copy(update={"artifacts": (*terminal.artifacts, alias)})
    client.seed(
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )
    report = ArtifactVerificationService(store).verify("run-1")
    assert report.state == "refused"
    assert report.reasons == ("run has conflicting immutable records",)
    assert _content_reads(client) == []


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_audit_accepts_complete_unsuccessful_terminal_inventories(outcome):
    store, client, _request_record, terminal = _run()
    del client.objects[
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    ]
    terminal = terminal.model_copy(update={"outcome": outcome})
    client.seed(
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )
    assert ArtifactVerificationService(store).verify("run-1").state == "verified"


def test_audit_refuses_nonterminal_runs_without_claiming_empty_success():
    store, client, _request_record, terminal = _run()
    del client.objects[
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    ]
    report = ArtifactVerificationService(store).verify("run-1")
    assert report.state == "refused"
    assert report.reasons == ("run has no authoritative terminal inventory",)
    assert _content_reads(client) == []


def test_request_disappearing_during_result_read_is_not_summary_unavailability(
    monkeypatch,
):
    store, _client, _request_record, _terminal_record = _run()
    original = store.read_request
    calls = 0

    def read_request(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return original(*args)

    monkeypatch.setattr(store, "read_request", read_request)
    with pytest.raises(ClientError):
        RemoteResultService(store).result("run-1")


@pytest.mark.parametrize(
    "alias_kind", ["input_artifact", "artifact_artifact", "input_input"]
)
def test_descriptor_size_contradictions_are_control_conflicts_without_content_reads(
    alias_kind,
):
    store, client, request, terminal = _run()
    del client.objects[
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    ]
    if alias_kind == "input_input":
        del client.objects[request_key("run-1", terminal.request_sha256)]
        files = list(request.context_manifest.files)
        files[1] = files[1].model_copy(
            update={
                "content": files[1].content.model_copy(
                    update={"size": files[1].content.size + 1}
                )
            }
        )
        manifest = request.context_manifest.model_copy(update={"files": tuple(files)})
        context = list(request.plan.context)
        context[1] = context[1].model_copy(update={"size": context[1].size + 1})
        plan = request.plan.model_copy(update={"context": tuple(context)})
        request = request.model_copy(
            update={
                "context_manifest": manifest,
                "context_manifest_sha256": sha256_hex(canonical_model_bytes(manifest)),
                "plan": plan,
                "plan_sha256": plan_digest(plan),
            }
        )
        digest = sha256_hex(canonical_model_bytes(request))
        client.seed(request_key("run-1", digest), canonical_model_bytes(request))
        terminal = terminal.model_copy(update={"request_sha256": digest})
        client.seed(
            admission_key("run-1"),
            canonical_model_bytes(
                new_admission(request, timestamp="2026-09-05T00:00:00Z")
            ),
        )
    else:
        descriptor = (
            request.context_manifest.files[0].content
            if alias_kind == "input_artifact"
            else terminal.artifacts[0].content
        )
        alias = ArtifactInventoryEntry(
            logical_path="alias",
            content=descriptor.model_copy(update={"size": descriptor.size + 1}),
        )
        terminal = terminal.model_copy(
            update={"artifacts": (*terminal.artifacts, alias)}
        )
    client.seed(
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )
    state = store.read_run_state("run-1")
    assert isinstance(state, ConflictRunState)
    assert any("content descriptors disagree" in reason for reason in state.reasons)
    assert RemoteResultService(store).result("run-1").state == "conflict"
    assert ArtifactVerificationService(store).verify("run-1").state == "refused"
    assert _content_reads(client) == []


def test_media_type_aliases_remain_valid_control_records():
    store, client, request, terminal = _run()
    del client.objects[
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal)))
    ]
    alias = ArtifactInventoryEntry(
        logical_path="alias",
        content=request.context_manifest.files[0].content.model_copy(
            update={"media_type": "text/plain"}
        ),
    )
    terminal = terminal.model_copy(update={"artifacts": (*terminal.artifacts, alias)})
    client.seed(
        terminal_key("run-1", sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )
    assert isinstance(store.read_run_state("run-1"), TerminalRunState)
    assert _content_reads(client) == []
    report = RemoteResultService(store).result("run-1")
    assert report.state == "terminal"
    assert report.payload_integrity == "unchecked"
    assert len(_content_reads(client)) == 1


@pytest.mark.parametrize("operation", ["audit", "result", "publish"])
def test_listing_exhaustion_refuses_before_any_content_scan(monkeypatch, operation):
    store, client, _request_record, terminal = _run()
    store = S3Store(
        store.storage,
        client,
        run_listing_limits=ListingLimits(max_pages=2),
        sleep=lambda _: None,
    )
    calls = 0

    def endless(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "Contents": (),
            "IsTruncated": True,
            "NextContinuationToken": str(calls),
        }

    monkeypatch.setattr(client, "list_objects_v2", endless)
    if operation == "audit":
        report = ArtifactVerificationService(store).verify("run-1")
        assert report.state == "refused"
        assert report.reasons == (
            "run record listing is incomplete: listing budget exceeded",
        )
    elif operation == "result":
        assert RemoteResultService(store).result("run-1").state == "conflict"
    else:
        with pytest.raises(S3ConflictError, match="listing incomplete"):
            store.publish_terminal(terminal)
    assert calls == 2
    assert _content_reads(client) == []
