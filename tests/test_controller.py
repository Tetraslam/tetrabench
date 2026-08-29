from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from modal.exception import (
    ExecutionError,
    OutputExpiredError,
    RemoteError,
    TimeoutError,
)

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller import (
    ControllerAdmissionService,
    ControllerIdentityError,
    ControllerInvocation,
    ModalControllerClient,
)
from tetrabench.models import ResolvedPlan
from tetrabench.plan import canonical_model_bytes, parse_canonical_model, plan_digest
from tetrabench.records import (
    AdmissionRecord,
    ContextManifest,
    RequestRecord,
    TerminalRecord,
    new_admission,
    transition_admission,
)
from tetrabench.s3 import AdmissionRead, S3CasConflictError


def _request() -> RequestRecord:
    plan = ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "systems-design",
            "controller": {
                "kind": "modal",
                "app_name": "tetrabench",
                "function_name": "controller",
                "secret_name": "secret-reference-not-value",
            },
            "execution": {"kind": "modal"},
            "storage": {
                "provider": "aws",
                "bucket": "bucket",
                "region": "us-west-2",
            },
            "selection": {},
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


def _invocation(request: RequestRecord | None = None) -> ControllerInvocation:
    request = request or _request()
    request_sha256 = sha256_hex(canonical_model_bytes(request))
    storage = request.plan.storage
    assert storage is not None
    return ControllerInvocation(
        schema_version=1,
        run_id=request.run_id,
        request_sha256=request_sha256,
        plan_sha256=request.plan_sha256,
        request_key=f"runs/{request.run_id}/requests/{request_sha256}.json",
        storage=storage,
    )


def _prepared_admission() -> AdmissionRecord:
    return new_admission(
        _request(),
        timestamp="2026-08-28T20:00:00Z",
    )


class _CasStore:
    def __init__(
        self, admission: AdmissionRecord, request: RequestRecord | None = None
    ) -> None:
        self.value = AdmissionRead(admission, "etag-0")
        self.request = request or _request()
        self._lock = threading.Lock()
        self.operations: list[str] = []

    def read_admission(self, run_id: str) -> AdmissionRead | None:
        assert run_id == "run-1"
        return self.value

    def read_request(
        self, run_id: str, request_sha256: str, request_key: str
    ) -> RequestRecord:
        self.operations.append("read-request")
        assert run_id == self.request.run_id
        assert request_key == f"runs/{run_id}/requests/{request_sha256}.json"
        return self.request

    def update_admission(
        self, expected: AdmissionRead, replacement: AdmissionRecord
    ) -> AdmissionRead:
        with self._lock:
            if expected.etag != self.value.etag:
                raise S3CasConflictError("stale")
            self.value = AdmissionRead(replacement, f"etag-{replacement.revision}")
            self.operations.append(f"cas:{replacement.state}")
            return self.value

    def publish_terminal(self, terminal) -> str:
        self.operations.append("publish-terminal")
        return sha256_hex(canonical_model_bytes(terminal))


def test_duplicate_spawned_controllers_have_one_admission_winner() -> None:
    store = _CasStore(_prepared_admission())
    service = ControllerAdmissionService(
        store, timestamp=lambda: "2026-08-28T20:00:01Z"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = tuple(
            pool.map(
                lambda call: service.claim(_invocation(), call),
                ("fc-1", "fc-2"),
            )
        )

    assert sum(decision.admitted for decision in decisions) == 1
    winner = next(
        decision.function_call_id for decision in decisions if decision.admitted
    )
    assert store.value.record.owner_function_call_id == winner
    assert store.value.record.state == "running"
    assert "exit before Harbor" in next(
        decision.detail for decision in decisions if not decision.admitted
    )


def test_controller_starting_after_prepared_cancellation_exits_before_harbor() -> None:
    cancelled = transition_admission(
        _prepared_admission(),
        "cancelled",
        timestamp="2026-08-28T20:00:01Z",
    )
    decision = ControllerAdmissionService(_CasStore(cancelled)).claim(
        _invocation(), "fc-1"
    )

    assert not decision.admitted
    assert decision.state == "cancelled"
    assert "exit before Harbor" in decision.detail


def test_controller_publishes_terminal_before_marking_admission_terminal() -> None:
    store = _CasStore(_prepared_admission())
    service = ControllerAdmissionService(
        store, timestamp=lambda: "2026-08-28T20:00:01Z"
    )
    invocation = _invocation()
    assert service.claim(invocation, "fc-1").admitted
    terminal = TerminalRecord(
        schema_version=1,
        run_id="run-1",
        request_sha256=invocation.request_sha256,
        winning_attempt_id="attempt-1",
        outcome="failed",
        harbor_version="0.22.0",
        artifacts=(),
        evidence=(),
        warnings=(),
    )

    digest = service.publish_terminal_and_finish(
        invocation, terminal, function_call_id="fc-1"
    )

    assert digest == sha256_hex(canonical_model_bytes(terminal))
    assert store.value.record.state == "terminal"
    assert store.value.record.terminal_sha256 == digest
    assert tuple(item.state for item in store.value.record.history) == (
        "prepared",
        "running",
        "terminal",
    )
    assert store.operations[-2:] == ["publish-terminal", "cas:terminal"]


def test_cancelled_admission_cannot_publish_terminal_proof() -> None:
    running = transition_admission(
        _prepared_admission(),
        "running",
        timestamp="2026-08-28T20:00:01Z",
        owner_function_call_id="fc-1",
    )
    cancelling = transition_admission(
        running, "cancelling", timestamp="2026-08-28T20:00:02Z"
    )
    cancelled = transition_admission(
        cancelling, "cancelled", timestamp="2026-08-28T20:00:03Z"
    )
    store = _CasStore(cancelled)
    service = ControllerAdmissionService(
        store, timestamp=lambda: "2026-08-28T20:00:04Z"
    )
    invocation = _invocation()
    terminal = TerminalRecord(
        schema_version=1,
        run_id="run-1",
        request_sha256=invocation.request_sha256,
        winning_attempt_id="attempt-1",
        outcome="failed",
        harbor_version="0.22.0",
        artifacts=(),
        evidence=(),
        warnings=(),
    )

    with pytest.raises(ControllerIdentityError, match="running or cancelling"):
        service.publish_terminal_and_finish(
            invocation, terminal, function_call_id="fc-1"
        )

    assert store.value.record.state == "cancelled"
    assert store.value.record.history[-1].state == "cancelled"
    assert "publish-terminal" not in store.operations


def test_stale_invocation_cannot_claim_admission() -> None:
    store = _CasStore(_prepared_admission())
    invocation = _invocation().model_copy(update={"plan_sha256": "f" * 64})

    with pytest.raises(ControllerIdentityError, match="immutable request"):
        ControllerAdmissionService(store).claim(invocation, "fc-1")

    assert store.value.record.state == "prepared"
    assert not any(operation.startswith("cas:") for operation in store.operations)


def test_wrong_invocation_storage_cannot_claim_admission() -> None:
    store = _CasStore(_prepared_admission())
    invocation = _invocation()
    wrong_storage = invocation.storage.model_copy(update={"region": "us-east-1"})
    invocation = invocation.model_copy(update={"storage": wrong_storage})

    with pytest.raises(ControllerIdentityError, match="immutable request"):
        ControllerAdmissionService(store).claim(invocation, "fc-1")

    assert store.value.record.state == "prepared"
    assert not any(operation.startswith("cas:") for operation in store.operations)


def test_non_owner_cannot_publish_terminal_proof() -> None:
    store = _CasStore(_prepared_admission())
    service = ControllerAdmissionService(
        store, timestamp=lambda: "2026-08-28T20:00:01Z"
    )
    invocation = _invocation()
    assert service.claim(invocation, "fc-owner").admitted
    terminal = TerminalRecord(
        schema_version=1,
        run_id="run-1",
        request_sha256=invocation.request_sha256,
        winning_attempt_id="attempt-1",
        outcome="failed",
        harbor_version="0.22.0",
        artifacts=(),
        evidence=(),
        warnings=(),
    )

    with pytest.raises(ControllerIdentityError, match="does not own"):
        service.publish_terminal_and_finish(
            invocation, terminal, function_call_id="fc-other"
        )

    assert "publish-terminal" not in store.operations


def test_mismatched_terminal_cannot_be_published() -> None:
    store = _CasStore(_prepared_admission())
    service = ControllerAdmissionService(
        store, timestamp=lambda: "2026-08-28T20:00:01Z"
    )
    invocation = _invocation()
    assert service.claim(invocation, "fc-owner").admitted
    terminal = TerminalRecord(
        schema_version=1,
        run_id="run-1",
        request_sha256="f" * 64,
        winning_attempt_id="attempt-1",
        outcome="failed",
        harbor_version="0.22.0",
        artifacts=(),
        evidence=(),
        warnings=(),
    )

    with pytest.raises(ControllerIdentityError, match="authorized run and request"):
        service.publish_terminal_and_finish(
            invocation, terminal, function_call_id="fc-owner"
        )

    assert "publish-terminal" not in store.operations


def test_modal_adapter_only_spawns_deployed_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tetrabench.controller as controller_module

    calls: list[tuple[str, object]] = []

    class Function:
        @staticmethod
        def from_name(app_name: str, function_name: str):
            calls.append(("from_name", (app_name, function_name)))

            class Deployed:
                @staticmethod
                def spawn(payload: bytes):
                    calls.append(("spawn", payload))
                    return SimpleNamespace(object_id="fc-deployed")

            return Deployed()

    monkeypatch.setattr(controller_module.modal, "Function", Function)
    adapter = ModalControllerClient("app", "controller")
    assert adapter.spawn(_invocation()) == "fc-deployed"
    assert calls[0] == ("from_name", ("app", "controller"))
    payload = calls[1][1]
    assert isinstance(payload, bytes)
    assert parse_canonical_model(payload, ControllerInvocation) == _invocation()


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (TimeoutError(), "running"),
        (OutputExpiredError(), "expired"),
        (RemoteError("failed"), "failed"),
        (ExecutionError("broken"), "inspection_failed"),
    ],
)
def test_modal_adapter_classifies_nonblocking_get(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    state: str,
) -> None:
    import tetrabench.controller as controller_module

    timeouts: list[float] = []

    class Call:
        @staticmethod
        def from_id(call_id: str):
            assert call_id == "fc-1"

            class Handle:
                @staticmethod
                def get(*, timeout: float):
                    timeouts.append(timeout)
                    raise error

            return Handle()

    monkeypatch.setattr(controller_module.modal, "FunctionCall", Call)
    result = ModalControllerClient("app", "controller").inspect("fc-1")
    assert result.state == state
    assert timeouts == [0]


def test_modal_adapter_success_and_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    import tetrabench.controller as controller_module

    operations: list[tuple[str, object]] = []

    class Call:
        @staticmethod
        def from_id(call_id: str):
            operations.append(("from_id", call_id))

            class Handle:
                @staticmethod
                def get(*, timeout: float):
                    operations.append(("get", timeout))
                    return {"ignored": "controller output is not persisted locally"}

                @staticmethod
                def cancel(*, terminate_containers: bool):
                    operations.append(("cancel", terminate_containers))

            return Handle()

    monkeypatch.setattr(controller_module.modal, "FunctionCall", Call)
    adapter = ModalControllerClient("app", "controller")
    assert adapter.inspect("fc-1").state == "succeeded"
    adapter.cancel("fc-1")
    assert operations == [
        ("from_id", "fc-1"),
        ("get", 0),
        ("from_id", "fc-1"),
        ("cancel", True),
    ]
