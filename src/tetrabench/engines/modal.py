"""Modal engine adapter over the existing CAS and immutable terminal protocol."""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import TypeAdapter

from tetrabench.artifacts import ArtifactPullService
from tetrabench.canonical_json import sha256_hex
from tetrabench.config import load_project_config
from tetrabench.controller import ModalControllerClient
from tetrabench.engines import Capabilities
from tetrabench.harbor import ModalChildObserver, S3ChildIdentitySource
from tetrabench.integrity import ArtifactVerificationService
from tetrabench.lifecycle import (
    CancellationService,
    RecoveryService,
    StatusService,
    validate_request_plan_storage_binding,
)
from tetrabench.models import ModalControllerConfig, RecordIdentifier
from tetrabench.plan import canonical_model_bytes
from tetrabench.preflight import check_runtime
from tetrabench.receipts import ReceiptStore
from tetrabench.records import ConflictRunState, TerminalRunState
from tetrabench.remote import RemoteResultService
from tetrabench.run_reference import RunReference, RunReferenceStore
from tetrabench.s3 import create_s3_store
from tetrabench.storage import request_key
from tetrabench.submission import (
    PreparedSubmission,
    SubmissionService,
)


def _store(reference: RunReference):
    if reference.storage is None:
        raise ValueError("remote run reference requires storage")
    return create_s3_store(reference.storage)


def _controller(reference: RunReference):
    if (
        not reference.app_name
        or not reference.function_name
        or not reference.environment_name
    ):
        raise ValueError("remote run reference requires controller binding")
    return ModalControllerClient(
        reference.app_name,
        reference.function_name,
        environment_name=reference.environment_name,
    )


def _bound_store(reference: RunReference):
    store = _store(reference)
    request = store.read_request(
        reference.run_id,
        reference.request_sha256,
        request_key(
            reference.run_id, reference.request_sha256, prefix=store.storage.prefix
        ),
    )
    if request.plan.storage != reference.storage:
        raise ValueError("recorded storage and immutable request disagree")
    controller = request.plan.controller
    if controller.kind != "modal" or (
        controller.app_name != reference.app_name
        or controller.function_name != reference.function_name
    ):
        raise ValueError("recorded controller and immutable request disagree")
    return store


def legacy_result_service(profile: str | None) -> RemoteResultService:
    """Explicit profile lookup for records written before routing references."""
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("remote reads require storage configuration")
    return RemoteResultService(create_s3_store(config.storage))


def _legacy_binding(
    profile: str | None,
    *,
    run_id: str | None = None,
    environment_name: str | None = None,
    mutation: bool = False,
):
    if mutation and (environment_name is None or run_id is None):
        raise ValueError(
            "legacy records do not persist the Modal environment; supply --environment "
            "with the original namespace for cancel/recover (no namespace was guessed)"
        )
    if environment_name is not None:
        TypeAdapter(RecordIdentifier).validate_python(environment_name, strict=True)
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("remote lifecycle requires storage configuration")
    store = create_s3_store(config.storage)
    app_name, function_name = "tetrabench", "controller"
    if mutation:
        if run_id is None:
            raise ValueError("legacy mutation requires a run ID")
        state = store.read_run_state(run_id)
        if isinstance(state, ConflictRunState):
            raise ValueError("legacy run has conflicting immutable records")
        admission = store.read_admission(run_id)
        digest = (
            state.terminal.request_sha256
            if isinstance(state, TerminalRunState)
            else admission.record.request_sha256
            if admission is not None
            else None
        )
        if digest is None:
            raise ValueError("legacy mutation requires an immutable request binding")
        request = validate_request_plan_storage_binding(
            store, run_id=run_id, request_sha256=digest
        )
        if request.plan.controller.kind != "modal":
            raise ValueError(
                "legacy immutable request does not name a Modal controller"
            )
        app_name = request.plan.controller.app_name
        function_name = request.plan.controller.function_name
    # FunctionCall inspection uses globally persisted call IDs, never an inferred
    # deployment environment. Only explicit legacy mutation uses the supplied namespace.
    controller = ModalControllerClient(
        app_name,
        function_name,
        environment_name=environment_name,
    )
    return store, controller, environment_name


def legacy_status_service(profile: str | None) -> StatusService:
    store, controller, _ = _legacy_binding(profile)
    return StatusService(store, ReceiptStore(), controller)


def legacy_cancellation_service(
    profile: str | None,
    *,
    run_id: str | None = None,
    environment_name: str | None = None,
) -> CancellationService:
    check_runtime("cancel")
    store, controller, environment = _legacy_binding(
        profile, run_id=run_id, environment_name=environment_name, mutation=True
    )
    if environment is None:
        raise ValueError("legacy cancellation requires an explicit environment")
    return CancellationService(
        store,
        controller,
        ModalChildObserver(
            S3ChildIdentitySource(store),
            environment_name=environment,
        ),
    )


def legacy_recovery_service(
    profile: str | None,
    *,
    run_id: str | None = None,
    environment_name: str | None = None,
) -> RecoveryService:
    check_runtime("recover")
    store, controller, environment = _legacy_binding(
        profile, run_id=run_id, environment_name=environment_name, mutation=True
    )
    if environment is None:
        raise ValueError("legacy recovery requires an explicit environment")
    return RecoveryService(
        store,
        controller,
        ModalChildObserver(
            S3ChildIdentitySource(store),
            environment_name=environment,
        ),
        SubmissionService(store, controller, ReceiptStore()),
    )


def legacy_artifact_service(profile: str | None) -> ArtifactPullService:
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("artifact pull requires storage configuration")
    return ArtifactPullService(create_s3_store(config.storage))


def legacy_verification_service(profile: str | None) -> ArtifactVerificationService:
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("artifact verification requires storage configuration")
    return ArtifactVerificationService(create_s3_store(config.storage))


class ModalEngine:
    kind = "modal"
    capabilities = Capabilities(detached=True, default_wait=False, recover=True)

    def compile(self, settings: dict[str, object]) -> tuple[dict, dict]:
        controller = ModalControllerConfig.model_validate({"kind": "modal", **settings})
        return controller.model_dump(), {"kind": "modal"}

    def launch(self, prepared: PreparedSubmission, output: Path | None):
        check_runtime("run")
        launch = prepared.controller_launch
        if prepared.plan.storage is None or launch is None:
            raise ValueError(
                "Modal execution requires storage and a deployed controller"
            )
        reference = RunReference(
            run_id=prepared.request.run_id,
            engine=self.kind,
            request_sha256=sha256_hex(canonical_model_bytes(prepared.request)),
            storage=prepared.plan.storage,
            app_name=launch.app_name,
            function_name=launch.function_name,
            environment_name=launch.environment_name,
        )
        references = RunReferenceStore()
        with references.admission(
            reference.run_id, engine=self.kind, request_sha256=reference.request_sha256
        ):
            store = _store(reference)
            return SubmissionService(
                store, _controller(reference), references.receipts
            ).submit(prepared)

    def status(self, reference: RunReference):
        return StatusService(
            _bound_store(reference), ReceiptStore(), _controller(reference)
        ).status(reference.run_id)

    def result(self, reference: RunReference):
        return RemoteResultService(_bound_store(reference)).result(reference.run_id)

    def cancel(self, reference: RunReference):
        check_runtime("cancel")
        if reference.environment_name is None:
            raise ValueError("missing controller environment")
        store = _bound_store(reference)
        return CancellationService(
            store,
            _controller(reference),
            ModalChildObserver(
                S3ChildIdentitySource(store),
                environment_name=reference.environment_name,
            ),
        ).cancel(reference.run_id)

    def recover(self, reference: RunReference):
        check_runtime("recover")
        if reference.environment_name is None:
            raise ValueError("missing controller environment")
        store = _bound_store(reference)
        controller = _controller(reference)
        return RecoveryService(
            store,
            controller,
            ModalChildObserver(
                S3ChildIdentitySource(store),
                environment_name=reference.environment_name,
            ),
            SubmissionService(store, controller, ReceiptStore()),
        ).recover(reference.run_id)

    def artifacts(self, reference: RunReference, output: Path):
        return ArtifactPullService(_bound_store(reference)).pull(
            reference.run_id, output
        )

    def verify(self, reference: RunReference):
        return ArtifactVerificationService(_bound_store(reference)).verify(
            reference.run_id
        )


def wait_for_run(engine, reference: RunReference, *, interval: float = 1):
    """Observe independently. Interruption propagates without cancelling compute."""
    while True:
        result = engine.result(reference)
        if result.state in {"terminal", "conflict"} or getattr(
            result, "admission_state", None
        ) in {"failed", "cancelled"}:
            return result
        time.sleep(interval)
