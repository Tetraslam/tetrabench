from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tetrabench.canonical_json import sha256_hex
from tetrabench.context import SealedContext
from tetrabench.controller import FakeDetachedController
from tetrabench.models import ResolvedPlan
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.receipts import ReceiptStore
from tetrabench.records import (
    AdmissionRecord,
    ContentObject,
    ContextManifest,
    RequestRecord,
)
from tetrabench.s3 import (
    AdmissionRead,
    S3CasConflictError,
    UnsafeCoordinationTopologyError,
)
from tetrabench.storage import content_object_key
from tetrabench.submission import (
    ControllerLaunchConfiguration,
    PreparedSubmission,
    SubmissionRefusedError,
    SubmissionService,
)


class _MemorySubmissionStore:
    def __init__(self) -> None:
        self.operations: list[str] = []
        self.admission: AdmissionRead | None = None
        self._revision = 0
        self._lock = threading.Lock()
        self.coordination_safe = True

    def require_coordination_safe(self):
        if not self.coordination_safe:
            self.operations.append("preflight-rejected")
            raise UnsafeCoordinationTopologyError("unsafe topology")
        return None

    def publish_content(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ContentObject:
        self.operations.append("content")
        digest = sha256_hex(data)
        return ContentObject(
            sha256=digest,
            key=content_object_key(digest, prefix="tenant"),
            size=len(data),
            media_type=media_type,
        )

    def publish_request(self, request: RequestRecord) -> str:
        self.operations.append("request")
        return sha256_hex(canonical_model_bytes(request))

    def create_admission(self, admission: AdmissionRecord) -> AdmissionRead:
        with self._lock:
            if self.admission is not None:
                raise S3CasConflictError("exists")
            self._revision += 1
            self.admission = AdmissionRead(admission, f"etag-{self._revision}")
            self.operations.append("admission-create")
            return self.admission

    def read_admission(self, run_id: str) -> AdmissionRead | None:
        self.operations.append("admission-read")
        return self.admission


def _prepared() -> PreparedSubmission:
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
                "prefix": "tenant",
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
    request = RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )
    return PreparedSubmission(
        plan=plan,
        sealed_context=SealedContext(manifest=manifest, files=()),
        request=request,
        controller_launch=ControllerLaunchConfiguration(
            app_name="tetrabench",
            function_name="controller",
            environment_name="tetrabench-default",
        ),
    )


class _Crash(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("boundary", "admission_exists", "spawn_count", "receipt_event"),
    [
        ("context-published", False, 0, None),
        ("request-published", False, 0, None),
        ("admission-prepared", True, 0, None),
        ("receipt-recorded", True, 0, "admission-observed"),
        ("spawned", True, 1, "admission-observed"),
        ("spawn-recorded", True, 1, "spawn-returned"),
    ],
)
def test_submission_crash_boundaries_preserve_durable_admission(
    tmp_path: Path,
    boundary: str,
    admission_exists: bool,
    spawn_count: int,
    receipt_event: str | None,
) -> None:
    receipts = ReceiptStore(tmp_path / "state")
    controller = FakeDetachedController()
    store = _MemorySubmissionStore()

    def crash(step: str) -> None:
        if step == boundary:
            raise _Crash(step)

    service = SubmissionService(
        store,
        controller,
        receipts,
        after_step=crash,
        timestamp=lambda: "2026-08-28T20:00:00Z",
    )
    with pytest.raises(_Crash, match=boundary):
        service.submit(_prepared())

    assert (store.admission is not None) is admission_exists
    assert len(controller.spawned) == spawn_count
    receipt = receipts.read("run-1")
    if receipt_event is None:
        assert receipt is None
    else:
        assert receipt is not None
        assert receipt.attempts[-1].transitions[-1].type == receipt_event


def test_explicit_recovery_spawns_for_still_prepared_admission(tmp_path: Path) -> None:
    store = _MemorySubmissionStore()
    controller = FakeDetachedController()
    receipts = ReceiptStore(tmp_path)
    first = SubmissionService(
        store,
        controller,
        receipts,
        after_step=lambda step: (
            (_ for _ in ()).throw(_Crash(step)) if step == "receipt-recorded" else None
        ),
        timestamp=lambda: "2026-08-28T20:00:00Z",
    )
    with pytest.raises(_Crash):
        first.submit(_prepared())

    recovered = SubmissionService(
        store,
        controller,
        receipts,
        timestamp=lambda: "2026-08-28T20:00:01Z",
    ).recover(_prepared())

    assert len(controller.spawned) == 1
    assert len(recovered.attempts) == 2
    assert recovered.attempts[-1].controller_calls[0].call_id == "fc-1"


def test_cross_host_submitters_may_spawn_against_same_prepared_record(
    tmp_path: Path,
) -> None:
    store = _MemorySubmissionStore()
    controller = FakeDetachedController()

    def submit(host: str) -> str:
        receipt = SubmissionService(
            store,
            controller,
            ReceiptStore(tmp_path / host),
            timestamp=lambda: "2026-08-28T20:00:00Z",
        ).submit(_prepared())
        return receipt.attempts[-1].controller_calls[0].call_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        call_ids = tuple(pool.map(submit, ("host-a", "host-b")))

    assert len(set(call_ids)) == 2
    assert len(controller.spawned) == 2
    assert store.admission is not None
    assert store.admission.record.state == "prepared"


def test_cli_side_submission_never_claims_owner(tmp_path: Path) -> None:
    store = _MemorySubmissionStore()
    receipt = SubmissionService(
        store,
        FakeDetachedController(),
        ReceiptStore(tmp_path),
        timestamp=lambda: "2026-08-28T20:00:00Z",
    ).submit(_prepared())

    assert store.admission is not None
    assert store.admission.record.owner_function_call_id is None
    assert receipt.attempts[-1].controller_calls[0].call_id == "fc-1"


def test_receipt_does_not_serialize_modal_secret_name(tmp_path: Path) -> None:
    receipt = SubmissionService(
        _MemorySubmissionStore(),
        FakeDetachedController(),
        ReceiptStore(tmp_path),
        timestamp=lambda: "2026-08-28T20:00:00Z",
    ).submit(_prepared())
    data = ReceiptStore(tmp_path).path_for(receipt.run_id).read_bytes()
    assert b"secret-reference-not-value" not in data
    assert b"bucket" not in data


def test_service_rejects_empty_plan_before_receipt_s3_or_modal(tmp_path: Path) -> None:
    base = _prepared()
    empty_plan = ResolvedPlan.model_validate(
        base.plan.model_dump()
        | {"trials": (), "runnable": False, "not_runnable_reasons": ("empty",)}
    )
    empty_request = RequestRecord(
        schema_version=1,
        run_id="run-empty",
        plan_sha256=plan_digest(empty_plan),
        plan=empty_plan,
        context_manifest_sha256=base.request.context_manifest_sha256,
        context_manifest=base.sealed_context.manifest,
    )
    store = _MemorySubmissionStore()
    controller = FakeDetachedController()
    receipts = ReceiptStore(tmp_path / "empty-state")
    with pytest.raises(SubmissionRefusedError, match="nonempty runnable"):
        SubmissionService(store, controller, receipts).submit(
            PreparedSubmission(
                plan=empty_plan,
                sealed_context=base.sealed_context,
                request=empty_request,
                controller_launch=base.controller_launch,
            )
        )
    assert store.operations == []
    assert controller.spawned == []
    assert receipts.read("run-empty") is None


def test_unsafe_topology_rejects_before_any_submission_mutation(tmp_path: Path) -> None:
    store = _MemorySubmissionStore()
    store.coordination_safe = False
    controller = FakeDetachedController()
    receipts = ReceiptStore(tmp_path)

    with pytest.raises(UnsafeCoordinationTopologyError, match="unsafe topology"):
        SubmissionService(store, controller, receipts).submit(_prepared())

    assert store.operations == ["preflight-rejected"]
    assert controller.spawned == []
    assert receipts.read("run-1") is None
