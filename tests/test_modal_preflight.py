from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_integrity import _run

from tetrabench.controller import FakeDetachedController
from tetrabench.diagnostics import PreflightError
from tetrabench.engines import modal
from tetrabench.preflight import check_runtime
from tetrabench.run_reference import RunReference


@pytest.mark.parametrize(
    "operation", ["launch", "cancel", "recover", "legacy_cancel", "legacy_recover"]
)
def test_mutations_reject_unsupported_python_before_constructing_providers(
    monkeypatch, operation
):
    def unsupported(name):
        return check_runtime(
            name,
            python_version=(3, 13, 0),
            platform_name="linux",
            implementation="cpython",
        )

    def unexpected(*_args, **_kwargs):
        pytest.fail("constructed provider or mutation resources before preflight")

    monkeypatch.setattr(modal, "check_runtime", unsupported)
    for name in (
        "_bound_store",
        "_store",
        "_controller",
        "create_s3_store",
        "ModalControllerClient",
        "RunReferenceStore",
        "load_project_config",
        "RecoveryService",
        "SubmissionService",
    ):
        monkeypatch.setattr(modal, name, unexpected)
    with pytest.raises(PreflightError) as caught:
        if operation == "legacy_cancel":
            modal.legacy_cancellation_service(
                None, run_id="run-1", environment_name="original"
            )
        elif operation == "legacy_recover":
            modal.legacy_recovery_service(
                None, run_id="run-1", environment_name="original"
            )
        else:
            method = getattr(modal.ModalEngine(), operation)
            method(None, None) if operation == "launch" else method(None)
    assert caught.value.code == "unsupported_python"
    assert caught.value.operation == (
        "run" if operation == "launch" else operation.removeprefix("legacy_")
    )


@pytest.mark.parametrize(
    "operation",
    ["result", "status", "verify", "legacy_result", "legacy_status", "legacy_verify"],
)
def test_read_only_inspection_does_not_invoke_execution_runtime_guard(
    monkeypatch, tmp_path, operation
):
    store, _client, request, terminal = _run()
    reference = RunReference(
        run_id="run-1",
        engine="modal",
        request_sha256=terminal.request_sha256,
        storage=store.storage,
        app_name="tetrabench",
        function_name="controller",
        environment_name="original",
    )
    assert request.plan.controller.kind == "modal"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(
        modal, "check_runtime", lambda *_: pytest.fail("read-only runtime guard")
    )
    monkeypatch.setattr(modal, "_store", lambda _: store)
    monkeypatch.setattr(modal, "create_s3_store", lambda _: store)
    monkeypatch.setattr(modal, "_controller", lambda _: FakeDetachedController())
    monkeypatch.setattr(
        modal, "ModalControllerClient", lambda *_, **__: FakeDetachedController()
    )
    monkeypatch.setattr(
        modal,
        "load_project_config",
        lambda *_, **__: SimpleNamespace(storage=store.storage),
    )
    if operation == "legacy_result":
        report = modal.legacy_result_service(None).result("run-1")
    elif operation == "legacy_status":
        report = modal.legacy_status_service(None).status("run-1")
    elif operation == "legacy_verify":
        report = modal.legacy_verification_service(None).verify("run-1")
    else:
        report = getattr(modal.ModalEngine(), operation)(reference)
    assert report.state == ("verified" if operation.endswith("verify") else "terminal")
