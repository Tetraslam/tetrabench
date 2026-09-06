from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tetrabench import docker_lifecycle
from tetrabench.authoring import initialize_project
from tetrabench.canonical_json import dumps_canonical_json, sha256_hex
from tetrabench.cli import app
from tetrabench.engines import docker as engine_module
from tetrabench.engines.docker import DockerEngine
from tetrabench.local_control import initialize_owner, read_owner_control
from tetrabench.plan import canonical_model_bytes
from tetrabench.run_reference import (
    RunReference,
    RunReferenceStore,
    process_identity,
    write_private_record,
)
from tetrabench.submission import prepare_run


def _output(tmp_path):
    project = initialize_project(tmp_path / "project")
    prepared = prepare_run(project, "example", run_id="cancel-run")
    output = tmp_path / "out"
    output.mkdir(mode=0o700)
    request = canonical_model_bytes(prepared.request)
    write_private_record(output / "request.json", request)
    write_private_record(
        output / "execution.json",
        dumps_canonical_json(
            {
                "schema_version": 1,
                "run_id": prepared.request.run_id,
                "request_sha256": sha256_hex(request),
                "state": "running",
            }
        ),
    )
    return output, sha256_hex(request)


def _reference(output: Path, digest: str, pid: int):
    metadata = output.stat()
    return RunReference(
        run_id="cancel-run",
        engine="docker",
        request_sha256=digest,
        output_directory=str(output),
        output_identity=(metadata.st_dev, metadata.st_ino),
        process=process_identity(pid),
    )


@pytest.mark.parametrize(
    "ordering", ["ctrl-c", "external", "ctrl-c-under-lock", "shutdown"]
)
def test_owner_boundary_serializes_ctrl_c_and_external_cancel(
    tmp_path, monkeypatch, ordering
):
    project = initialize_project(tmp_path / "project")
    output = tmp_path / "out"
    script = """
import asyncio, pathlib, sys
from contextlib import nullcontext
from tetrabench.harbor_api import Harbor022Api
from tetrabench.local_execution import run_prepared_local
from tetrabench.receipts import ReceiptStore
from tetrabench.submission import prepare_run
project, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
async def cleanup():
    (root / 'cleanup-started').touch()
    while not (root / 'release-cleanup').exists():
        await asyncio.sleep(.01)
    (root / 'cleanup-finished').touch()
async def job(config):
    task = asyncio.current_task()
    if sys.argv[3] == 'shutdown':
        async def background():
            own_task = asyncio.current_task()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.shield(cleanup())
                (root / 'cancel-count').write_text(str(own_task.cancelling()))
        asyncio.create_task(background())
        await asyncio.sleep(0)
        print('ready', flush=True)
        return None
    lock = (
        ReceiptStore(root).lock('cancel')
        if sys.argv[3] == 'ctrl-c-under-lock' else nullcontext()
    )
    with lock:
        print('ready', flush=True)
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.shield(cleanup())
            (root / 'cancel-count').write_text(str(task.cancelling()))
Harbor022Api._execute = staticmethod(job)
try:
    run_prepared_local(prepare_run(project, 'example', run_id='cancel-run'), root)
except (KeyboardInterrupt, FileNotFoundError):
    pass
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(project), str(output), ordering],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    monkeypatch.setattr(
        "tetrabench.run_reference.send_process_signal",
        lambda *a: pytest.fail("external cancel sent a signal"),
    )
    monkeypatch.setattr(
        engine_module,
        "cleanup_containers",
        lambda path, **kwargs: docker_lifecycle.owner_active(path) is False,
    )
    try:
        assert child.stdout is not None and child.stdout.readline() == "ready\n"
        control = read_owner_control(output)
        assert control is not None
        reference = control.reference
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = None
            if ordering == "external":
                first = pool.submit(DockerEngine().cancel, reference)
            else:
                child.send_signal(signal.SIGINT)
            deadline = time.monotonic() + 5
            while (
                not (output / "cleanup-started").exists()
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert (output / "cleanup-started").exists()
            # The opposite source, and another Ctrl-C, must not cancel the task again.
            child.send_signal(signal.SIGINT)
            if first is None:
                first = pool.submit(DockerEngine().cancel, reference)
            second = pool.submit(DockerEngine().cancel, reference)
            assert child.poll() is None
            assert not (output / "cleanup-finished").exists()
            (output / "release-cleanup").touch()
            assert first.result(timeout=10).cleanup_complete
            assert second.result(timeout=10).cleanup_complete
        assert child.wait(timeout=5) == 0
        assert (output / "cleanup-finished").exists()
        assert (output / "cancel-count").read_text() == "1"
    finally:
        (output / "release-cleanup").touch()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_request_publication_failure_is_idempotent_without_signals(
    tmp_path, monkeypatch
):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    attempts = []

    publish = engine_module.publish_cancellation

    def failed_publish(ref):
        attempts.append(ref)
        publish(ref)
        raise OSError("injected publication boundary failure")

    monkeypatch.setattr(engine_module, "publish_cancellation", failed_publish)
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
        with pytest.raises(OSError, match="publication boundary"):
            DockerEngine().cancel(reference)
        assert (output / "cancel.json").is_file()
        report = DockerEngine().cancel(reference)
    assert len(attempts) == 1
    assert report.state == "cancelling" and not report.cleanup_complete


def _record_owner(output, lock_identity):
    reference = _reference(output, "a" * 64, os.getpid())
    initialize_owner(reference, lock_identity)


class DockerInventory:
    def __init__(self, output):
        self.containers = {
            "a" * 64: output / "context/benchmarks/tasks/example/task/environment",
            "b" * 64: output / "context/benchmarks/tasks/example/task/tests",
            "c" * 64: output / "context-other/task",
            "d" * 64: output / "context/../../unrelated",
        }
        self.removed = []
        self.daemon = "original-daemon"

    def __call__(self, *args):
        if args[0] == "info":
            return self.daemon
        if args[0] == "ps":
            return "\n".join(self.containers)
        if args[0] == "inspect":
            return json.dumps(
                {
                    "com.docker.compose.project.working_dir": str(
                        self.containers[args[-1]]
                    ),
                    "com.docker.compose.project": "native-project",
                    "com.docker.compose.service": "main",
                }
            )
        assert args[:2] == ("rm", "--force")
        self.removed.append(args[-1])
        del self.containers[args[-1]]
        return ""


def test_cleanup_proves_exact_owned_native_containers_and_preserves_others(
    tmp_path, monkeypatch
):
    output = tmp_path / "out"
    output.mkdir()
    inventory = DockerInventory(output)
    monkeypatch.setattr(docker_lifecycle, "_docker", inventory)
    docker_lifecycle.bind_docker(output)
    with docker_lifecycle.execution_owner(output) as lock_identity:
        _record_owner(output, lock_identity)
        assert not docker_lifecycle.cleanup_containers(output, remove=True)
        assert inventory.removed == []
    assert not docker_lifecycle.observe_cleanup(output)
    assert inventory.removed == []
    assert docker_lifecycle.cleanup_containers(output, remove=True)
    assert inventory.removed == ["a" * 64, "b" * 64]
    assert set(inventory.containers) == {"c" * 64, "d" * 64}


def test_wrong_daemon_or_missing_binding_never_proves_cleanup(tmp_path, monkeypatch):
    inventory = DockerInventory(tmp_path)
    monkeypatch.setattr(docker_lifecycle, "_docker", inventory)
    with docker_lifecycle.execution_owner(tmp_path) as lock_identity:
        _record_owner(tmp_path, lock_identity)
    assert not docker_lifecycle.cleanup_containers(tmp_path, remove=True)
    docker_lifecycle.bind_docker(tmp_path)
    inventory.daemon = "different-daemon"
    assert not docker_lifecycle.observe_cleanup(tmp_path)
    with pytest.raises(ValueError, match="daemon changed"):
        docker_lifecycle.cleanup_containers(tmp_path, remove=True)
    assert inventory.removed == []


def test_orphaned_compose_client_blocks_cleanup_proof_and_container_mutation(
    tmp_path, monkeypatch
):
    inventory = DockerInventory(tmp_path)
    monkeypatch.setattr(docker_lifecycle, "_docker", inventory)
    docker_lifecycle.bind_docker(tmp_path)
    with docker_lifecycle.execution_owner(tmp_path) as lock_identity:
        _record_owner(tmp_path, lock_identity)
    monkeypatch.setattr(docker_lifecycle, "_compose_client_active", lambda _: True)
    assert not docker_lifecycle.cleanup_containers(tmp_path, remove=True)
    assert inventory.removed == []


def test_output_symlink_ancestor_is_rejected_before_allocation(tmp_path, monkeypatch):
    project = initialize_project(tmp_path / "project")
    real_parent = tmp_path / "real" / "nested"
    real_parent.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent.parent, target_is_directory=True)
    output = alias / "nested" / "run"
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "tetrabench.local_execution.HarborRunner",
        lambda: pytest.fail("constructed Harbor before output rejection"),
    )
    result = CliRunner().invoke(
        app, ["run", "example", "--output", str(output), "--json"]
    )
    assert result.exit_code == 2, result.stderr
    assert "symlink ancestors" in result.stderr
    assert not (real_parent / "run").exists()
    assert RunReferenceStore().list() == ()


def test_cleanup_rejects_alias_of_recorded_real_output_before_docker(
    tmp_path, monkeypatch
):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    aliased_output = alias / "out"
    monkeypatch.setattr(
        docker_lifecycle,
        "_docker",
        lambda *a: pytest.fail("Docker accessed through ambiguous output spelling"),
    )
    with pytest.raises(ValueError, match="symlink ancestor"):
        docker_lifecycle.cleanup_containers(aliased_output, remove=True)
    with pytest.raises(ValueError):
        DockerEngine().cancel(
            reference.model_copy(update={"output_directory": str(aliased_output)})
        )


@pytest.mark.parametrize("native", ["truncated", "missing"])
def test_stopped_owner_native_corruption_does_not_block_orphan_cleanup(
    tmp_path, monkeypatch, native
):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    RunReferenceStore().create(reference)
    inventory = DockerInventory(output)
    monkeypatch.setattr(docker_lifecycle, "_docker", inventory)
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
        docker_lifecycle.bind_docker(output)
    if native == "truncated":
        job = output / "harbor-job"
        job.mkdir()
        (job / "result.json").write_bytes(b'{"finished_at":')
    result = CliRunner().invoke(app, ["cancel", reference.run_id, "--yes", "--json"])
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["cleanup_complete"] is True
    assert report["outcome"] is None
    assert report["result_error"] is not None
    assert "native result" in report["result_error"]
    assert inventory.removed == ["a" * 64, "b" * 64]
    assert set(inventory.containers) == {"c" * 64, "d" * 64}


def test_replaced_execution_lock_cannot_authorize_cleanup(tmp_path, monkeypatch):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
    (output / "execution.lock").rename(output / "original.lock")
    (output / "execution.lock").touch()
    monkeypatch.setattr(
        docker_lifecycle,
        "_docker",
        lambda *a: pytest.fail("Docker mutation after lock replacement"),
    )
    with pytest.raises(ValueError, match="lock identity changed"):
        DockerEngine().cancel(reference)


def test_cleanup_holds_owner_lock_and_rechecks_output_identity(tmp_path, monkeypatch):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    inventory = DockerInventory(output)
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
    monkeypatch.setattr(docker_lifecycle, "_docker", inventory)
    docker_lifecycle.bind_docker(output)
    scanned = docker_lifecycle._owned_containers

    def replace_after_inventory(path, binding):
        assert docker_lifecycle.owner_active(path) is True
        owned = scanned(path, binding)
        output.rename(tmp_path / "moved")
        output.mkdir()
        return owned

    monkeypatch.setattr(docker_lifecycle, "_owned_containers", replace_after_inventory)
    with pytest.raises(ValueError, match="output identity changed"):
        docker_lifecycle.cleanup_containers(
            output, remove=True, identity=reference.output_identity
        )
    assert inventory.removed == []


def test_missing_owner_stop_witness_allows_reclamation_but_not_cleanup_claim(
    tmp_path, monkeypatch
):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    inventory = DockerInventory(output)
    monkeypatch.setattr(docker_lifecycle, "_docker", inventory)
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
        docker_lifecycle.bind_docker(output)
    (output / "owner-stopped.json").unlink()
    assert not docker_lifecycle.cleanup_containers(output, remove=True)
    assert inventory.removed == ["a" * 64, "b" * 64]


def test_queued_cancellation_prevents_native_work_and_restores_signal_handler(tmp_path):
    from tetrabench.local_control import OwnerCancellation, publish_cancellation

    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    invoked = []

    async def native_work():
        invoked.append(True)

    previous = signal.getsignal(signal.SIGINT)
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
        publish_cancellation(reference)
        control = read_owner_control(output)
        assert control is not None
        with pytest.raises(KeyboardInterrupt):
            OwnerCancellation(control).run(native_work)
    assert invoked == []
    assert signal.getsignal(signal.SIGINT) == previous


def test_disappearing_control_records_do_not_become_equal_completion_proof(
    tmp_path, monkeypatch
):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    inventory = DockerInventory(output)
    monkeypatch.setattr(docker_lifecycle, "_docker", inventory)
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
        docker_lifecycle.bind_docker(output)
    read = docker_lifecycle.read_owner_control

    def disappear(path):
        control = read(path)
        (path / "owner-control.json").unlink(missing_ok=True)
        (path / "owner-stopped.json").unlink(missing_ok=True)
        return control

    monkeypatch.setattr(docker_lifecycle, "read_owner_control", disappear)
    assert not docker_lifecycle.cleanup_containers(output, remove=True)
    assert inventory.removed == ["a" * 64, "b" * 64]


@pytest.mark.parametrize("active", [True, False])
def test_owner_exit_during_pidfd_open_is_reconciled_only_after_lock_release(
    tmp_path, monkeypatch, active
):
    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    with docker_lifecycle.execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)

    def exited(_pid):
        raise ProcessLookupError

    monkeypatch.setattr(engine_module, "open_process_handle", exited)
    monkeypatch.setattr(engine_module, "owner_active", lambda _: active)
    if active:
        with pytest.raises(ProcessLookupError):
            engine_module._queue_cancellation(reference, output)
    else:
        engine_module._queue_cancellation(reference, output)
    assert not (output / "cancel.json").exists()
