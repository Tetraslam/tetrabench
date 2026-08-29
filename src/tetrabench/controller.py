"""Detached controller contracts and the deployed Modal adapter."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Literal, Protocol

import modal
from modal.exception import OutputExpiredError, RemoteError, TimeoutError
from pydantic import model_validator

from tetrabench.canonical_json import sha256_hex
from tetrabench.models import (
    FrozenRecord,
    NonEmptyString,
    ResolvedStorageConfig,
    Sha256,
)
from tetrabench.plan import canonical_model_bytes
from tetrabench.records import (
    AdmissionRecord,
    RequestRecord,
    RunId,
    TerminalRecord,
    transition_admission,
    utc_now_timestamp,
)
from tetrabench.s3 import AdmissionRead, S3CasConflictError
from tetrabench.storage import request_key


class ControllerInvocation(FrozenRecord):
    schema_version: Literal[1]
    run_id: RunId
    request_sha256: Sha256
    plan_sha256: Sha256
    request_key: NonEmptyString
    storage: ResolvedStorageConfig

    @model_validator(mode="after")
    def validate_request_key(self) -> ControllerInvocation:
        expected = request_key(
            self.run_id,
            self.request_sha256,
            prefix=self.storage.prefix,
        )
        if self.request_key != expected:
            raise ValueError("controller request key does not match its identity")
        return self


class ControllerCallState(FrozenRecord):
    schema_version: Literal[1] = 1
    call_id: NonEmptyString
    state: Literal["running", "succeeded", "failed", "expired", "inspection_failed"]
    detail: NonEmptyString | None = None


class DetachedControllerClient(Protocol):
    def spawn(self, invocation: ControllerInvocation) -> str: ...

    def inspect(self, call_id: str) -> ControllerCallState: ...

    def cancel(self, call_id: str) -> None: ...


class ControllerAdmissionStore(Protocol):
    def read_request(
        self, run_id: str, request_sha256: str, request_key: str
    ) -> RequestRecord: ...

    def read_admission(self, run_id: str) -> AdmissionRead | None: ...

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead: ...

    def publish_terminal(self, terminal: TerminalRecord) -> str: ...


class ControllerStartDecision(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    function_call_id: NonEmptyString
    admitted: bool
    state: Literal[
        "prepared",
        "running",
        "cancelling",
        "cancelled",
        "terminal",
        "failed",
        "missing",
    ]
    detail: NonEmptyString


class ControllerIdentityError(RuntimeError):
    """A controller invocation is not bound to current durable authority."""


class ControllerAdmissionService:
    """CAS coordination used by a deployed controller before Harbor starts.

    This service deliberately contains no deployed Function or Harbor execution.
    """

    def __init__(
        self,
        store: ControllerAdmissionStore,
        *,
        timestamp: Callable[[], str] = utc_now_timestamp,
    ) -> None:
        self._store = store
        self._timestamp = timestamp

    def claim(
        self,
        invocation: ControllerInvocation,
        function_call_id: str,
    ) -> ControllerStartDecision:
        invocation, request = self._validate_invocation(invocation)
        observed = self._store.read_admission(invocation.run_id)
        if observed is None:
            return ControllerStartDecision(
                run_id=invocation.run_id,
                function_call_id=function_call_id,
                admitted=False,
                state="missing",
                detail="admission record is missing",
            )
        record = observed.record
        self._validate_bindings(invocation, request, record)
        if (
            record.state == "running"
            and record.owner_function_call_id == function_call_id
        ):
            return ControllerStartDecision(
                run_id=invocation.run_id,
                function_call_id=function_call_id,
                admitted=True,
                state="running",
                detail="this FunctionCall already owns the admission",
            )
        if record.state != "prepared":
            return ControllerStartDecision(
                run_id=invocation.run_id,
                function_call_id=function_call_id,
                admitted=False,
                state=record.state,
                detail="admission is no longer prepared; exit before Harbor",
            )
        replacement = transition_admission(
            record,
            "running",
            timestamp=self._timestamp(),
            owner_function_call_id=function_call_id,
        )
        try:
            self._store.update_admission(observed, replacement)
        except S3CasConflictError:
            winner = self._store.read_admission(invocation.run_id)
            state = winner.record.state if winner is not None else "missing"
            owner = winner.record.owner_function_call_id if winner is not None else None
            if winner is not None:
                self._validate_bindings(invocation, request, winner.record)
            return ControllerStartDecision(
                run_id=invocation.run_id,
                function_call_id=function_call_id,
                admitted=state == "running" and owner == function_call_id,
                state=state,
                detail=(
                    "this FunctionCall owns the concurrently committed revision"
                    if state == "running" and owner == function_call_id
                    else "another writer won admission; exit before Harbor"
                ),
            )
        return ControllerStartDecision(
            run_id=invocation.run_id,
            function_call_id=function_call_id,
            admitted=True,
            state="running",
            detail="FunctionCall claimed admission",
        )

    def publish_terminal_and_finish(
        self,
        invocation: ControllerInvocation,
        terminal: TerminalRecord,
        *,
        function_call_id: str,
    ) -> str:
        """Authorize terminal publication, publish proof last, then CAS it."""
        invocation, request = self._validate_invocation(invocation)
        observed = self._store.read_admission(invocation.run_id)
        if observed is None:
            raise ControllerIdentityError("admission record is missing")
        self._validate_bindings(invocation, request, observed.record)
        self._validate_terminal_authority(
            invocation,
            observed.record,
            terminal,
            function_call_id=function_call_id,
        )
        expected_digest = sha256_hex(canonical_model_bytes(terminal))
        digest = self._store.publish_terminal(terminal)
        if digest != expected_digest:
            raise ControllerIdentityError(
                "published terminal digest does not match the authorized terminal"
            )
        for _attempt in range(3):
            record = observed.record
            self._validate_bindings(invocation, request, record)
            if record.owner_function_call_id != function_call_id:
                raise ControllerIdentityError("FunctionCall does not own the admission")
            if record.state == "terminal":
                if record.terminal_sha256 != digest:
                    raise ControllerIdentityError(
                        "admission terminal digest conflicts with terminal proof"
                    )
                return digest
            if record.state not in {"running", "cancelling", "cancelled", "failed"}:
                raise ControllerIdentityError(
                    "admission state "
                    f"{record.state!r} cannot acknowledge terminal proof"
                )
            replacement = transition_admission(
                record,
                "terminal",
                timestamp=self._timestamp(),
                terminal_sha256=digest,
            )
            try:
                self._store.update_admission(observed, replacement)
            except S3CasConflictError:
                refreshed = self._store.read_admission(invocation.run_id)
                if refreshed is None:
                    raise ControllerIdentityError(
                        "admission disappeared after terminal publication"
                    ) from None
                observed = refreshed
                continue
            return digest
        raise S3CasConflictError(
            "admission kept changing after terminal publication; proof is durable"
        )

    def _validate_invocation(
        self, invocation: ControllerInvocation
    ) -> tuple[ControllerInvocation, RequestRecord]:
        invocation = ControllerInvocation.model_validate(invocation.model_dump())
        request = self._store.read_request(
            invocation.run_id,
            invocation.request_sha256,
            invocation.request_key,
        )
        if sha256_hex(canonical_model_bytes(request)) != invocation.request_sha256:
            raise ControllerIdentityError(
                "immutable request digest does not match the controller invocation"
            )
        if (
            request.run_id != invocation.run_id
            or request.plan_sha256 != invocation.plan_sha256
            or request.plan.storage != invocation.storage
        ):
            raise ControllerIdentityError(
                "immutable request does not match the controller invocation"
            )
        return invocation, request

    @staticmethod
    def _validate_bindings(
        invocation: ControllerInvocation,
        request: RequestRecord,
        admission: AdmissionRecord,
    ) -> None:
        if (
            admission.run_id != invocation.run_id
            or admission.request_sha256 != invocation.request_sha256
            or admission.plan_sha256 != invocation.plan_sha256
            or request.run_id != admission.run_id
            or request.plan_sha256 != admission.plan_sha256
        ):
            raise ControllerIdentityError(
                "admission, request, plan, and controller invocation do not match"
            )

    @staticmethod
    def _validate_terminal_authority(
        invocation: ControllerInvocation,
        admission: AdmissionRecord,
        terminal: TerminalRecord,
        *,
        function_call_id: str,
    ) -> None:
        if admission.owner_function_call_id != function_call_id:
            raise ControllerIdentityError("FunctionCall does not own the admission")
        if admission.state not in {"running", "cancelling"}:
            raise ControllerIdentityError(
                "terminal publication requires running or cancelling admission"
            )
        if (
            terminal.run_id != invocation.run_id
            or terminal.request_sha256 != invocation.request_sha256
        ):
            raise ControllerIdentityError(
                "terminal does not match the authorized run and request"
            )

    def mark_failed(self, run_id: str, *, function_call_id: str) -> None:
        """Record controller failure only when no terminal proof was published."""
        observed = self._store.read_admission(run_id)
        if observed is None:
            return
        record = observed.record
        if record.owner_function_call_id != function_call_id or record.state not in {
            "running",
            "cancelling",
        }:
            return
        self._store.update_admission(
            observed,
            transition_admission(record, "failed", timestamp=self._timestamp()),
        )


class ModalControllerClient:
    """Adapter for one deployed Modal Function. It has no foreground call path."""

    def __init__(self, app_name: str, function_name: str) -> None:
        self._app_name = app_name
        self._function_name = function_name

    def spawn(self, invocation: ControllerInvocation) -> str:
        function = modal.Function.from_name(self._app_name, self._function_name)
        call = function.spawn(canonical_model_bytes(invocation))
        return call.object_id

    def inspect(self, call_id: str) -> ControllerCallState:
        call = modal.FunctionCall.from_id(call_id)
        try:
            call.get(timeout=0)
        except OutputExpiredError:
            return ControllerCallState(call_id=call_id, state="expired")
        except TimeoutError:
            return ControllerCallState(call_id=call_id, state="running")
        except RemoteError as error:
            return ControllerCallState(
                call_id=call_id,
                state="failed",
                detail=type(error).__name__,
            )
        except modal.exception.Error as error:
            return ControllerCallState(
                call_id=call_id,
                state="inspection_failed",
                detail=type(error).__name__,
            )
        return ControllerCallState(call_id=call_id, state="succeeded")

    def cancel(self, call_id: str) -> None:
        modal.FunctionCall.from_id(call_id).cancel(terminate_containers=True)


class FakeDetachedController:
    """Deterministic detached controller for service and race tests."""

    def __init__(self) -> None:
        self.spawned: list[ControllerInvocation] = []
        self.cancelled: list[str] = []
        self.operations: list[tuple[str, str]] = []
        self.states: dict[str, ControllerCallState] = {}
        self._lock = threading.Lock()

    def spawn(self, invocation: ControllerInvocation) -> str:
        with self._lock:
            call_id = f"fc-{len(self.spawned) + 1}"
            self.spawned.append(invocation)
            self.operations.append(("spawn", call_id))
            self.states[call_id] = ControllerCallState(
                call_id=call_id,
                state="running",
            )
        return call_id

    def inspect(self, call_id: str) -> ControllerCallState:
        self.operations.append(("inspect", call_id))
        return self.states.get(
            call_id,
            ControllerCallState(
                call_id=call_id,
                state="inspection_failed",
                detail="unknown call",
            ),
        )

    def cancel(self, call_id: str) -> None:
        self.operations.append(("cancel", call_id))
        self.cancelled.append(call_id)

    def set_state(
        self,
        call_id: str,
        state: Literal[
            "running", "succeeded", "failed", "expired", "inspection_failed"
        ],
        *,
        detail: str | None = None,
    ) -> None:
        self.states[call_id] = ControllerCallState(
            call_id=call_id,
            state=state,
            detail=detail,
        )
