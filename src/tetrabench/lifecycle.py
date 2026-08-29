"""Durable admission status and cancellation orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, Protocol

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller import ControllerCallState, DetachedControllerClient
from tetrabench.models import FrozenRecord, NonEmptyString, ResolvedStorageConfig
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.receipts import ReceiptStore
from tetrabench.records import (
    AdmissionRecord,
    ConflictRunState,
    RequestRecord,
    RunId,
    RunReadState,
    TerminalRunState,
    transition_admission,
    utc_now_timestamp,
    validate_run_id,
)
from tetrabench.s3 import (
    AdmissionRead,
    CoordinationTopology,
    S3CasConflictError,
    S3IntegrityError,
)
from tetrabench.storage import request_key


class BindingStore(Protocol):
    @property
    def storage(self) -> ResolvedStorageConfig: ...
    def read_request(
        self, run_id: str, request_sha256: str, request_object_key: str
    ) -> RequestRecord: ...


class LifecycleStore(BindingStore, Protocol):
    def require_coordination_safe(self) -> CoordinationTopology: ...

    def read_run_state(self, run_id: str) -> RunReadState: ...

    def read_admission(self, run_id: str) -> AdmissionRead | None: ...

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead: ...


class ActiveAdmissionBindingError(RuntimeError):
    """An active admission is not bound to its immutable request and storage."""


class AuthoritativeBindingError(RuntimeError):
    """A terminal or request does not bind its canonical plan and storage."""


def validate_request_plan_storage_binding(
    store: BindingStore,
    *,
    run_id: str,
    request_sha256: str,
    plan_sha256: str | None = None,
) -> RequestRecord:
    """Validate canonical request, plan, run, and configured storage authority."""
    storage = store.storage
    request = store.read_request(
        run_id,
        request_sha256,
        request_key(run_id, request_sha256, prefix=storage.prefix),
    )
    if sha256_hex(canonical_model_bytes(request)) != request_sha256:
        raise AuthoritativeBindingError("request canonical digest does not match")
    if request.run_id != run_id:
        raise AuthoritativeBindingError("request run ID does not match")
    if plan_digest(request.plan) != request.plan_sha256:
        raise AuthoritativeBindingError("request plan digest does not match")
    if plan_sha256 is not None and request.plan_sha256 != plan_sha256:
        raise AuthoritativeBindingError("referenced plan digest does not match")
    if request.plan.storage != storage:
        raise AuthoritativeBindingError("request plan storage does not match")
    return request


def validate_admission_request_binding(
    store: BindingStore,
    admission: AdmissionRecord,
) -> RequestRecord:
    """Read and validate the immutable authority bound by an admission."""
    try:
        return validate_request_plan_storage_binding(
            store,
            run_id=admission.run_id,
            request_sha256=admission.request_sha256,
            plan_sha256=admission.plan_sha256,
        )
    except AuthoritativeBindingError as error:
        raise ActiveAdmissionBindingError(
            "active admission does not match its immutable request, plan, and storage"
        ) from error


def validate_active_admission(
    store: LifecycleStore,
    admission: AdmissionRecord,
) -> RequestRecord:
    """Read and validate the immutable authority behind an active admission."""
    if admission.state not in {
        "prepared",
        "running",
        "recovering",
        "cancelling",
        "failed",
    }:
        raise ValueError("active admission validation requires an active state")
    return validate_admission_request_binding(store, admission)


def terminal_admission_conflicts(
    store: BindingStore,
    terminal: TerminalRunState,
    admission: AdmissionRecord | None,
) -> tuple[str, ...]:
    """Return binding conflicts for one authoritative terminal observation."""
    reasons: list[str] = []
    try:
        validate_request_plan_storage_binding(
            store,
            run_id=terminal.run_id,
            request_sha256=terminal.terminal.request_sha256,
        )
    except (
        AuthoritativeBindingError,
        OSError,
        S3IntegrityError,
        TypeError,
        ValueError,
    ) as error:
        reasons.append(f"invalid terminal binding: {error}")
    if admission is None:
        return tuple(reasons)
    try:
        validate_admission_request_binding(store, admission)
    except (
        ActiveAdmissionBindingError,
        OSError,
        S3IntegrityError,
        TypeError,
        ValueError,
    ) as error:
        reasons.append(f"invalid terminal admission binding: {error}")
    if admission.request_sha256 != terminal.terminal.request_sha256:
        reasons.append("admission and terminal bind different requests")
    if (
        admission.state == "terminal"
        and admission.terminal_sha256 != terminal.terminal_sha256
    ):
        reasons.append("admission and terminal digests disagree")
    return tuple(reasons)


class RunStatus(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    state: Literal[
        "terminal",
        "conflict",
        "prepared",
        "running",
        "recovering",
        "cancelling",
        "cancelled",
        "failed",
        "attention",
        "pending_or_unknown",
    ]
    outcome: Literal["succeeded", "failed", "cancelled"] | None = None
    admission_state: (
        Literal[
            "prepared",
            "running",
            "recovering",
            "cancelling",
            "cancelled",
            "terminal",
            "failed",
        ]
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
        if isinstance(durable, TerminalRunState):
            conflict_reasons.extend(
                terminal_admission_conflicts(self._store, durable, admission)
            )
        elif admission is not None and admission.state in {
            "prepared",
            "running",
            "recovering",
            "cancelling",
        }:
            try:
                validate_active_admission(self._store, admission)
            except (
                ActiveAdmissionBindingError,
                OSError,
                S3IntegrityError,
                TypeError,
                ValueError,
            ) as error:
                conflict_reasons.append(f"invalid active admission binding: {error}")
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
            detail = "admission terminal acknowledgement is not yet visible as proof"
        elif admission.state == "recovering":
            state = "attention"
            detail = (
                "detached-controller recovery is quiescing stale children; "
                "rerun recover to resume if cleanup stopped"
            )
            warnings.append(detail)
        else:
            state = admission.state
            detail = (
                f"durable admission is {admission.state}; no terminal proof is visible"
            )
        call_state = None
        if admission.owner_function_call_id is not None:
            call_state = self._controller.inspect(admission.owner_function_call_id)
        if (
            admission.state != "recovering"
            and call_state is not None
            and call_state.state
            in {
                "succeeded",
                "failed",
                "expired",
            }
        ):
            detail = (
                f"controller call is {call_state.state} but no terminal proof "
                "is visible"
            )
            warnings.append(detail)
            state = "attention"
        return RunStatus(
            run_id=run_id,
            state=state,
            admission_state=admission.state,
            admission_revision=admission.revision,
            owner_function_call_id=admission.owner_function_call_id,
            receipt_evidence=receipt_evidence,
            controller=call_state,
            warnings=tuple(warnings),
            detail=detail,
        )


class ChildSweepResult(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    remaining_child_ids: tuple[NonEmptyString, ...]
    evidence: NonEmptyString


class ChildCleanupObserver(Protocol):
    def sweep(self, run_id: str) -> ChildSweepResult: ...


class RecoverySpawner(Protocol):
    def recover_request(self, request: RequestRecord) -> str: ...


class RecoveryResult(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    state: Literal["spawned", "prepared", "recovering", "terminal"]
    prior_owner_function_call_id: NonEmptyString | None = None
    successor_function_call_id: NonEmptyString | None = None
    terminal_proof_observed: bool
    cleanup_complete: bool
    sweeps: int
    detail: NonEmptyString


class RecoveryConflictError(RuntimeError):
    """Recovery raced incompatible durable authority."""


class RecoveryRefusedError(RuntimeError):
    """The run is not eligible for detached-controller recovery."""


class RecoveryService:
    """Recover a detached controller only after its prior owner has stopped."""

    def __init__(
        self,
        store: LifecycleStore,
        controller: DetachedControllerClient,
        children: ChildCleanupObserver,
        submission: RecoverySpawner,
        *,
        sweep_limit: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        delay_seconds: float = 0.2,
        owner_settle_seconds: float = 30.0,
        timestamp: Callable[[], str] = utc_now_timestamp,
    ) -> None:
        if sweep_limit < 2:
            raise ValueError("recovery requires at least two child sweeps")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        if owner_settle_seconds < 0:
            raise ValueError("owner settle time must be non-negative")
        self._store = store
        self._controller = controller
        self._children = children
        self._submission = submission
        self._sweep_limit = sweep_limit
        self._sleep = sleep
        self._delay_seconds = delay_seconds
        self._owner_settle_seconds = owner_settle_seconds
        self._timestamp = timestamp

    def recover(self, run_id: str) -> RecoveryResult:
        run_id = validate_run_id(run_id)
        self._store.require_coordination_safe()
        for _attempt in range(5):
            terminal = self._read_terminal(run_id)
            if terminal is not None:
                return self._terminal_result(run_id, terminal)
            observed = self._store.read_admission(run_id)
            if observed is None:
                raise RecoveryConflictError("admission record is missing")
            admission = observed.record
            if admission.state in {"terminal", "cancelling", "cancelled"}:
                raise RecoveryRefusedError(
                    f"cannot recover admission state {admission.state}"
                )
            request = self._read_bound_request(admission)
            if admission.state == "prepared":
                call_id = self._submission.recover_request(request)
                return RecoveryResult(
                    run_id=run_id,
                    state="spawned",
                    successor_function_call_id=call_id,
                    terminal_proof_observed=False,
                    cleanup_complete=True,
                    sweeps=0,
                    detail=(
                        "spawned a controller call for prepared admission; "
                        "admission CAS selects the sole controller owner"
                    ),
                )
            if admission.state in {"running", "failed"}:
                owner = admission.owner_function_call_id
                if owner is None:
                    raise RecoveryConflictError(
                        "owned admission has no controller owner"
                    )
                owner_state = self._controller.inspect(owner)
                if owner_state.state == "running":
                    raise RecoveryRefusedError("controller owner is still running")
                if owner_state.state == "inspection_failed":
                    raise RecoveryRefusedError(
                        "controller owner inspection is unknown; "
                        "recovery made no mutation"
                    )
                self._sleep(self._owner_settle_seconds)
                terminal = self._read_terminal(run_id)
                if terminal is not None:
                    return self._terminal_result(run_id, terminal)
                try:
                    observed = self._store.update_admission(
                        observed,
                        transition_admission(
                            admission, "recovering", timestamp=self._timestamp()
                        ),
                    )
                except S3CasConflictError:
                    continue
                admission = observed.record
            if admission.state != "recovering":
                raise RecoveryRefusedError(
                    f"cannot recover admission state {admission.state}"
                )
            result = self._finish_recovery(observed, request)
            if result is not None:
                return result
        raise RecoveryConflictError("admission kept changing during recovery")

    def _finish_recovery(
        self, observed: AdmissionRead, request: RequestRecord
    ) -> RecoveryResult | None:
        admission = observed.record
        owner = admission.owner_function_call_id
        sweeps = 0
        consecutive_empty = 0
        while sweeps < self._sweep_limit and consecutive_empty < 2:
            terminal = self._read_terminal(admission.run_id)
            if terminal is not None:
                return self._terminal_result(admission.run_id, terminal)
            try:
                result = self._children.sweep(admission.run_id)
            except Exception as error:
                return RecoveryResult(
                    run_id=admission.run_id,
                    state="recovering",
                    prior_owner_function_call_id=owner,
                    terminal_proof_observed=False,
                    cleanup_complete=False,
                    sweeps=sweeps,
                    detail=f"child cleanup failed: {type(error).__name__}",
                )
            sweeps += 1
            consecutive_empty = (
                consecutive_empty + 1 if not result.remaining_child_ids else 0
            )
            if consecutive_empty < 2 and sweeps < self._sweep_limit:
                self._sleep(self._delay_seconds)
        if consecutive_empty < 2:
            return RecoveryResult(
                run_id=admission.run_id,
                state="recovering",
                prior_owner_function_call_id=owner,
                terminal_proof_observed=False,
                cleanup_complete=False,
                sweeps=sweeps,
                detail="stale child cleanup did not reach quiescence",
            )
        terminal = self._read_terminal(admission.run_id)
        if terminal is not None:
            return self._terminal_result(admission.run_id, terminal)
        current = self._store.read_admission(admission.run_id)
        if current is None:
            raise RecoveryConflictError("admission disappeared during recovery")
        if current.etag != observed.etag or current.record.state != "recovering":
            if current.record.state == "terminal":
                terminal = self._read_terminal(admission.run_id)
                if terminal is not None:
                    return self._terminal_result(admission.run_id, terminal)
            if current.record.state == "prepared":
                return RecoveryResult(
                    run_id=admission.run_id,
                    state="prepared",
                    prior_owner_function_call_id=owner,
                    terminal_proof_observed=False,
                    cleanup_complete=True,
                    sweeps=sweeps,
                    detail="another recovery prepared the successor admission",
                )
            return None
        try:
            self._store.update_admission(
                current,
                transition_admission(
                    current.record,
                    "prepared",
                    timestamp=self._timestamp(),
                    clear_owner=True,
                ),
            )
        except S3CasConflictError:
            refreshed = self._store.read_admission(admission.run_id)
            if refreshed is not None and refreshed.record.state == "prepared":
                return RecoveryResult(
                    run_id=admission.run_id,
                    state="prepared",
                    prior_owner_function_call_id=owner,
                    terminal_proof_observed=False,
                    cleanup_complete=True,
                    sweeps=sweeps,
                    detail="another recovery won the prepare CAS",
                )
            return None
        terminal = self._read_terminal(admission.run_id)
        if terminal is not None:
            return self._terminal_result(admission.run_id, terminal)
        call_id = self._submission.recover_request(request)
        return RecoveryResult(
            run_id=admission.run_id,
            state="spawned",
            prior_owner_function_call_id=owner,
            successor_function_call_id=call_id,
            terminal_proof_observed=False,
            cleanup_complete=True,
            sweeps=sweeps,
            detail=(
                "stale children quiesced and a controller call was spawned; "
                "concurrent calls must race for sole admission ownership"
            ),
        )

    def _read_bound_request(self, admission: AdmissionRecord) -> RequestRecord:
        try:
            return validate_active_admission(self._store, admission)
        except (OSError, S3IntegrityError, TypeError, ValueError) as error:
            raise RecoveryConflictError(
                f"invalid recovery admission binding: {error}"
            ) from error

    def _read_terminal(self, run_id: str) -> TerminalRunState | None:
        durable = self._store.read_run_state(run_id)
        if isinstance(durable, ConflictRunState):
            raise RecoveryConflictError("; ".join(durable.reasons))
        return durable if isinstance(durable, TerminalRunState) else None

    def _terminal_result(
        self, run_id: str, terminal: TerminalRunState
    ) -> RecoveryResult:
        current = self._store.read_admission(run_id)
        conflicts = terminal_admission_conflicts(
            self._store,
            terminal,
            current.record if current is not None else None,
        )
        if conflicts:
            raise RecoveryConflictError("; ".join(conflicts))
        owner = current.record.owner_function_call_id if current is not None else None
        sweeps = 0
        consecutive_empty = 0
        failure: str | None = None
        while sweeps < self._sweep_limit and consecutive_empty < 2:
            try:
                result = self._children.sweep(run_id)
            except Exception as error:
                failure = f"child cleanup failed: {type(error).__name__}"
                break
            sweeps += 1
            consecutive_empty = (
                consecutive_empty + 1 if not result.remaining_child_ids else 0
            )
            if consecutive_empty < 2 and sweeps < self._sweep_limit:
                self._sleep(self._delay_seconds)
        cleanup_complete = consecutive_empty >= 2
        if failure is None and not cleanup_complete:
            failure = "stale child cleanup did not reach quiescence"
        return RecoveryResult(
            run_id=run_id,
            state="terminal",
            prior_owner_function_call_id=owner,
            terminal_proof_observed=True,
            cleanup_complete=cleanup_complete,
            sweeps=sweeps,
            detail=(
                "immutable terminal proof observed and child cleanup reached "
                "quiescence; "
                "no controller was spawned"
                if cleanup_complete
                else (
                    f"immutable terminal proof observed; {failure}; "
                    "no controller was spawned"
                )
            ),
        )


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
        controller_checks: int = 50,
        sweep_limit: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        delay_seconds: float = 0.2,
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
        self._store.require_coordination_safe()
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
            if admission.state in {"prepared", "running", "cancelling"}:
                try:
                    validate_active_admission(self._store, admission)
                except (
                    ActiveAdmissionBindingError,
                    OSError,
                    S3IntegrityError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise CancellationConflictError(
                        f"invalid active admission binding: {error}"
                    ) from error
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
            if admission.state == "recovering":
                raise CancellationConflictError(
                    "detached-controller recovery is already in progress"
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
        if self._children is None:
            raise CancellationUnavailableError(
                "running cancellation requires the deployed Harbor child observer"
            )
        call_id = admission.owner_function_call_id
        if call_id is None:
            raise CancellationConflictError(
                "running cancellation admission has no controller owner"
            )
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
        if current.record.state in {"prepared", "running", "cancelling"}:
            try:
                validate_active_admission(self._store, current.record)
            except (
                ActiveAdmissionBindingError,
                OSError,
                S3IntegrityError,
                TypeError,
                ValueError,
            ) as error:
                raise CancellationConflictError(
                    f"invalid active admission binding: {error}"
                ) from error
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
                durable = self._store.read_run_state(admission.run_id)
                if isinstance(durable, ConflictRunState):
                    raise CancellationConflictError(
                        "; ".join(durable.reasons)
                    ) from error
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
                refreshed = self._store.read_admission(admission.run_id)
                if refreshed is not None and refreshed.record.state == "cancelled":
                    return CancellationResult(
                        run_id=admission.run_id,
                        state="cancelled",
                        owner_function_call_id=call_id,
                        controller_terminal_observed=controller_terminal,
                        terminal_proof_observed=False,
                        cleanup_complete=cleanup_complete,
                        sweeps=sweeps,
                    )
                raise
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
