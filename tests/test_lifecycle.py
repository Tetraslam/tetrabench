from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import pytest

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller import (
    ControllerAdmissionService,
    ControllerCallState,
    ControllerInvocation,
    FakeDetachedController,
)
from tetrabench.lifecycle import (
    CancellationConflictError,
    CancellationService,
    ChildCleanupObserver,
    ChildSweepResult,
    FakeChildCleanupObserver,
    RecoveryConflictError,
    RecoveryRefusedError,
    RecoveryService,
    StatusService,
)
from tetrabench.models import ResolvedPlan
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.receipts import (
    ControllerCallReceipt,
    PhysicalSubmissionAttempt,
    ReceiptStore,
    SubmissionReceipt,
    SubmissionTransition,
    record_spawn_return,
)
from tetrabench.records import (
    AdmissionRecord,
    ContextManifest,
    RequestRecord,
    TerminalEvidence,
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
from tetrabench.storage import request_key
from tetrabench.submission import SubmissionService


def _request(*, region: str = "us-west-2") -> RequestRecord:
    plan = ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "systems-design",
            "controller": {
                "kind": "modal",
                "app_name": "tetrabench",
                "function_name": "controller",
                "secret_name": None,
            },
            "execution": {"kind": "modal"},
            "storage": {
                "provider": "aws",
                "bucket": "bucket",
                "region": region,
            },
            "selection": {},
            "harbor": {},
            "context": (),
            "trials": ({"task_id": "task", "harbor_task": "task.module"},),
            "runnable": True,
            "not_runnable_reasons": (),
        }
    )
    manifest = ContextManifest(schema_version=1, files=())
    return RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )


def _prepared(request: RequestRecord | None = None) -> AdmissionRecord:
    return new_admission(
        request or _request(),
        timestamp="2026-08-28T20:00:00Z",
    )


def _running() -> AdmissionRecord:
    return transition_admission(
        _prepared(),
        "running",
        timestamp="2026-08-28T20:00:01Z",
        owner_function_call_id="fc-owner",
    )


def _invocation() -> ControllerInvocation:
    request = _request()
    request_sha256 = sha256_hex(canonical_model_bytes(request))
    storage = request.plan.storage
    assert storage is not None
    return ControllerInvocation(
        schema_version=1,
        run_id=request.run_id,
        request_sha256=request_sha256,
        plan_sha256=request.plan_sha256,
        request_key=request_key(
            request.run_id,
            request_sha256,
            prefix=storage.prefix,
        ),
        storage=storage,
    )


def _receipt(call_id: str = "fc-local") -> SubmissionReceipt:
    request = _request()
    receipt = SubmissionReceipt(
        schema_version=2,
        run_id="run-1",
        request_sha256=sha256_hex(canonical_model_bytes(request)),
        plan_sha256=request.plan_sha256,
        context_manifest_sha256=request.context_manifest_sha256,
        attempts=(
            PhysicalSubmissionAttempt(
                attempt_id="submit-1",
                transitions=(
                    SubmissionTransition(sequence=0, type="admission-observed"),
                ),
            ),
        ),
    )
    return record_spawn_return(receipt, ControllerCallReceipt(call_id=call_id))


def _terminal() -> TerminalRecord:
    request = _request()
    return TerminalRecord(
        schema_version=1,
        run_id="run-1",
        request_sha256=sha256_hex(canonical_model_bytes(request)),
        winning_attempt_id="attempt-1",
        outcome="failed",
        harbor_version="0.22.0",
        artifacts=(),
        evidence=(TerminalEvidence(type="failure", message="test terminal"),),
        warnings=(),
    )


class _Store:
    def __init__(
        self,
        admission: AdmissionRecord,
        state=None,
        *,
        request: RequestRecord | None = None,
    ) -> None:
        self.admission = AdmissionRead(admission, f"etag-{admission.revision}")
        self.state = state or UnknownRunState(run_id="run-1")
        self.request = request or _request()
        storage = self.request.plan.storage
        assert storage is not None
        self.storage = storage
        self.operations: list[str] = []
        self.coordination_safe = True

    def require_coordination_safe(self):
        if not self.coordination_safe:
            self.operations.append("preflight-rejected")
            raise UnsafeCoordinationTopologyError("unsafe topology")
        return None

    def read_run_state(self, run_id: str):
        self.operations.append("run-state")
        return self.state

    def read_request(
        self, run_id: str, request_sha256: str, request_object_key: str
    ) -> RequestRecord:
        self.operations.append("request")
        return self.request

    def read_admission(self, run_id: str) -> AdmissionRead | None:
        self.operations.append("admission")
        return self.admission

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead:
        self.operations.append(f"cas:{replacement.state}")
        if expected.etag != self.admission.etag:
            raise S3CasConflictError("stale")
        self.admission = AdmissionRead(replacement, f"etag-{replacement.revision}")
        return self.admission

    def create_admission(self, admission: AdmissionRecord) -> AdmissionRead:
        raise AssertionError("recovery must not create admission")

    def publish_content(
        self, data: bytes, *, media_type: str = "application/octet-stream"
    ):
        raise AssertionError("recovery must not republish content")

    def publish_request(self, request: RequestRecord) -> str:
        raise AssertionError("recovery must not republish request")

    def publish_terminal(self, terminal: TerminalRecord) -> str:
        raise AssertionError("recovery must not publish terminal proof")


def test_recovery_rejects_unsafe_topology_before_provider_mutation(
    tmp_path: Path,
) -> None:
    store = _Store(new_admission(_request(), timestamp="2026-08-28T20:00:00Z"))
    store.coordination_safe = False
    controller = FakeDetachedController()
    children = FakeChildCleanupObserver()

    with pytest.raises(UnsafeCoordinationTopologyError, match="unsafe topology"):
        _recovery_service(
            store,
            controller,
            children,
            ReceiptStore(tmp_path),
        ).recover("run-1")

    assert store.operations == ["preflight-rejected"]
    assert controller.operations == []
    assert children.operations == []


def test_cancellation_rejects_unsafe_topology_before_provider_mutation() -> None:
    store = _Store(new_admission(_request(), timestamp="2026-08-28T20:00:00Z"))
    store.coordination_safe = False
    controller = FakeDetachedController()
    children = FakeChildCleanupObserver()

    with pytest.raises(UnsafeCoordinationTopologyError, match="unsafe topology"):
        CancellationService(store, controller, children).cancel("run-1")

    assert store.operations == ["preflight-rejected"]
    assert controller.operations == []
    assert children.operations == []


def _recovery_service(
    store: _Store,
    controller: FakeDetachedController,
    children: ChildCleanupObserver,
    receipts: ReceiptStore,
    **kwargs,
) -> RecoveryService:
    return RecoveryService(
        store,
        controller,
        children,
        SubmissionService(store, controller, receipts),
        sleep=lambda _delay: None,
        owner_settle_seconds=0,
        timestamp=lambda: "2026-08-28T20:00:04Z",
        **kwargs,
    )


def test_status_conflicts_dominate_terminal_and_local_evidence(tmp_path: Path) -> None:
    terminal = _terminal()
    terminal_digest = sha256_hex(canonical_model_bytes(terminal))
    admission = transition_admission(
        _running(),
        "terminal",
        timestamp="2026-08-28T20:00:02Z",
        terminal_sha256="f" * 64,
    )
    store = _Store(
        admission,
        TerminalRunState(
            run_id="run-1",
            terminal_sha256=terminal_digest,
            terminal=terminal,
        ),
    )
    receipts = ReceiptStore(tmp_path)
    receipts.path_for("run-1").write_bytes(b"corrupt")

    status = StatusService(store, receipts, FakeDetachedController()).status("run-1")

    assert status.state == "conflict"
    assert "digests disagree" in status.detail
    assert "unreadable" in status.warnings[0]
    assert store.operations[:2] == ["run-state", "admission"]


def test_terminal_survives_corrupt_receipt_as_warning(tmp_path: Path) -> None:
    terminal = _terminal()
    store = _Store(
        _running(),
        TerminalRunState(
            run_id="run-1",
            terminal_sha256=sha256_hex(canonical_model_bytes(terminal)),
            terminal=terminal,
        ),
    )
    receipts = ReceiptStore(tmp_path)
    receipts.path_for("run-1").write_bytes(b"not-json")

    status = StatusService(store, receipts, FakeDetachedController()).status("run-1")

    assert status.state == "terminal"
    assert status.outcome == "failed"
    assert status.warnings and "unreadable" in status.warnings[0]


def test_status_uses_admission_owner_not_contradictory_local_call(
    tmp_path: Path,
) -> None:
    receipts = ReceiptStore(tmp_path)
    receipts.write(_receipt("fc-local"))
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "running")
    controller.set_state("fc-local", "failed")

    status = StatusService(_Store(_running()), receipts, controller).status("run-1")

    assert status.state == "running"
    assert status.owner_function_call_id == "fc-owner"
    assert status.controller == ControllerCallState(call_id="fc-owner", state="running")
    assert ("inspect", "fc-local") not in controller.operations


def test_status_flags_controller_terminal_without_proof(tmp_path: Path) -> None:
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "succeeded")

    status = StatusService(
        _Store(_running()), ReceiptStore(tmp_path), controller
    ).status("run-1")

    assert status.state == "attention"
    assert status.controller is not None and status.controller.state == "succeeded"
    assert status.warnings[-1] == (
        "controller call is succeeded but no terminal proof is visible"
    )
    assert status.detail == status.warnings[-1]


def test_active_admission_binding_corruption_blocks_status_and_cancellation(
    tmp_path: Path,
) -> None:
    store = _Store(_prepared(), request=_request(region="us-east-1"))

    status = StatusService(
        store, ReceiptStore(tmp_path), FakeDetachedController()
    ).status("run-1")

    assert status.state == "conflict"
    assert "invalid active admission binding" in status.detail
    with pytest.raises(CancellationConflictError, match="active admission binding"):
        CancellationService(
            store,
            FakeDetachedController(),
            _Children([]),
        ).cancel("run-1")
    assert not any(operation.startswith("cas:") for operation in store.operations)


def test_missing_receipt_is_warning_not_failure(tmp_path: Path) -> None:
    status = StatusService(
        _Store(_prepared()), ReceiptStore(tmp_path), FakeDetachedController()
    ).status("run-1")
    assert status.state == "prepared"
    assert status.warnings == ("local receipt is missing",)


class _Children:
    def __init__(self, sweeps: list[tuple[str, ...]]) -> None:
        self.sweeps = sweeps
        self.operations: list[str] = []

    def sweep(self, run_id: str) -> ChildSweepResult:
        self.operations.append("sweep")
        remaining = self.sweeps.pop(0)
        return ChildSweepResult(
            run_id=run_id,
            remaining_child_ids=remaining,
            evidence="empty" if not remaining else "children remain",
        )


@pytest.mark.parametrize("owner_state", ["running", "inspection_failed"])
def test_recovery_never_steals_unknown_or_running_owner(
    tmp_path: Path, owner_state: Literal["running", "inspection_failed"]
) -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", owner_state)
    children = _Children([(), ()])

    with pytest.raises(RecoveryRefusedError):
        _recovery_service(store, controller, children, ReceiptStore(tmp_path)).recover(
            "run-1"
        )

    assert store.admission.record.state == "running"
    assert children.operations == []
    assert controller.spawned == []


@pytest.mark.parametrize("owner_state", ["succeeded", "failed", "expired"])
def test_recovery_quiesces_and_spawns_for_terminal_owner(
    tmp_path: Path, owner_state: Literal["succeeded", "failed", "expired"]
) -> None:
    failed = transition_admission(
        _running(), "failed", timestamp="2026-08-28T20:00:02Z"
    )
    store = _Store(failed)
    controller = FakeDetachedController()
    controller.set_state("fc-owner", owner_state)

    result = _recovery_service(
        store,
        controller,
        _Children([("stale",), (), ()]),
        ReceiptStore(tmp_path),
    ).recover("run-1")

    assert result.state == "spawned"
    assert result.successor_function_call_id == "fc-1"
    assert len(controller.spawned) == 1
    assert store.admission.record.state == "prepared"
    assert store.admission.record.owner_function_call_id is None
    assert tuple(item.state for item in store.admission.record.history) == (
        "prepared",
        "running",
        "failed",
        "recovering",
        "prepared",
    )
    assert tuple(
        item.owner_function_call_id for item in store.admission.record.history
    ) == (
        None,
        "fc-owner",
        "fc-owner",
        "fc-owner",
        None,
    )
    receipt = ReceiptStore(tmp_path).read("run-1")
    assert receipt is not None
    assert tuple(item.type for item in receipt.attempts[-1].transitions) == (
        "recovery-intended",
        "spawn-returned",
    )


def test_abruptly_cancelled_owner_without_intent_is_recoverable(tmp_path: Path) -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "failed")

    result = _recovery_service(
        store, controller, _Children([(), ()]), ReceiptStore(tmp_path)
    ).recover("run-1")

    assert result.state == "spawned"
    assert "cancelling" not in tuple(
        item.state for item in store.admission.record.history
    )


def test_recovery_settles_terminal_owner_before_cleanup_and_spawn(
    tmp_path: Path,
) -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "failed")
    sleeps: list[float] = []
    service = RecoveryService(
        store,
        controller,
        _Children([(), ()]),
        SubmissionService(store, controller, ReceiptStore(tmp_path)),
        sleep=sleeps.append,
        delay_seconds=0,
        owner_settle_seconds=7,
        timestamp=lambda: "2026-08-28T20:00:04Z",
    )

    result = service.recover("run-1")

    assert result.state == "spawned"
    assert sleeps == [7, 0]


def test_cleanup_failure_leaves_recovering_and_resume_spawns(tmp_path: Path) -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "failed")
    receipts = ReceiptStore(tmp_path)

    blocked = _recovery_service(
        store,
        controller,
        _Children([("child",), ("child",)]),
        receipts,
        sweep_limit=2,
    ).recover("run-1")

    assert blocked.state == "recovering"
    assert not blocked.cleanup_complete
    assert store.admission.record.state == "recovering"
    assert controller.spawned == []

    resumed = _recovery_service(
        store, controller, _Children([(), ()]), receipts
    ).recover("run-1")
    assert resumed.state == "spawned"
    assert len(controller.spawned) == 1


class _LockedStore(_Store):
    def __init__(self, admission: AdmissionRecord) -> None:
        super().__init__(admission)
        self._lock = threading.Lock()

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead:
        with self._lock:
            return super().update_admission(expected, replacement)


class _BarrierChildren:
    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self._calls = threading.local()

    def sweep(self, run_id: str) -> ChildSweepResult:
        calls = getattr(self._calls, "count", 0) + 1
        self._calls.count = calls
        if calls == 2:
            self._barrier.wait()
        return ChildSweepResult(
            run_id=run_id,
            remaining_child_ids=(),
            evidence="empty",
        )


def test_concurrent_recoveries_before_prepare_have_one_prepare_cas_winner(
    tmp_path: Path,
) -> None:
    recovering = transition_admission(
        _running(), "recovering", timestamp="2026-08-28T20:00:02Z"
    )
    store = _LockedStore(recovering)
    controller = FakeDetachedController()
    children = _BarrierChildren(threading.Barrier(2))

    def recover(host: str):
        return _recovery_service(
            store, controller, children, ReceiptStore(tmp_path / host)
        ).recover("run-1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(recover, ("a", "b")))

    assert {item.state for item in results} == {"prepared", "spawned"}
    assert len(controller.spawned) == 1


class _ClaimingSpawner:
    def __init__(self, store: _LockedStore) -> None:
        self._store = store
        self._barrier = threading.Barrier(2)
        self._first_spawned = threading.Event()
        self._lock = threading.Lock()
        self.calls: list[str] = []
        self.harbor_runs = 0

    def wait_for_first_spawn(self) -> None:
        assert self._first_spawned.wait(timeout=2)

    def recover_request(self, request: RequestRecord) -> str:
        assert request == _request()
        with self._lock:
            call_id = f"fc-successor-{len(self.calls) + 1}"
            self.calls.append(call_id)
            if len(self.calls) == 1:
                self._first_spawned.set()
        self._barrier.wait(timeout=2)
        decision = ControllerAdmissionService(
            self._store, timestamp=lambda: "2026-08-28T20:00:05Z"
        ).claim(_invocation(), call_id)
        if decision.admitted:
            with self._lock:
                self.harbor_runs += 1
        return call_id


def test_recovery_race_after_prepared_may_spawn_multiple_calls_but_one_owner(
    tmp_path: Path,
) -> None:
    recovering = transition_admission(
        _running(), "recovering", timestamp="2026-08-28T20:00:02Z"
    )
    store = _LockedStore(recovering)
    spawner = _ClaimingSpawner(store)

    def recover(host: str):
        return RecoveryService(
            store,
            FakeDetachedController(),
            _Children([(), ()]),
            spawner,
            sleep=lambda _delay: None,
            timestamp=lambda: "2026-08-28T20:00:04Z",
        ).recover("run-1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(recover, "a")
        spawner.wait_for_first_spawn()
        second = pool.submit(recover, "b")
        results = (first.result(), second.result())

    assert tuple(item.state for item in results) == ("spawned", "spawned")
    assert len(spawner.calls) == 2
    assert spawner.harbor_runs == 1
    assert store.admission.record.state == "running"
    assert store.admission.record.owner_function_call_id in spawner.calls


class _TerminalAtReadStore(_Store):
    def __init__(self, admission: AdmissionRecord, terminal_at: int) -> None:
        super().__init__(admission)
        self._terminal_at = terminal_at
        self._run_reads = 0
        self.terminal = _terminal()
        self.terminal_visible = False

    def read_run_state(self, run_id: str):
        self.operations.append("run-state")
        self._run_reads += 1
        if self._run_reads >= self._terminal_at:
            self.terminal_visible = True
            return TerminalRunState(
                run_id=run_id,
                terminal_sha256=sha256_hex(canonical_model_bytes(self.terminal)),
                terminal=self.terminal,
            )
        return UnknownRunState(run_id=run_id)


class _TerminalBoundaryChildren:
    def __init__(self, store: _TerminalAtReadStore) -> None:
        self._store = store
        self.operations: list[str] = []
        self._terminal_sweeps = 0

    def sweep(self, run_id: str) -> ChildSweepResult:
        self.operations.append("sweep")
        remaining: tuple[str, ...] = ()
        if self._store.terminal_visible:
            self._terminal_sweeps += 1
            if self._terminal_sweeps == 1:
                remaining = ("stale-terminal-child",)
        return ChildSweepResult(
            run_id=run_id,
            remaining_child_ids=remaining,
            evidence="empty" if not remaining else "stale terminal child",
        )


class _TerminalCasRaceStore(_Store):
    def __init__(self, admission: AdmissionRecord, race_state: str) -> None:
        super().__init__(admission)
        self._race_state = race_state
        self.terminal = _terminal()

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead:
        if replacement.state == self._race_state:
            digest = sha256_hex(canonical_model_bytes(self.terminal))
            self.admission = AdmissionRead(
                transition_admission(
                    expected.record,
                    "terminal",
                    timestamp="2026-08-28T20:00:03Z",
                    terminal_sha256=digest,
                ),
                "etag-terminal",
            )
            self.state = TerminalRunState(
                run_id="run-1",
                terminal_sha256=digest,
                terminal=self.terminal,
            )
            raise S3CasConflictError("terminal won recovery CAS")
        return super().update_admission(expected, replacement)


@pytest.mark.parametrize("terminal_at", range(1, 7))
def test_terminal_at_each_recovery_boundary_stops_successor(
    tmp_path: Path, terminal_at: int
) -> None:
    store = _TerminalAtReadStore(_running(), terminal_at)
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "failed")
    children = _TerminalBoundaryChildren(store)

    result = _recovery_service(
        store, controller, children, ReceiptStore(tmp_path)
    ).recover("run-1")

    assert result.state == "terminal"
    assert result.terminal_proof_observed
    assert result.cleanup_complete
    assert result.sweeps == 3
    assert children._terminal_sweeps == 3
    assert controller.spawned == []
    assert "cas:terminal" not in store.operations


@pytest.mark.parametrize("race_state", ["recovering", "prepared"])
def test_terminal_winning_recovery_cas_stops_successor(
    tmp_path: Path, race_state: str
) -> None:
    admission = (
        _running()
        if race_state == "recovering"
        else transition_admission(
            _running(), "recovering", timestamp="2026-08-28T20:00:02Z"
        )
    )
    store = _TerminalCasRaceStore(admission, race_state)
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "failed")

    sweeps: list[tuple[str, ...]] = (
        [("stale-terminal-child",), (), ()]
        if race_state == "recovering"
        else [(), (), ("stale-terminal-child",), (), ()]
    )
    children = _Children(sweeps)
    result = _recovery_service(
        store, controller, children, ReceiptStore(tmp_path)
    ).recover("run-1")

    assert result.state == "terminal"
    assert result.cleanup_complete
    assert result.sweeps == 3
    assert controller.spawned == []


@pytest.mark.parametrize("state", ["cancelling", "cancelled"])
def test_recovery_refuses_cancellation_admission(tmp_path: Path, state: str) -> None:
    running = _running()
    if state == "cancelling":
        admission = transition_admission(
            running, "cancelling", timestamp="2026-08-28T20:00:02Z"
        )
    elif state == "cancelled":
        cancelling = transition_admission(
            running, "cancelling", timestamp="2026-08-28T20:00:02Z"
        )
        admission = transition_admission(
            cancelling, "cancelled", timestamp="2026-08-28T20:00:03Z"
        )
    controller = FakeDetachedController()
    with pytest.raises(RecoveryRefusedError):
        _recovery_service(
            _Store(admission),
            controller,
            _Children([(), ()]),
            ReceiptStore(tmp_path),
        ).recover("run-1")
    assert controller.spawned == []


def test_already_terminal_recovery_only_cleans_stale_children(tmp_path: Path) -> None:
    terminal = _terminal()
    digest = sha256_hex(canonical_model_bytes(terminal))
    admission = transition_admission(
        _running(),
        "terminal",
        timestamp="2026-08-28T20:00:02Z",
        terminal_sha256=digest,
    )
    state = TerminalRunState(
        run_id="run-1",
        terminal_sha256=digest,
        terminal=terminal,
    )
    store = _Store(admission, state)
    controller = FakeDetachedController()

    result = _recovery_service(
        store,
        controller,
        _Children([("stale-terminal-child",), (), ()]),
        ReceiptStore(tmp_path),
    ).recover("run-1")

    assert result.state == "terminal"
    assert result.cleanup_complete
    assert result.sweeps == 3
    assert store.admission.record == admission
    assert not any(operation.startswith("cas:") for operation in store.operations)
    assert controller.spawned == []


@pytest.mark.parametrize(
    "mismatch",
    ["admission-terminal", "terminal-request", "request-plan"],
)
def test_terminal_recovery_binding_conflict_never_sweeps_children(
    tmp_path: Path,
    mismatch: str,
) -> None:
    terminal = _terminal()
    digest = sha256_hex(canonical_model_bytes(terminal))
    admission = transition_admission(
        _running(),
        "terminal",
        timestamp="2026-08-28T20:00:02Z",
        terminal_sha256=digest,
    )
    request = _request()
    if mismatch == "admission-terminal":
        admission = admission.model_copy(update={"terminal_sha256": "f" * 64})
    elif mismatch == "terminal-request":
        terminal = terminal.model_copy(update={"request_sha256": "e" * 64})
        digest = sha256_hex(canonical_model_bytes(terminal))
        admission = admission.model_copy(update={"terminal_sha256": digest})
    else:
        request = _request(region="us-east-1")
    state = TerminalRunState(
        run_id="run-1",
        terminal_sha256=digest,
        terminal=terminal,
    )
    store = _Store(admission, state, request=request)
    children = _Children([(), ()])

    status = StatusService(
        store, ReceiptStore(tmp_path), FakeDetachedController()
    ).status("run-1")
    assert status.state == "conflict"

    with pytest.raises(RecoveryConflictError):
        _recovery_service(
            store,
            FakeDetachedController(),
            children,
            ReceiptStore(tmp_path),
        ).recover("run-1")

    assert children.operations == []


class _CleanupErrorChildren:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def sweep(self, run_id: str) -> ChildSweepResult:
        self.operations.append("sweep")
        raise RuntimeError("cleanup unavailable")


def test_terminal_cleanup_failure_is_retryable_without_spawn(tmp_path: Path) -> None:
    terminal = _terminal()
    state = TerminalRunState(
        run_id="run-1",
        terminal_sha256=sha256_hex(canonical_model_bytes(terminal)),
        terminal=terminal,
    )
    store = _Store(_running(), state)
    controller = FakeDetachedController()

    blocked = _recovery_service(
        store, controller, _CleanupErrorChildren(), ReceiptStore(tmp_path)
    ).recover("run-1")

    assert blocked.state == "terminal"
    assert not blocked.cleanup_complete
    assert blocked.sweeps == 0
    assert "child cleanup failed" in blocked.detail
    assert controller.spawned == []

    resumed = _recovery_service(
        store, controller, _Children([(), ()]), ReceiptStore(tmp_path)
    ).recover("run-1")
    assert resumed.state == "terminal"
    assert resumed.cleanup_complete
    assert controller.spawned == []


def test_prepared_orphan_and_receipt_loss_remain_recoverable(tmp_path: Path) -> None:
    store = _Store(_prepared())
    controller = FakeDetachedController()

    result = _recovery_service(
        store, controller, _Children([]), ReceiptStore(tmp_path / "missing")
    ).recover("run-1")

    assert result.state == "spawned"
    receipt = ReceiptStore(tmp_path / "missing").read("run-1")
    assert receipt is not None
    assert receipt.attempts[0].transitions[-1].type == "spawn-returned"
    assert store.admission.record.state == "prepared"


def test_corrupt_local_receipt_cannot_block_prepared_recovery(tmp_path: Path) -> None:
    receipts = ReceiptStore(tmp_path)
    receipts.path_for("run-1").write_bytes(b"corrupt-local-cache")
    store = _Store(_prepared())
    controller = FakeDetachedController()

    result = _recovery_service(store, controller, _Children([]), receipts).recover(
        "run-1"
    )

    assert result.state == "spawned"
    assert len(controller.spawned) == 1
    assert receipts.path_for("run-1").read_bytes() == b"corrupt-local-cache"


def test_successor_claim_preserves_old_and_new_owner_history(tmp_path: Path) -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "failed")
    result = _recovery_service(
        store, controller, _Children([(), ()]), ReceiptStore(tmp_path)
    ).recover("run-1")
    assert result.successor_function_call_id == "fc-1"
    decision = ControllerAdmissionService(
        store, timestamp=lambda: "2026-08-28T20:00:05Z"
    ).claim(_invocation(), "fc-1")

    assert decision.admitted
    assert store.admission.record.owner_function_call_id == "fc-1"
    assert tuple(
        item.owner_function_call_id for item in store.admission.record.history
    ) == (
        None,
        "fc-owner",
        "fc-owner",
        None,
        "fc-1",
    )


def test_status_reports_recovering_as_attention(tmp_path: Path) -> None:
    recovering = transition_admission(
        _running(), "recovering", timestamp="2026-08-28T20:00:02Z"
    )
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "failed")

    status = StatusService(
        _Store(recovering), ReceiptStore(tmp_path), controller
    ).status("run-1")

    assert status.state == "attention"
    assert status.admission_state == "recovering"
    assert "rerun recover to resume" in status.detail


def test_cancel_prepared_cas_directly_without_controller_or_events() -> None:
    store = _Store(_prepared())
    controller = FakeDetachedController()
    children = _Children([])

    result = CancellationService(
        store,
        controller,
        children,
        timestamp=lambda: "2026-08-28T20:00:01Z",
    ).cancel("run-1")

    assert result.state == "cancelled"
    assert store.admission.record.state == "cancelled"
    assert store.admission.record.owner_function_call_id is None
    assert controller.operations == []
    assert children.operations == []


def test_cancel_running_preserves_owner_polls_then_sweeps_and_finalizes() -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "succeeded")
    children = _Children([("child",), (), ()])

    result = CancellationService(
        store,
        controller,
        children,
        sleep=lambda _delay: None,
        timestamp=lambda: "2026-08-28T20:00:02Z",
    ).cancel("run-1")

    assert store.operations == [
        "run-state",
        "admission",
        "request",
        "cas:cancelling",
        "run-state",
        "admission",
        "request",
        "cas:cancelled",
    ]
    assert controller.operations[:2] == [
        ("cancel", "fc-owner"),
        ("inspect", "fc-owner"),
    ]
    assert result.state == "cancelled"
    assert result.cleanup_complete
    assert store.admission.record.owner_function_call_id == "fc-owner"
    assert children.operations == ["sweep", "sweep", "sweep"]


class _DelayedTerminalController(FakeDetachedController):
    def __init__(self, running_checks: int) -> None:
        super().__init__()
        self._running_checks = running_checks

    def inspect(self, call_id: str) -> ControllerCallState:
        self.operations.append(("inspect", call_id))
        if self._running_checks:
            self._running_checks -= 1
            return ControllerCallState(call_id=call_id, state="running")
        return ControllerCallState(call_id=call_id, state="failed")


def test_cancel_default_poll_window_allows_provider_shutdown_delay() -> None:
    store = _Store(_running())
    controller = _DelayedTerminalController(running_checks=5)

    result = CancellationService(
        store,
        controller,
        _Children([(), ()]),
        sleep=lambda _delay: None,
        timestamp=lambda: "2026-08-28T20:00:02Z",
    ).cancel("run-1")

    assert result.state == "cancelled"
    assert result.controller_terminal_observed
    assert store.admission.record.state == "cancelled"


class _TerminalRaceStore(_Store):
    def __init__(self, admission: AdmissionRecord, terminal: TerminalRecord) -> None:
        super().__init__(admission)
        self._terminal = terminal
        self._reads = 0

    def read_run_state(self, run_id: str):
        self.operations.append("run-state")
        self._reads += 1
        if self._reads >= 2:
            return TerminalRunState(
                run_id=run_id,
                terminal_sha256=sha256_hex(canonical_model_bytes(self._terminal)),
                terminal=self._terminal,
            )
        return UnknownRunState(run_id=run_id)


def test_controller_terminal_object_wins_cancel_finalization_race() -> None:
    terminal = _terminal()
    store = _TerminalRaceStore(_running(), terminal)
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "succeeded")

    result = CancellationService(
        store,
        controller,
        _Children([(), ()]),
        sleep=lambda _delay: None,
        timestamp=lambda: "2026-08-28T20:00:02Z",
    ).cancel("run-1")

    assert result.state == "terminal"
    assert result.terminal_proof_observed
    assert store.admission.record.state == "cancelling"
    assert "cas:cancelled" not in store.operations


class _FinalCasTerminalRaceStore(_Store):
    def __init__(self, admission: AdmissionRecord, terminal: TerminalRecord) -> None:
        super().__init__(admission)
        self._terminal = terminal

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead:
        if replacement.state == "cancelled":
            digest = sha256_hex(canonical_model_bytes(self._terminal))
            self.admission = AdmissionRead(
                transition_admission(
                    expected.record,
                    "terminal",
                    timestamp="2026-08-28T20:00:03Z",
                    terminal_sha256=digest,
                ),
                "etag-terminal",
            )
            self.state = TerminalRunState(
                run_id="run-1",
                terminal_sha256=digest,
                terminal=self._terminal,
            )
            self.operations.append("cas:cancelled-conflict")
            raise S3CasConflictError("terminal won final CAS")
        return super().update_admission(expected, replacement)


def test_terminal_winning_final_cancellation_cas_is_returned() -> None:
    store = _FinalCasTerminalRaceStore(_running(), _terminal())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "succeeded")

    result = CancellationService(
        store,
        controller,
        _Children([(), ()]),
        sleep=lambda _delay: None,
        timestamp=lambda: "2026-08-28T20:00:02Z",
    ).cancel("run-1")

    assert result.state == "terminal"
    assert result.terminal_proof_observed
    assert "cas:cancelled-conflict" in store.operations


def test_cancel_cleanup_failure_leaves_cancelling() -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "succeeded")
    result = CancellationService(
        store,
        controller,
        _Children([("child",), ("child",)]),
        sweep_limit=2,
        sleep=lambda _delay: None,
        timestamp=lambda: "2026-08-28T20:00:02Z",
    ).cancel("run-1")
    assert result.state == "failed"
    assert not result.cleanup_complete
    assert store.admission.record.state == "cancelling"


def test_cancel_does_not_treat_inspection_failure_as_stopped() -> None:
    store = _Store(_running())
    controller = FakeDetachedController()
    controller.set_state("fc-owner", "inspection_failed", detail="AuthError")
    result = CancellationService(
        store,
        controller,
        _Children([(), ()]),
        controller_checks=2,
        sleep=lambda _delay: None,
        timestamp=lambda: "2026-08-28T20:00:02Z",
    ).cancel("run-1")
    assert result.state == "failed"
    assert not result.controller_terminal_observed
    assert store.admission.record.state == "cancelling"
