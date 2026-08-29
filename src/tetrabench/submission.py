"""Crash-conservative detached submission orchestration."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tetrabench.canonical_json import sha256_hex
from tetrabench.catalog import SectionName
from tetrabench.config import load_project_config
from tetrabench.context import SealedContext, seal_context
from tetrabench.controller import ControllerInvocation, DetachedControllerClient
from tetrabench.models import ResolvedPlan
from tetrabench.plan import canonical_model_bytes, plan_digest, resolve_plan
from tetrabench.receipts import (
    ControllerCallReceipt,
    PhysicalSubmissionAttempt,
    ReceiptConflictError,
    ReceiptStore,
    SubmissionReceipt,
    SubmissionTransition,
    append_submission_attempt,
    record_spawn_return,
)
from tetrabench.records import (
    AdmissionRecord,
    ContentObject,
    RequestRecord,
    new_admission,
    utc_now_timestamp,
    validate_run_id,
)
from tetrabench.s3 import AdmissionRead, CoordinationTopology, S3CasConflictError
from tetrabench.storage import request_key


class SubmissionStore(Protocol):
    def require_coordination_safe(self) -> CoordinationTopology: ...

    def publish_content(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ContentObject: ...

    def publish_request(self, request: RequestRecord) -> str: ...

    def create_admission(self, admission: AdmissionRecord) -> AdmissionRead: ...

    def read_admission(self, run_id: str) -> AdmissionRead | None: ...


class SubmissionRefusedError(RuntimeError):
    """Submission cannot proceed without unsafe or duplicate side effects."""


@dataclass(frozen=True, slots=True)
class PreparedSubmission:
    plan: ResolvedPlan
    sealed_context: SealedContext
    request: RequestRecord


def prepare_submission(
    root: Path,
    section: SectionName,
    profile: str | None = None,
    *,
    run_id: str | None = None,
) -> PreparedSubmission:
    """Resolve and seal locally, refusing empty plans before cloud access."""
    plan = resolve_plan(root, section, profile)
    if not plan.runnable or not plan.trials:
        reasons = "; ".join(plan.not_runnable_reasons)
        raise SubmissionRefusedError(f"plan is not runnable: {reasons}")
    if plan.storage is None:
        raise SubmissionRefusedError("submission requires storage configuration")
    if plan.controller.kind != "modal" or plan.execution.kind != "modal":
        raise SubmissionRefusedError("submit supports detached Modal execution only")

    config = load_project_config(root, profile=profile)
    sealed = seal_context(root, config.context, key_prefix=plan.storage.prefix)
    manifest_plan_context = tuple(
        (item.destination, item.mode, item.content.size, item.content.sha256)
        for item in sealed.manifest.files
    )
    plan_context = tuple(
        (item.destination, item.mode, item.size, item.sha256) for item in plan.context
    )
    if manifest_plan_context != plan_context:
        raise SubmissionRefusedError(
            "selected context changed while preparing submission"
        )

    selected_run_id = validate_run_id(run_id or f"run-{uuid.uuid4().hex}")
    manifest_bytes = canonical_model_bytes(sealed.manifest)
    request = RequestRecord(
        schema_version=1,
        run_id=selected_run_id,
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(manifest_bytes),
        context_manifest=sealed.manifest,
    )
    return PreparedSubmission(plan=plan, sealed_context=sealed, request=request)


class SubmissionService:
    """Publish a request, establish durable admission, then spawn detached."""

    def __init__(
        self,
        store: SubmissionStore,
        controller: DetachedControllerClient,
        receipts: ReceiptStore,
        *,
        after_step: Callable[[str], None] | None = None,
        timestamp: Callable[[], str] = utc_now_timestamp,
    ) -> None:
        self._store = store
        self._controller = controller
        self._receipts = receipts
        self._after_step = after_step or (lambda _step: None)
        self._timestamp = timestamp

    def submit(self, prepared: PreparedSubmission) -> SubmissionReceipt:
        self._validate_prepared(prepared)
        self._store.require_coordination_safe()
        with self._receipts.lock(prepared.request.run_id):
            return self._submit_locked(prepared, recovery=False)

    def recover(self, prepared: PreparedSubmission) -> SubmissionReceipt:
        """Explicitly spawn another call while durable admission is prepared."""
        self._validate_prepared(prepared)
        self._store.require_coordination_safe()
        with self._receipts.lock(prepared.request.run_id):
            return self._submit_locked(prepared, recovery=True)

    def recover_request(self, request: RequestRecord) -> str:
        """Spawn from an already-published immutable request and prepared admission."""
        self._validate_recovery_request(request)
        self._store.require_coordination_safe()
        durable = self._store.read_admission(request.run_id)
        if durable is None:
            raise SubmissionRefusedError("recovery requires a prepared admission")
        self._validate_prepared_admission(request, durable.record)
        receipt: SubmissionReceipt | None
        try:
            receipt = self._record_intent(request, recovery=True)
        except (OSError, ReceiptConflictError, TypeError, ValueError):
            receipt = None
        call_id = self._spawn(request)
        if receipt is not None:
            try:
                self._record_spawn(receipt, call_id)
            except (OSError, ReceiptConflictError, TypeError, ValueError):
                pass
        return call_id

    @staticmethod
    def _validate_prepared(prepared: PreparedSubmission) -> None:
        request = prepared.request
        if not prepared.plan.runnable or not prepared.plan.trials:
            raise SubmissionRefusedError("submission requires a nonempty runnable plan")
        if request.plan != prepared.plan:
            raise SubmissionRefusedError("prepared request and plan disagree")
        if request.context_manifest != prepared.sealed_context.manifest:
            raise SubmissionRefusedError("prepared request and sealed context disagree")
        if (
            prepared.plan.storage is None
            or prepared.plan.controller.kind != "modal"
            or prepared.plan.execution.kind != "modal"
        ):
            raise SubmissionRefusedError(
                "submission requires detached Modal execution and storage"
            )

    @staticmethod
    def _validate_recovery_request(request: RequestRecord) -> None:
        plan = request.plan
        if not plan.runnable or not plan.trials:
            raise SubmissionRefusedError("recovery requires a nonempty runnable plan")
        if (
            plan.storage is None
            or plan.controller.kind != "modal"
            or plan.execution.kind != "modal"
        ):
            raise SubmissionRefusedError(
                "recovery requires detached Modal execution and storage"
            )

    @staticmethod
    def _validate_prepared_admission(
        request: RequestRecord, admission: AdmissionRecord
    ) -> None:
        request_sha256 = sha256_hex(canonical_model_bytes(request))
        if (
            admission.request_sha256 != request_sha256
            or admission.plan_sha256 != request.plan_sha256
        ):
            raise SubmissionRefusedError("durable admission belongs to another request")
        if admission.state != "prepared":
            raise SubmissionRefusedError(
                f"durable admission is {admission.state}; no controller was spawned"
            )

    def _submit_locked(
        self,
        prepared: PreparedSubmission,
        *,
        recovery: bool,
    ) -> SubmissionReceipt:
        request = prepared.request
        request_bytes = canonical_model_bytes(request)
        request_sha256 = sha256_hex(request_bytes)

        for sealed_file in prepared.sealed_context.files:
            published = self._store.publish_content(
                sealed_file.content,
                media_type=sealed_file.descriptor.media_type,
            )
            if published != sealed_file.descriptor:
                raise ReceiptConflictError("published context descriptor changed")
        manifest = canonical_model_bytes(prepared.sealed_context.manifest)
        published_manifest = self._store.publish_content(
            manifest,
            media_type="application/json",
        )
        if published_manifest.sha256 != request.context_manifest_sha256:
            raise ReceiptConflictError("published context manifest digest changed")
        self._after_step("context-published")

        published_request = self._store.publish_request(request)
        if published_request != request_sha256:
            raise ReceiptConflictError("published request digest changed")
        self._after_step("request-published")

        durable = self._store.read_admission(request.run_id) if recovery else None
        if durable is None:
            if recovery:
                raise SubmissionRefusedError("recovery requires a prepared admission")
            try:
                durable = self._store.create_admission(
                    new_admission(request, timestamp=self._timestamp())
                )
            except S3CasConflictError:
                durable = self._store.read_admission(request.run_id)
                if durable is None:
                    raise SubmissionRefusedError(
                        "admission create conflicted but no record is readable; "
                        "submission is ambiguous and was not retried"
                    ) from None
        self._validate_prepared_admission(request, durable.record)
        self._after_step("admission-prepared")

        return self._record_and_spawn(request, recovery=recovery)

    def _record_and_spawn(
        self, request: RequestRecord, *, recovery: bool
    ) -> SubmissionReceipt:
        receipt = self._record_intent(request, recovery=recovery)
        call_id = self._spawn(request)
        return self._record_spawn(receipt, call_id)

    def _record_intent(
        self, request: RequestRecord, *, recovery: bool
    ) -> SubmissionReceipt:
        request_sha256 = sha256_hex(canonical_model_bytes(request))

        attempt_id = f"{'recover' if recovery else 'submit'}-{uuid.uuid4().hex}"
        attempt = PhysicalSubmissionAttempt(
            attempt_id=attempt_id,
            transitions=(
                SubmissionTransition(
                    sequence=0,
                    type="recovery-intended" if recovery else "admission-observed",
                ),
            ),
        )
        receipt = self._receipts.read(request.run_id)
        if receipt is None:
            receipt = SubmissionReceipt(
                schema_version=2,
                run_id=request.run_id,
                request_sha256=request_sha256,
                plan_sha256=request.plan_sha256,
                context_manifest_sha256=request.context_manifest_sha256,
                attempts=(attempt,),
            )
        else:
            identity = (
                receipt.request_sha256,
                receipt.plan_sha256,
                receipt.context_manifest_sha256,
            )
            if identity != (
                request_sha256,
                request.plan_sha256,
                request.context_manifest_sha256,
            ):
                raise ReceiptConflictError("local receipt identity conflicts")
            receipt = append_submission_attempt(receipt, attempt)
        self._receipts.write(receipt)
        self._after_step("receipt-recorded")
        return receipt

    def _spawn(self, request: RequestRecord) -> str:
        request_sha256 = sha256_hex(canonical_model_bytes(request))
        storage = request.plan.storage
        if storage is None:
            raise SubmissionRefusedError(
                "controller spawn requires resolved storage configuration"
            )
        invocation = ControllerInvocation(
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
        call_id = self._controller.spawn(invocation)
        self._after_step("spawned")
        return call_id

    def _record_spawn(
        self, receipt: SubmissionReceipt, call_id: str
    ) -> SubmissionReceipt:
        receipt = record_spawn_return(
            receipt,
            ControllerCallReceipt(call_id=call_id),
        )
        self._receipts.write(receipt)
        self._after_step("spawn-recorded")
        return receipt
