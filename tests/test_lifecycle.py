from __future__ import annotations

from pathlib import Path

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller import ControllerCallState, FakeDetachedController
from tetrabench.lifecycle import (
    CancellationService,
    ChildSweepResult,
    StatusService,
)
from tetrabench.plan import canonical_model_bytes
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
    AdmissionRevision,
    TerminalEvidence,
    TerminalRecord,
    TerminalRunState,
    UnknownRunState,
    transition_admission,
)
from tetrabench.s3 import AdmissionRead, S3CasConflictError


def _prepared() -> AdmissionRecord:
    first = AdmissionRevision(
        revision=0,
        state="prepared",
        timestamp="2026-08-28T20:00:00Z",
    )
    return AdmissionRecord(
        schema_version=1,
        revision=0,
        run_id="run-1",
        request_sha256="1" * 64,
        plan_sha256="2" * 64,
        state="prepared",
        created_at=first.timestamp,
        updated_at=first.timestamp,
        history=(first,),
    )


def _running() -> AdmissionRecord:
    return transition_admission(
        _prepared(),
        "running",
        timestamp="2026-08-28T20:00:01Z",
        owner_function_call_id="fc-owner",
    )


def _receipt(call_id: str = "fc-local") -> SubmissionReceipt:
    receipt = SubmissionReceipt(
        schema_version=2,
        run_id="run-1",
        request_sha256="1" * 64,
        plan_sha256="2" * 64,
        context_manifest_sha256="3" * 64,
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
    return TerminalRecord(
        schema_version=1,
        run_id="run-1",
        request_sha256="1" * 64,
        winning_attempt_id="attempt-1",
        outcome="failed",
        harbor_version="0.22.0",
        artifacts=(),
        evidence=(TerminalEvidence(type="failure", message="test terminal"),),
        warnings=(),
    )


class _Store:
    def __init__(self, admission: AdmissionRecord, state=None) -> None:
        self.admission = AdmissionRead(admission, f"etag-{admission.revision}")
        self.state = state or UnknownRunState(run_id="run-1")
        self.operations: list[str] = []

    def read_run_state(self, run_id: str):
        self.operations.append("run-state")
        return self.state

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
        "cas:cancelling",
        "run-state",
        "admission",
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
