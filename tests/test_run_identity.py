from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from typer.testing import CliRunner

from tetrabench.authoring import initialize_project
from tetrabench.cli import app
from tetrabench.controller import FakeDetachedController
from tetrabench.engines.modal import ModalEngine
from tetrabench.lifecycle import (
    CancellationService,
    FakeChildCleanupObserver,
    StatusService,
)
from tetrabench.local_execution import run_prepared_local
from tetrabench.plan import canonical_model_bytes
from tetrabench.receipts import ReceiptStore, RunIdentityConflictError
from tetrabench.remote import RemoteResultService
from tetrabench.run_reference import RunReference, RunReferenceStore, process_identity
from tetrabench.submission import SubmissionService, prepare_run


def _docker_reference(root, run_id="run-1"):
    return RunReference(
        run_id=run_id,
        engine="docker",
        request_sha256="d" * 64,
        output_directory=str(root / "output"),
        process=process_identity(os.getpid()),
    )


def _modal_reference(prepared):
    from tetrabench.canonical_json import sha256_hex

    launch = prepared.controller_launch
    return RunReference(
        run_id=prepared.request.run_id,
        engine="modal",
        request_sha256=sha256_hex(canonical_model_bytes(prepared.request)),
        storage=prepared.plan.storage,
        app_name=launch.app_name,
        function_name=launch.function_name,
        environment_name=launch.environment_name,
    )


def test_legacy_modal_id_blocks_docker_and_keeps_remote_route(tmp_path, monkeypatch):
    from test_lifecycle import _receipt, _running, _Store

    project = initialize_project(tmp_path / "project")
    monkeypatch.chdir(project)
    receipts = ReceiptStore()
    receipt = _receipt("fc-owner")
    receipts.write(receipt)
    before = receipts.path_for(receipt.run_id).read_bytes()
    output = tmp_path / "output"
    monkeypatch.setattr(
        "tetrabench.local_execution.HarborRunner",
        lambda: pytest.fail("constructed native runner for occupied ID"),
    )
    monkeypatch.setattr(
        "tetrabench.engines.modal.create_s3_store",
        lambda *a: pytest.fail("constructed provider for rejected local run"),
    )
    invocation = CliRunner().invoke(
        app,
        [
            "run",
            "example",
            "--engine",
            "docker",
            "--run-id",
            receipt.run_id,
            "--output",
            str(output),
            "--json",
        ],
    )
    assert invocation.exit_code == 2, invocation.stderr
    assert "Modal receipt" in invocation.stderr
    assert not output.exists()
    assert RunReferenceStore().read(receipt.run_id) is None
    assert receipts.path_for(receipt.run_id).read_bytes() == before

    class Controller(FakeDetachedController):
        def cancel(self, call_id):
            super().cancel(call_id)
            self.set_state(call_id, "failed")

    class Store(_Store):
        def read_content(self, descriptor):
            pytest.fail("nonterminal lookup must not read artifacts")

        def discover_runs(self):
            pytest.fail("single-run lookup must not list runs")

    store = Store(_running())
    controller = Controller()
    controller.set_state("fc-owner", "running")
    monkeypatch.setattr(
        "tetrabench.cli._status_service",
        lambda profile: StatusService(store, receipts, controller),
    )
    monkeypatch.setattr(
        "tetrabench.cli._remote_result_service",
        lambda profile: RemoteResultService(store),
    )
    monkeypatch.setattr(
        "tetrabench.cli._cancellation_service",
        lambda profile, **kwargs: CancellationService(
            store,
            controller,
            FakeChildCleanupObserver(),
            delay_seconds=0,
        ),
    )
    for operation in ("status", "result"):
        result = CliRunner().invoke(
            app, [operation, receipt.run_id, "--profile", "legacy", "--json"]
        )
        assert result.exit_code == 0, result.stderr
    cancelled = CliRunner().invoke(
        app,
        [
            "cancel",
            receipt.run_id,
            "--profile",
            "legacy",
            "--environment",
            "original-env",
            "--yes",
            "--json",
        ],
    )
    assert cancelled.exit_code == 0, cancelled.stderr
    assert controller.cancelled == ["fc-owner"]
    assert RunReferenceStore().read(receipt.run_id) is None
    assert receipts.path_for(receipt.run_id).read_bytes() == before


def test_receipt_arriving_during_validation_rejects_before_output_reservation(
    tmp_path, monkeypatch
):
    from test_receipts import _receipt

    project = initialize_project(tmp_path / "project")
    prepared = prepare_run(project, "example", run_id="run-1")
    references = RunReferenceStore()

    class Runner:
        def validate_tasks(self, *args):
            references.receipts.write(_receipt())

        def run(self, *args, **kwargs):
            pytest.fail("launched after concurrent receipt admission")

    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)
    with pytest.raises(RunIdentityConflictError):
        run_prepared_local(prepared, tmp_path / "output", references=references)
    assert not (tmp_path / "output").exists()
    assert references.read("run-1") is None
    assert references.receipts.read("run-1") == _receipt()


@pytest.mark.parametrize("winner", ["receipt", "reference"])
def test_receipts_and_references_share_admission_lock_in_both_orders(tmp_path, winner):
    from test_receipts import _receipt

    references = RunReferenceStore(tmp_path / "run-references")
    receipts = ReceiptStore(tmp_path / "receipts")
    reference = _docker_reference(tmp_path)
    published = threading.Event()
    release = threading.Event()
    attempted = threading.Event()

    def first():
        with receipts.lock("run-1"):
            if winner == "receipt":
                receipts.write(_receipt())
            else:
                references.create(reference)
            published.set()
            assert release.wait(5)

    def second():
        attempted.set()
        if winner == "receipt":
            references.create(reference)
        else:
            receipts.write(_receipt())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first)
        assert published.wait(5)
        second_future = pool.submit(second)
        try:
            assert attempted.wait(5)
            assert not second_future.done()
        finally:
            release.set()
        first_future.result(timeout=5)
        with pytest.raises(RunIdentityConflictError):
            second_future.result(timeout=5)
    assert (references.read("run-1") is not None) == (winner == "reference")
    assert (receipts.read("run-1") is not None) == (winner == "receipt")


def test_nested_incompatible_admission_cannot_bypass_reentrant_lock(tmp_path):
    from test_receipts import _receipt

    references = RunReferenceStore(tmp_path / "run-references")
    reference = _docker_reference(tmp_path)
    with references.admission(
        "run-1",
        engine="docker",
        request_sha256=reference.request_sha256,
        require_new=True,
    ):
        with pytest.raises(RunIdentityConflictError):
            references.receipts.write(_receipt())
        references.create(reference)
    references.create(reference)
    assert references.read("run-1") == reference
    assert references.receipts.read("run-1") is None


def test_matching_modal_receipt_and_reference_can_be_written_concurrently(tmp_path):
    from test_receipts import _receipt
    from test_submission import _prepared

    prepared = _prepared()
    receipt = _receipt()
    reference = _modal_reference(prepared).model_copy(
        update={"request_sha256": receipt.request_sha256}
    )
    references = RunReferenceStore(tmp_path / "run-references")
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipt_write = pool.submit(references.receipts.write, receipt)
        reference_write = pool.submit(references.create, reference)
        receipt_write.result(timeout=5)
        reference_write.result(timeout=5)
    assert references.read("run-1") == reference
    assert references.receipts.read("run-1") == receipt


def test_custom_layout_uses_explicitly_paired_stores(tmp_path):
    from test_receipts import _receipt

    receipts = ReceiptStore(
        tmp_path / "old-cache", reference_root=tmp_path / "bindings"
    )
    references = RunReferenceStore(tmp_path / "bindings", receipts=receipts)
    references.create(_docker_reference(tmp_path))
    with pytest.raises(RunIdentityConflictError):
        receipts.write(_receipt())
    with pytest.raises(ValueError, match="different namespaces"):
        RunReferenceStore(tmp_path / "other-bindings", receipts=receipts)


def test_matching_legacy_receipt_and_reference_preserve_submit_and_recovery(tmp_path):
    from test_submission import _MemorySubmissionStore, _prepared

    prepared = _prepared()
    controller = FakeDetachedController()
    store = _MemorySubmissionStore()
    receipts = ReceiptStore(tmp_path / "receipts")
    service = SubmissionService(store, controller, receipts)
    receipt = service._record_intent(prepared.request, recovery=False)
    references = RunReferenceStore(tmp_path / "run-references", receipts=receipts)
    assert references.read(receipt.run_id) is None
    submitted = service.submit(prepared)
    reference = references.read(receipt.run_id)
    assert reference == _modal_reference(prepared)
    assert reference is not None
    references.create(reference)
    repeated = service.submit(prepared)
    recovered = service.recover(prepared)
    service.recover_request(prepared.request)
    assert len(controller.spawned) == 4
    assert len(submitted.attempts) < len(repeated.attempts) < len(recovered.attempts)
    assert references.read(receipt.run_id) == reference


def test_modal_conflicting_reference_refuses_before_provider_and_recovery_spawn(
    tmp_path, monkeypatch
):
    from test_submission import _MemorySubmissionStore, _prepared

    prepared = _prepared()
    references = RunReferenceStore()
    references.create(_docker_reference(tmp_path))
    monkeypatch.setattr(
        "tetrabench.engines.modal.create_s3_store",
        lambda *a: pytest.fail("provider constructed before ID admission"),
    )
    with pytest.raises(RunIdentityConflictError):
        ModalEngine().launch(prepared, None)
    store = _MemorySubmissionStore()
    controller = FakeDetachedController()
    service = SubmissionService(store, controller, references.receipts)
    with pytest.raises(RunIdentityConflictError):
        service.recover_request(prepared.request)
    assert store.operations == []
    assert controller.spawned == []


def test_explicit_recovery_tolerates_malformed_reference_without_overwriting_it(
    tmp_path,
):
    from test_submission import _MemorySubmissionStore, _prepared

    from tetrabench.records import new_admission

    prepared = _prepared()
    references = RunReferenceStore(tmp_path / "run-references")
    references.root.mkdir()
    reference_path = references.root / "run-1.json"
    reference_path.write_bytes(b"malformed routing hint")
    store = _MemorySubmissionStore()
    store.create_admission(
        new_admission(prepared.request, timestamp="2026-09-06T00:00:00Z")
    )
    controller = FakeDetachedController()
    service = SubmissionService(store, controller, references.receipts)
    assert service.recover_request(prepared.request) == "fc-1"
    assert reference_path.read_bytes() == b"malformed routing hint"
    assert references.receipts.read("run-1") is not None
