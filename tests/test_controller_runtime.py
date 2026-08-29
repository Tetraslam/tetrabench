from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller import ControllerInvocation
from tetrabench.controller_runtime import (
    ControllerRuntime,
    HarborRunResult,
    parse_controller_invocation,
)
from tetrabench.lifecycle import ChildSweepResult
from tetrabench.models import ResolvedPlan
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.records import (
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
from tetrabench.s3 import AdmissionRead, S3CasConflictError
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

    def run(self, request, paths, *, environment_import_path, labels):
        self.operations.append("runner")
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


def test_replay_cleans_prior_children_before_unique_attempt(tmp_path: Path) -> None:
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
    assert result.attempt_id == "attempt-one"
    assert old.is_dir()
    assert (old.parent / "attempt-one").is_dir()
    assert operations.index("observer:sweep") < operations.index("runner")
    assert "s3:event:replay-reconciled" in operations


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


@pytest.mark.parametrize("admission_state", ["failed", "cancelling", "cancelled"])
def test_startup_reconciles_terminal_before_claim_or_attempt(
    tmp_path: Path,
    admission_state: str,
) -> None:
    runtime, store, operations, invocation = _runtime(tmp_path)
    running = transition_admission(
        store.admission.record,
        "running",
        timestamp="2026-08-28T00:00:01Z",
        owner_function_call_id="fc-old",
    )
    if admission_state == "failed":
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
    assert store.admission.record.state == "terminal"
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


def test_invocation_digest_is_checked_before_parsing() -> None:
    request = _request()
    invocation = _invocation(request)
    payload = canonical_model_bytes(invocation)
    assert parse_controller_invocation(payload, sha256_hex(payload)) == invocation
    with pytest.raises(Exception, match="digest mismatch"):
        parse_controller_invocation(payload, "f" * 64)
