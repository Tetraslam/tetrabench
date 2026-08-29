from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller import ControllerInvocation
from tetrabench.controller_runtime import (
    ArtifactCollectionLimits,
    ControllerRuntime,
    HarborRunResult,
    attempt_paths,
    parse_controller_invocation,
)
from tetrabench.lifecycle import ChildSweepResult
from tetrabench.models import ResolvedPlan
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.records import (
    ArtifactInventoryEntry,
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
    UnsafeCoordinationTopologyError,
)
from tetrabench.storage import content_object_key


def _request(*, context: bytes | None = None) -> RequestRecord:
    files = ()
    plan_context = ()
    if context is not None:
        digest = sha256_hex(context)
        descriptor = ContentObject(
            sha256=digest,
            key=content_object_key(digest),
            size=len(context),
            media_type="text/plain",
        )
        files = (
            ContextManifestFile(
                destination="input/data.txt",
                mode=420,
                content=descriptor,
            ),
        )
        plan_context = (
            {
                "destination": "input/data.txt",
                "mode": 420,
                "size": len(context),
                "sha256": digest,
            },
        )
    plan = ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "systems-design",
            "controller": {
                "kind": "modal",
                "app_name": "tetrabench",
                "function_name": "controller",
                "secret_name": "controller-secret",
            },
            "execution": {"kind": "modal"},
            "storage": {
                "provider": "aws",
                "bucket": "bucket",
                "region": "us-west-2",
            },
            "selection": {},
            "harbor": {},
            "context": plan_context,
            "trials": ({"task_id": "task", "harbor_task": "task.module"},),
            "runnable": True,
            "not_runnable_reasons": (),
        }
    )
    manifest = ContextManifest(schema_version=1, files=files)
    return RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )


def _invocation(request: RequestRecord) -> ControllerInvocation:
    digest = sha256_hex(canonical_model_bytes(request))
    assert request.plan.storage is not None
    return ControllerInvocation(
        schema_version=1,
        run_id=request.run_id,
        request_sha256=digest,
        plan_sha256=request.plan_sha256,
        request_key=f"runs/{request.run_id}/requests/{digest}.json",
        storage=request.plan.storage,
    )


class _Volume:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    def commit(self) -> None:
        self.operations.append("volume:commit")

    def reload(self) -> None:
        self.operations.append("volume:reload")


class _DirtyVolume(_Volume):
    def __init__(self, operations: list[str]) -> None:
        super().__init__(operations)
        self.dirty = True

    def commit(self) -> None:
        super().commit()
        self.dirty = False


class _Observer:
    def __init__(
        self,
        operations: list[str],
        sweeps: list[tuple[str, ...]] | None = None,
    ) -> None:
        self.operations = operations
        self.sweeps = sweeps or [(), ()]

    def sweep(self, run_id: str) -> ChildSweepResult:
        self.operations.append("observer:sweep")
        remaining = self.sweeps.pop(0) if self.sweeps else ()
        return ChildSweepResult(
            run_id=run_id,
            remaining_child_ids=remaining,
            evidence="test sweep",
        )


class _Store:
    def __init__(self, request: RequestRecord, operations: list[str]) -> None:
        self.request = request
        self.operations = operations
        self.admission = AdmissionRead(
            new_admission(request, timestamp="2026-08-28T00:00:00Z"),
            "etag-0",
        )
        self.content = {
            item.content.sha256: b"context bytes"
            for item in request.context_manifest.files
        }
        self.events = []
        self.terminals = []
        self.after_run_state = None
        self.run_state = UnknownRunState(run_id=request.run_id)
        self.fail_terminal_cas = False
        self.fail_terminal_publish = False
        self.after_stream_read = None
        self._lock = threading.Lock()
        self.coordination_safe = True

    def require_coordination_safe(self):
        self.operations.append("s3:preflight")
        if not self.coordination_safe:
            raise UnsafeCoordinationTopologyError("unsafe topology")
        return None

    def read_request(self, run_id, request_sha256, request_key, /):
        self.operations.append("s3:request")
        return self.request

    def read_admission(self, run_id):
        return self.admission

    def update_admission(self, expected, replacement):
        with self._lock:
            if replacement.state == "terminal" and self.fail_terminal_cas:
                raise S3CasConflictError("terminal acknowledgement lost")
            if expected.etag != self.admission.etag:
                raise S3CasConflictError("stale")
            self.admission = AdmissionRead(
                replacement,
                f"etag-{replacement.revision}",
            )
            self.operations.append(f"s3:cas:{replacement.state}")
            return self.admission

    def read_run_state(self, run_id):
        self.operations.append("s3:run-state")
        if self.after_run_state is not None:
            self.after_run_state()
        return self.run_state

    def read_content(self, descriptor):
        self.operations.append("s3:content-read")
        return self.content[descriptor.sha256]

    def publish_content_stream(self, stream, *, media_type="application/octet-stream"):
        data = stream.read()
        self.operations.append(f"s3:artifact:{len(data)}")
        if self.after_stream_read is not None:
            self.after_stream_read(data)
        digest = sha256_hex(data)
        return ContentObject(
            sha256=digest,
            key=content_object_key(digest),
            size=len(data),
            media_type=media_type,
        )

    def publish_event(self, event):
        self.operations.append(f"s3:event:{event.type}")
        self.events.append(event)
        return sha256_hex(canonical_model_bytes(event))

    def publish_terminal(self, terminal):
        self.operations.append("s3:terminal")
        self.terminals.append(terminal)
        if self.fail_terminal_publish:
            raise RuntimeError("provider response lost after terminal write")
        return sha256_hex(canonical_model_bytes(terminal))


class _FakeHarborRunner:
    """Test-only runner; this does not represent working Harbor execution."""

    def __init__(self, operations: list[str], *, fail: bool = False) -> None:
        self.operations = operations
        self.fail = fail

    def run(self, request, paths, *, environment_import_path, labels, event_sink_key):
        self.operations.append("runner")
        assert event_sink_key
        assert environment_import_path.endswith(":TetrabenchModalEnvironment")
        assert labels["tetrabench.run_id"] == request.run_id
        job = paths.jobs / "native-job"
        job.mkdir()
        config = job / "config.json"
        lock = job / "lock.json"
        result = job / "result.json"
        trajectory = job / "trajectory.json"
        config.write_text("{}")
        lock.write_text("{}")
        result.write_text('{"reward":1}')
        trajectory.write_text("[]")
        if self.fail:
            raise RuntimeError("test runner failure")
        return HarborRunResult(
            outcome="succeeded",
            job_directory=job,
            config_path=config,
            lock_path=lock,
            result_path=result,
            evidence=("test-only fake runner completed",),
        )


def _runtime(tmp_path: Path, *, context: bytes | None = None, fail: bool = False):
    operations: list[str] = []
    request = _request(context=context)
    store = _Store(request, operations)
    if context is not None:
        store.content[next(iter(store.content))] = context
    runtime = ControllerRuntime(
        store,
        _Volume(operations),
        _FakeHarborRunner(operations, fail=fail),
        _Observer(operations),
        controller_root=tmp_path,
        attempt_id=lambda: "attempt-one",
    )
    return runtime, store, operations, _invocation(request)


def test_claim_precedes_runner_and_success_publishes_terminal_last(
    tmp_path: Path,
) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "terminal"
    assert operations.index("s3:cas:running") < operations.index("runner")
    assert operations[-2:] == ["s3:terminal", "s3:cas:terminal"]
    terminal = store.terminals[0]
    assert terminal.outcome == "succeeded"
    assert terminal.harbor_config is not None
    assert any(
        item.logical_path.endswith("trajectory.json") for item in terminal.artifacts
    )


def test_unsafe_topology_stops_controller_before_any_run_side_effect(
    tmp_path: Path,
) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    store.coordination_safe = False

    with pytest.raises(UnsafeCoordinationTopologyError, match="unsafe topology"):
        runtime.run(invocation, function_call_id="fc-1")

    assert operations == ["s3:preflight"]
    assert not (tmp_path / "runs").exists()
    assert store.events == []
    assert store.terminals == []
    assert store.admission.record.state == "prepared"


def test_duplicate_loser_performs_no_volume_or_runner_work(tmp_path: Path) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    store.admission = AdmissionRead(
        transition_admission(
            store.admission.record,
            "running",
            timestamp="2026-08-28T00:00:01Z",
            owner_function_call_id="fc-winner",
        ),
        "etag-1",
    )
    result = runtime.run(invocation, function_call_id="fc-loser")
    assert result.state == "skipped"
    assert "runner" not in operations
    assert not any(item.startswith("volume:") for item in operations)


def test_cancellation_after_claim_stops_before_attempt_work(tmp_path: Path) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)

    def cancel_after_claim() -> None:
        current = store.admission
        if current.record.state != "running":
            return
        store.admission = AdmissionRead(
            transition_admission(
                current.record,
                "cancelling",
                timestamp="9999-12-31T23:59:59Z",
            ),
            "etag-2",
        )
        store.after_run_state = None

    store.after_run_state = cancel_after_claim
    result = runtime.run(invocation, function_call_id="fc-1")
    assert result.state == "skipped"
    assert "runner" not in operations
    assert operations.count("observer:sweep") == 2


def test_context_is_verified_and_materialized_between_commit_reload(
    tmp_path: Path,
) -> None:
    runtime, _store, operations, invocation = _runtime(
        tmp_path,
        context=b"context bytes",
    )
    result = runtime.run(invocation, function_call_id="fc-1")
    materialized = tmp_path / "runs/run-1/attempts/attempt-one/context/input/data.txt"
    assert result.state == "terminal"
    assert materialized.read_bytes() == b"context bytes"
    read_index = operations.index("s3:content-read")
    assert operations[read_index + 1 : read_index + 3] == [
        "volume:commit",
        "volume:reload",
    ]


def test_tampered_context_marks_failed_without_false_terminal(tmp_path: Path) -> None:
    runtime, store, operations, invocation = _runtime(
        tmp_path,
        context=b"context bytes",
    )
    store.content[next(iter(store.content))] = b"tampered"
    result = runtime.run(invocation, function_call_id="fc-1")
    assert result.state == "failed"
    assert store.admission.record.state == "failed"
    assert store.terminals == []
    assert "s3:event:controller-failed" in operations


def test_runner_failure_publishes_available_evidence_but_no_terminal(
    tmp_path: Path,
) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path, fail=True)
    result = runtime.run(invocation, function_call_id="fc-1")
    assert result.state == "failed"
    assert store.terminals == []
    assert store.admission.record.state == "failed"
    assert any(item.startswith("s3:artifact:") for item in operations)
    assert "s3:event:controller-failed" in operations


def test_not_quiescent_cleanup_code_is_consistent_at_every_publication_site(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    operations: list[str] = []
    request = _request()
    store = _Store(request, operations)

    class CancellingRunner(_FakeHarborRunner):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            store.admission = AdmissionRead(
                transition_admission(
                    store.admission.record,
                    "cancelling",
                    timestamp=store.admission.record.updated_at,
                ),
                "etag-2",
            )
            return result

    runtime = ControllerRuntime(
        store,
        _Volume(operations),
        CancellingRunner(operations),
        _Observer(operations, [("sb-stale",)] * 5),
        controller_root=tmp_path,
        attempt_id=lambda: "attempt-one",
        cleanup_delay_seconds=0,
    )

    result = runtime.run(_invocation(request), function_call_id="fc-1")

    assert result.state == "failed"
    output = capsys.readouterr().out
    assert "code=child-not-quiescent" in output
    failure_path = attempt_paths(tmp_path, "run-1", "attempt-one").failure
    assert json.loads(failure_path.read_text())["error_code"] == ("child-not-quiescent")
    failure_event = next(
        event for event in store.events if event.type == "controller-failed"
    )
    assert failure_event.payload["error_code"] == "child-not-quiescent"


def test_same_owner_automatic_replay_exits_without_harbor(tmp_path: Path) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    store.admission = AdmissionRead(
        transition_admission(
            store.admission.record,
            "running",
            timestamp="2026-08-28T00:00:01Z",
            owner_function_call_id="fc-1",
        ),
        "etag-1",
    )
    old = tmp_path / "runs/run-1/attempts/attempt-old"
    old.mkdir(parents=True)
    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "skipped"
    assert "automatic replay exits before Harbor" in result.detail
    assert old.is_dir()
    assert not (old.parent / "attempt-one").exists()
    assert "observer:sweep" not in operations
    assert "runner" not in operations


def test_fresh_successor_waits_for_stale_child_listing_to_quiesce(
    tmp_path: Path,
) -> None:
    operations: list[str] = []
    sleeps: list[float] = []
    request = _request()
    store = _Store(request, operations)
    old = tmp_path / "runs/run-1/attempts/attempt-old"
    old.mkdir(parents=True)
    runtime = ControllerRuntime(
        store,
        _Volume(operations),
        _FakeHarborRunner(operations),
        _Observer(operations, [("sb-old",), ("sb-old",), (), ()]),
        controller_root=tmp_path,
        attempt_id=lambda: "attempt-successor",
        cleanup_delay_seconds=0.25,
        sleep=sleeps.append,
    )

    result = runtime.run(_invocation(request), function_call_id="fc-successor")

    assert result.state == "terminal"
    assert operations.count("observer:sweep") == 4
    assert sleeps == [0.25, 0.25, 0.25]
    assert old.is_dir()
    assert (old.parent / "attempt-successor").is_dir()


def test_fresh_successor_commits_interrupted_owner_files_before_attempt_setup(
    tmp_path: Path,
) -> None:
    operations: list[str] = []
    request = _request()
    store = _Store(request, operations)
    old = tmp_path / "runs/run-1/attempts/attempt-old"
    old.mkdir(parents=True)
    runtime = ControllerRuntime(
        store,
        _DirtyVolume(operations),
        _FakeHarborRunner(operations),
        _Observer(operations),
        controller_root=tmp_path,
        attempt_id=lambda: "attempt-successor",
        cleanup_delay_seconds=0,
    )

    result = runtime.run(_invocation(request), function_call_id="fc-successor")

    assert result.state == "terminal"
    assert operations.index("volume:commit") < operations.index("observer:sweep")
    assert old.is_dir()


def test_output_symlink_is_rejected_without_following_escape(tmp_path: Path) -> None:
    runtime, store, _operations, invocation = _runtime(tmp_path)
    escaped = tmp_path / "controller-environment"
    escaped.mkdir()
    (escaped / "credentials").write_text("do-not-upload")

    original_run = runtime._runner.run

    def run_with_symlink(*args, **kwargs):
        result = original_run(*args, **kwargs)
        (result.job_directory / "escaped-parent").symlink_to(
            escaped,
            target_is_directory=True,
        )
        return result

    runtime._runner.run = run_with_symlink
    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "failed"
    assert store.terminals == []


def test_artifact_mutation_during_stream_is_rejected(tmp_path: Path) -> None:
    runtime, store, _operations, invocation = _runtime(tmp_path)
    trajectory = (
        tmp_path / "runs/run-1/attempts/attempt-one/jobs/native-job/trajectory.json"
    )

    def mutate(data: bytes) -> None:
        if data == b"[]":
            trajectory.write_text("mutated")

    store.after_stream_read = mutate
    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "failed"
    assert store.terminals == []


@pytest.mark.parametrize(
    "limits",
    [
        ArtifactCollectionLimits(max_files=5),
        ArtifactCollectionLimits(max_file_bytes=1),
        ArtifactCollectionLimits(max_total_bytes=5),
    ],
)
def test_artifact_limits_fail_before_any_artifact_publication(
    tmp_path: Path, limits: ArtifactCollectionLimits
) -> None:
    operations: list[str] = []
    request = _request()
    store = _Store(request, operations)
    runtime = ControllerRuntime(
        store,
        _Volume(operations),
        _FakeHarborRunner(operations),
        _Observer(operations),
        controller_root=tmp_path,
        attempt_id=lambda: "attempt-one",
        artifact_limits=limits,
    )

    result = runtime.run(_invocation(request), function_call_id="fc-1")

    assert result.state == "failed"
    assert not any(item.startswith("s3:artifact:") for item in operations)


def test_atif_discovery_uses_inventory_for_multistep_and_continuation(
    tmp_path: Path,
) -> None:
    runtime, store, _operations, _invocation_value = _runtime(tmp_path)
    paths = attempt_paths(tmp_path, "run-1", "attempt-one")
    root = paths.jobs / "native-job/trial-one/steps/step-one/agent/trajectory.json"
    continuation = root.with_name("trajectory-continued.json")

    def trajectory(reference: str | None) -> bytes:
        value = {
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "fixture", "version": "1"},
            "steps": [{"step_id": 1, "source": "agent", "message": "done"}],
        }
        if reference is not None:
            value["continued_trajectory_ref"] = reference
        return json.dumps(value).encode()

    inventory = []
    for path, data in (
        (root, trajectory(continuation.name)),
        (continuation, trajectory(None)),
    ):
        digest = sha256_hex(data)
        store.content[digest] = data
        inventory.append(
            ArtifactInventoryEntry(
                logical_path=(
                    f"attempts/attempt-one/{path.relative_to(paths.root).as_posix()}"
                ),
                content=ContentObject(
                    sha256=digest,
                    key=content_object_key(digest),
                    size=len(data),
                    media_type="application/json",
                ),
            )
        )

    evidence, warnings = runtime._atif_evidence(paths, tuple(inventory), (root,))

    assert evidence == ("Secure artifact inventory contains 2 ATIF trajectory file(s)",)
    assert warnings == ()


def test_terminal_cas_failure_emits_no_later_immutable_writes(tmp_path: Path) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    store.fail_terminal_cas = True

    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "terminal"
    terminal_index = operations.index("s3:terminal")
    assert not any(
        operation.startswith(("s3:artifact:", "s3:event:"))
        for operation in operations[terminal_index + 1 :]
    )
    assert store.admission.record.state == "running"


def test_uncertain_terminal_publish_emits_no_later_immutable_writes(
    tmp_path: Path,
) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    store.fail_terminal_publish = True

    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "failed"
    assert result.detail == "terminal-publication-uncertain"
    terminal_index = operations.index("s3:terminal")
    assert not any(
        operation.startswith(("s3:artifact:", "s3:event:"))
        for operation in operations[terminal_index + 1 :]
    )


@pytest.mark.parametrize(
    "admission_state", ["prepared", "failed", "cancelling", "cancelled"]
)
def test_startup_reconciles_terminal_before_claim_or_attempt(
    tmp_path: Path,
    admission_state: str,
) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    prepared = store.admission.record
    running = transition_admission(
        prepared,
        "running",
        timestamp="2026-08-28T00:00:01Z",
        owner_function_call_id="fc-old",
    )
    if admission_state == "prepared":
        record = prepared
    elif admission_state == "failed":
        record = transition_admission(
            running, "failed", timestamp="2026-08-28T00:00:02Z"
        )
    else:
        cancelling = transition_admission(
            running, "cancelling", timestamp="2026-08-28T00:00:02Z"
        )
        record = (
            transition_admission(
                cancelling, "cancelled", timestamp="2026-08-28T00:00:03Z"
            )
            if admission_state == "cancelled"
            else cancelling
        )
    store.admission = AdmissionRead(record, f"etag-{record.revision}")
    terminal = TerminalRecord(
        schema_version=1,
        run_id=invocation.run_id,
        request_sha256=invocation.request_sha256,
        winning_attempt_id="attempt-old",
        outcome="failed",
        harbor_version="0.22.0",
        artifacts=(),
        evidence=(),
        warnings=(),
    )
    digest = sha256_hex(canonical_model_bytes(terminal))
    store.run_state = TerminalRunState(
        run_id=invocation.run_id,
        terminal_sha256=digest,
        terminal=terminal,
    )

    result = runtime.run(invocation, function_call_id="fc-new")

    assert result.state == "terminal"
    assert store.admission.record.state == (
        "prepared" if admission_state == "prepared" else "terminal"
    )
    assert "runner" not in operations
    assert "s3:cas:running" not in operations
    assert not (tmp_path / "runs/run-1/attempts/attempt-one").exists()


def test_failure_evidence_never_serializes_exception_message(tmp_path: Path) -> None:
    runtime, store, _operations, invocation = _runtime(tmp_path, fail=True)
    secret = "AWS_SECRET_ACCESS_KEY=very-secret-provider-value"

    def raise_secret(*_args, **_kwargs):
        raise RuntimeError(secret)

    runtime._runner.run = raise_secret
    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "failed"
    failure = tmp_path / "runs/run-1/attempts/attempt-one/failure.json"
    assert secret.encode() not in failure.read_bytes()
    assert all(secret not in str(event.payload) for event in store.events)


def test_provider_failure_evidence_excludes_raw_sdk_material(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, store, _operations, invocation = _runtime(tmp_path, fail=True)
    secret = "TIGRIS_SECRET_ACCESS_KEY=provider-secret-material"

    def raise_provider_error(*_args, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": secret}},
            "PutObject",
        )

    runtime._runner.run = raise_provider_error
    result = runtime.run(invocation, function_call_id="fc-1")

    failure = tmp_path / "runs/run-1/attempts/attempt-one/failure.json"
    durable = failure.read_bytes() + b"".join(
        canonical_model_bytes(event) for event in store.events
    )
    assert result.detail == "ClientError"
    assert secret.encode() not in durable
    assert b"provider-secret-material" not in durable
    assert secret not in capsys.readouterr().out


def test_complete_harbor_boundary_excludes_provider_environment_and_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _store, _operations, invocation = _runtime(tmp_path)
    provider_names = {
        "AWS_ACCOUNT_ID",
        "aws_access_key_id",
        "AwS_AlternateCredential",
        "TIGRIS_SECRET_ACCESS_KEY",
        "tigris_storage_secret_access_key",
        "TiGrIs_AlternateCredential",
        "bOtO_cOnFiG",
        "bOtOcOrE_TcP_KeEpAlIvE",
    }
    secrets = {
        name: f"sensitive-{index}" for index, name in enumerate(sorted(provider_names))
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    introduced_name = "aWs_InjectedDuringHarborRun"
    unrelated_name = "CUSTOM_S3_PROVIDER_VARIABLE"
    monkeypatch.setenv(unrelated_name, "outside-reviewed-namespaces")
    original_run = runtime._runner.run

    def inspect_boundary(
        request, paths, *, environment_import_path, labels, event_sink_key
    ):
        assert provider_names.isdisjoint(__import__("os").environ)
        assert __import__("os").environ[unrelated_name] == (
            "outside-reviewed-namespaces"
        )
        __import__("os").environ[introduced_name] = "must-be-removed"
        payload = repr(
            (
                request.model_dump(mode="json"),
                environment_import_path,
                labels,
                event_sink_key,
            )
        )
        assert all(name not in payload for name in provider_names)
        assert all(value not in payload for value in secrets.values())
        return original_run(
            request,
            paths,
            environment_import_path=environment_import_path,
            labels=labels,
            event_sink_key=event_sink_key,
        )

    runtime._runner.run = inspect_boundary
    result = runtime.run(invocation, function_call_id="fc-1")

    assert result.state == "terminal"
    assert all(
        __import__("os").environ[name] == value for name, value in secrets.items()
    )
    assert introduced_name not in __import__("os").environ
    assert __import__("os").environ[unrelated_name] == "outside-reviewed-namespaces"


def test_invocation_digest_is_checked_before_parsing() -> None:
    request = _request()
    invocation = _invocation(request)
    payload = canonical_model_bytes(invocation)
    assert parse_controller_invocation(payload, sha256_hex(payload)) == invocation
    with pytest.raises(Exception, match="digest mismatch"):
        parse_controller_invocation(payload, "f" * 64)
