from __future__ import annotations

from pathlib import Path

import pytest

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller import ControllerCallState, FakeDetachedController
from tetrabench.lifecycle import (
    CancellationConflictError,
    CancellationService,
    ChildSweepResult,
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
from tetrabench.s3 import AdmissionRead, S3CasConflictError


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
