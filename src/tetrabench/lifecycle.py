"""Durable admission status and cancellation orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, Protocol

from tetrabench.controller import ControllerCallState, DetachedControllerClient
from tetrabench.models import FrozenRecord, NonEmptyString
from tetrabench.receipts import ReceiptStore
from tetrabench.records import (
    AdmissionRecord,
    ConflictRunState,
    RunId,
    RunReadState,
    TerminalRunState,
    transition_admission,
    utc_now_timestamp,
    validate_run_id,
)
from tetrabench.s3 import AdmissionRead, S3CasConflictError, S3IntegrityError


class LifecycleStore(Protocol):
    def read_run_state(self, run_id: str) -> RunReadState: ...

    def read_admission(self, run_id: str) -> AdmissionRead | None: ...

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead: ...


class RunStatus(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    state: Literal[
        "terminal",
        "conflict",
        "prepared",
        "running",
        "cancelling",
        "cancelled",
        "failed",
        "pending_or_unknown",
    ]
    outcome: Literal["succeeded", "failed", "cancelled"] | None = None
    admission_state: (
        Literal["prepared", "running", "cancelling", "cancelled", "terminal", "failed"]
        | None
    ) = None
    admission_revision: int | None = None
    owner_function_call_id: NonEmptyString | None = None
    receipt_evidence: NonEmptyString | None = None
    controller: ControllerCallState | None = None
    warnings: tuple[NonEmptyString, ...] = ()
    detail: NonEmptyString


class StatusService:
    def __init__(
        self,
        store: LifecycleStore,
        receipts: ReceiptStore,
        controller: DetachedControllerClient,
    ) -> None:
        self._store = store
        self._receipts = receipts
        self._controller = controller

    def status(self, run_id: str) -> RunStatus:
        run_id = validate_run_id(run_id)
        durable = self._store.read_run_state(run_id)
        admission_error: str | None = None
        try:
            admission_read = self._store.read_admission(run_id)
        except (OSError, S3IntegrityError, TypeError, ValueError) as error:
            admission_read = None
            admission_error = f"invalid admission record: {error}"

        warnings: list[str] = []
        receipt_evidence: str | None = None
        try:
            receipt = self._receipts.read(run_id)
        except (OSError, TypeError, ValueError) as error:
            receipt = None
            warnings.append(f"local receipt is unreadable: {error}")
        if receipt is None:
            warnings.append("local receipt is missing")
        else:
            latest = receipt.attempts[-1]
            receipt_evidence = latest.transitions[-1].type

        conflict_reasons: list[str] = []
        if isinstance(durable, ConflictRunState):
            conflict_reasons.extend(durable.reasons)
        if admission_error is not None:
            conflict_reasons.append(admission_error)
        admission = admission_read.record if admission_read is not None else None
        if isinstance(durable, TerminalRunState) and admission is not None:
            if admission.request_sha256 != durable.terminal.request_sha256:
                conflict_reasons.append(
                    "admission and terminal bind different requests"
                )
            if (
                admission.state == "terminal"
                and admission.terminal_sha256 != durable.terminal_sha256
            ):
                conflict_reasons.append("admission and terminal digests disagree")
        if conflict_reasons:
            return RunStatus(
                run_id=run_id,
                state="conflict",
                admission_state=admission.state if admission is not None else None,
                admission_revision=(
                    admission.revision if admission is not None else None
                ),
                owner_function_call_id=(
                    admission.owner_function_call_id if admission is not None else None
                ),
                receipt_evidence=receipt_evidence,
                warnings=tuple(warnings),
                detail="; ".join(conflict_reasons),
            )
        if isinstance(durable, TerminalRunState):
            return RunStatus(
                run_id=run_id,
                state="terminal",
                outcome=durable.terminal.outcome,
                admission_state=admission.state if admission is not None else None,
                admission_revision=(
                    admission.revision if admission is not None else None
                ),
                owner_function_call_id=(
                    admission.owner_function_call_id if admission is not None else None
                ),
                receipt_evidence=receipt_evidence,
                warnings=tuple(warnings),
                detail="immutable S3 terminal record is authoritative",
            )
        if admission is None:
            return RunStatus(
                run_id=run_id,
                state="pending_or_unknown",
                receipt_evidence=receipt_evidence,
                warnings=tuple(warnings),
                detail="no terminal proof or admission record is visible",
            )
        if admission.state == "terminal":
            warnings.append("admission names a terminal that is not yet visible")
            state = "pending_or_unknown"
        else:
            state = admission.state
        call_state = None
        if admission.owner_function_call_id is not None:
            call_state = self._controller.inspect(admission.owner_function_call_id)
        return RunStatus(
            run_id=run_id,
            state=state,
            admission_state=admission.state,
            admission_revision=admission.revision,
            owner_function_call_id=admission.owner_function_call_id,
            receipt_evidence=receipt_evidence,
            controller=call_state,
            warnings=tuple(warnings),
            detail=(
                f"durable admission is {admission.state}; no terminal proof is visible"
            ),
        )


class ChildSweepResult(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    remaining_child_ids: tuple[NonEmptyString, ...]
    evidence: NonEmptyString


class ChildCleanupObserver(Protocol):
    def sweep(self, run_id: str) -> ChildSweepResult: ...


class CancellationResult(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    state: Literal["cancelled", "terminal", "failed"]
    owner_function_call_id: NonEmptyString | None = None
    controller_terminal_observed: bool
    terminal_proof_observed: bool
    cleanup_complete: bool
    sweeps: int


class CancellationConflictError(RuntimeError):
    """Cancellation raced an incompatible durable transition."""


class CancellationUnavailableError(RuntimeError):
    """Running cancellation needs the not-yet-deployed child observer."""


class CancellationService:
    def __init__(
        self,
        store: LifecycleStore,
        controller: DetachedControllerClient,
        children: ChildCleanupObserver | None,
        *,
        controller_checks: int = 5,
        sweep_limit: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        delay_seconds: float = 0.1,
        timestamp: Callable[[], str] = utc_now_timestamp,
    ) -> None:
        if controller_checks <= 0 or sweep_limit < 2:
            raise ValueError("cancellation requires checks and at least two sweeps")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        self._store = store
        self._controller = controller
        self._children = children
        self._controller_checks = controller_checks
        self._sweep_limit = sweep_limit
        self._sleep = sleep
        self._delay_seconds = delay_seconds
        self._timestamp = timestamp

    def cancel(self, run_id: str) -> CancellationResult:
        run_id = validate_run_id(run_id)
        for _attempt in range(3):
            durable = self._store.read_run_state(run_id)
            if isinstance(durable, ConflictRunState):
                raise CancellationConflictError("; ".join(durable.reasons))
            if isinstance(durable, TerminalRunState):
                return CancellationResult(
                    run_id=run_id,
                    state="terminal",
                    controller_terminal_observed=True,
                    terminal_proof_observed=True,
                    cleanup_complete=True,
                    sweeps=0,
                )
            observed = self._store.read_admission(run_id)
            if observed is None:
                raise CancellationConflictError("admission record is missing")
            admission = observed.record
            if admission.state == "prepared":
                try:
                    self._store.update_admission(
                        observed,
                        transition_admission(
                            admission, "cancelled", timestamp=self._timestamp()
                        ),
                    )
                except S3CasConflictError:
                    continue
                return CancellationResult(
                    run_id=run_id,
                    state="cancelled",
                    controller_terminal_observed=False,
                    terminal_proof_observed=False,
                    cleanup_complete=True,
                    sweeps=0,
                )
            if admission.state == "cancelled":
                return CancellationResult(
                    run_id=run_id,
                    state="cancelled",
                    owner_function_call_id=admission.owner_function_call_id,
                    controller_terminal_observed=True,
                    terminal_proof_observed=False,
                    cleanup_complete=True,
                    sweeps=0,
                )
            if admission.state == "terminal":
                return CancellationResult(
                    run_id=run_id,
                    state="terminal",
                    owner_function_call_id=admission.owner_function_call_id,
                    controller_terminal_observed=True,
                    terminal_proof_observed=False,
                    cleanup_complete=True,
                    sweeps=0,
                )
            if admission.state == "failed":
                return CancellationResult(
                    run_id=run_id,
                    state="failed",
                    owner_function_call_id=admission.owner_function_call_id,
                    controller_terminal_observed=True,
                    terminal_proof_observed=False,
                    cleanup_complete=False,
                    sweeps=0,
                )
            if admission.state == "running":
                if self._children is None:
                    raise CancellationUnavailableError(
                        "running cancellation requires the deployed Harbor child "
                        "observer; "
                        "admission was not changed"
                    )
                try:
                    observed = self._store.update_admission(
                        observed,
                        transition_admission(
                            admission, "cancelling", timestamp=self._timestamp()
                        ),
                    )
                except S3CasConflictError:
                    continue
                admission = observed.record
            if self._children is None:
                raise CancellationUnavailableError(
                    "resuming cancellation requires the deployed Harbor child observer"
                )
            return self._finish_running_cancellation(observed)
        raise CancellationConflictError("admission kept changing during cancellation")

    def _finish_running_cancellation(
        self, observed: AdmissionRead
    ) -> CancellationResult:
        admission = observed.record
        assert self._children is not None
        call_id = admission.owner_function_call_id
        assert call_id is not None
        self._controller.cancel(call_id)
        controller_terminal = False
        for check in range(self._controller_checks):
            if self._controller.inspect(call_id).state in {
                "succeeded",
                "failed",
                "expired",
            }:
                controller_terminal = True
                break
            if check + 1 < self._controller_checks:
                self._sleep(self._delay_seconds)

        sweeps = 0
        consecutive_empty = 0
        while sweeps < self._sweep_limit and consecutive_empty < 2:
            result = self._children.sweep(admission.run_id)
            sweeps += 1
            consecutive_empty = (
                consecutive_empty + 1 if not result.remaining_child_ids else 0
            )
            if consecutive_empty < 2 and sweeps < self._sweep_limit:
                self._sleep(self._delay_seconds)
        cleanup_complete = consecutive_empty >= 2

        durable = self._store.read_run_state(admission.run_id)
        if isinstance(durable, ConflictRunState):
            raise CancellationConflictError("; ".join(durable.reasons))
        if isinstance(durable, TerminalRunState):
            return CancellationResult(
                run_id=admission.run_id,
                state="terminal",
                owner_function_call_id=call_id,
                controller_terminal_observed=controller_terminal,
                terminal_proof_observed=True,
                cleanup_complete=cleanup_complete,
                sweeps=sweeps,
            )
        current = self._store.read_admission(admission.run_id)
        if current is None:
            raise CancellationConflictError("admission disappeared during cancellation")
        if current.record.state == "cancelled":
            final = current.record
        elif (
            current.record.state == "cancelling"
            and controller_terminal
            and cleanup_complete
        ):
            try:
                final = self._store.update_admission(
                    current,
                    transition_admission(
                        current.record, "cancelled", timestamp=self._timestamp()
                    ),
                ).record
            except S3CasConflictError as error:
                raise CancellationConflictError(
                    "cancellation finalization lost admission CAS"
                ) from error
        else:
            final = current.record
        state = "cancelled" if final.state == "cancelled" else "failed"
        return CancellationResult(
            run_id=admission.run_id,
            state=state,
            owner_function_call_id=call_id,
            controller_terminal_observed=controller_terminal,
            terminal_proof_observed=False,
            cleanup_complete=cleanup_complete and final.state == "cancelled",
            sweeps=sweeps,
        )


class FakeChildCleanupObserver:
    def __init__(
        self,
        *,
        sweeps: tuple[tuple[str, ...], ...] = ((), ()),
        operations: list[str] | None = None,
    ) -> None:
        self._sweeps = list(sweeps)
        self.operations = operations if operations is not None else []

    def sweep(self, run_id: str) -> ChildSweepResult:
        self.operations.append("sweep")
        remaining = self._sweeps.pop(0) if self._sweeps else ()
        return ChildSweepResult(
            run_id=run_id,
            remaining_child_ids=remaining,
            evidence="children remain" if remaining else "no children visible",
        )
