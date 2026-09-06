from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from tetrabench import authoring
from tetrabench.canonical_json import loads_canonical_json, sha256_hex
from tetrabench.catalog import get_section, load_catalog
from tetrabench.cli import app
from tetrabench.config import load_project_config
from tetrabench.controller_runtime import HarborRunResult
from tetrabench.engines import Capabilities, get_engine, register_engine
from tetrabench.engines.docker import DockerEngine, LocalReport
from tetrabench.engines.modal import ModalEngine, wait_for_run
from tetrabench.local_execution import run_prepared_local
from tetrabench.models import ConfigOverrides, EnginePatch
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.receipts import ReceiptConflictError, ReceiptStore, SubmissionReceipt
from tetrabench.records import RequestRecord
from tetrabench.rewards import SectionRewardSummary, TaskRewardSummary, TrialReward
from tetrabench.run_reference import RunReference, RunReferenceStore, process_identity
from tetrabench.submission import SubmissionService, prepare_run, prepare_submission

runner = CliRunner()


def _project(tmp_path: Path, section: str = "third-domain") -> Path:
    root = tmp_path / "project"
    authoring.initialize_project(root, section)
    return root


def _summary(request) -> SectionRewardSummary:
    task_ids = sorted(trial.task_id for trial in request.plan.trials)
    return SectionRewardSummary(
        policy="binary",
        aggregate_kind="binary_pass_rate",
        aggregate="1",
        task_count=len(task_ids),
        sample_count=len(task_ids),
        pass_count=len(task_ids),
        tasks=tuple(
            TaskRewardSummary(
                task_id=task,
                policy="binary",
                sample_count=1,
                pass_count=1,
                aggregate="1",
            )
            for task in task_ids
        ),
        trials=tuple(
            TrialReward(task_id=task, trial_name=task, policy="binary", value="1")
            for task in task_ids
        ),
    )


class SuccessfulRunner:
    def validate_tasks(self, request, context):
        for task in request.plan.trials:
            authoring.Harbor022Api.validate_task(path=context / task.harbor_task)

    def run(self, request, paths, **kwargs):
        job = paths.jobs / "harbor-job"
        job.mkdir()
        return HarborRunResult(
            outcome="succeeded",
            reward="1",
            summary=_summary(request),
            job_directory=job,
        )


def test_neutral_default_and_arbitrary_category_end_to_end(tmp_path, monkeypatch):
    neutral = tmp_path / "neutral"
    result = runner.invoke(app, ["init", str(neutral)])
    assert result.exit_code == 0, result.stderr
    assert set(load_catalog(neutral, "benchmarks/catalog.toml").sections) == {"example"}
    root = tmp_path / "custom"
    assert (
        runner.invoke(app, ["init", str(root), "--section", "data-quality"]).exit_code
        == 0
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", SuccessfulRunner)
    commands = [
        ["task", "new", "data-quality", "second"],
        ["task", "validate", "benchmarks/tasks/data-quality/second"],
        [
            "task",
            "add",
            "data-quality",
            "second",
            "benchmarks/tasks/data-quality/second",
        ],
        ["doctor"],
        ["sections"],
        ["plan", "data-quality", "--engine", "docker"],
        [
            "run",
            "data-quality",
            "--engine",
            "docker",
            "--run-id",
            "my-local-run",
            "--json",
        ],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, (command, result.stderr, result.exception)
    report = loads_canonical_json(result.stdout.strip().encode())
    assert isinstance(report, dict)
    assert report["run_id"] == "my-local-run"
    reference = RunReferenceStore().read("my-local-run")
    assert reference is not None and reference.engine == "docker"
    assert reference.output_directory == str(root / "my-local-run")
    assert (root / "my-local-run").stat().st_mode & 0o777 == 0o700
    assert (root / "my-local-run/request.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "initial",
    [
        "tasks = [] # keep empty-array comment\n",
        'tasks = [{ id = "old", harbor_task = "tasks/old", '
        'reward_policy = "binary" }] # keep inline\n',
    ],
)
def test_add_handles_inline_arrays_preserving_comments_and_data(tmp_path, initial):
    root = _project(tmp_path)
    catalog = root / "benchmarks/catalog.toml"
    catalog.write_text(
        "schema_version = 1\n# user header\n[sections.third-domain]\n"
        'description = "retained description"\nreadme = "third-domain/README.md"\n'
        + initial
    )
    fixture = "benchmarks/tasks/third-domain/hello-tetrabench"
    authoring.add_task(root, "third-domain", "added", fixture)
    text = catalog.read_text()
    assert "# user header" in text
    assert initial.split(" # ")[1].strip() in text
    section = get_section(load_catalog(root, "benchmarks/catalog.toml"), "third-domain")
    assert section.description == "retained description"
    assert section.tasks[-1].id == "added"
    if 'id = "old"' in initial:
        assert section.tasks[0].harbor_task == "tasks/old"


def test_engine_precedence_and_backend_switch_clears_settings(tmp_path):
    root = _project(tmp_path)
    (root / "tetrabench.toml").write_text(
        'schema_version=1\n[engine]\nkind="modal"\n'
        '[engine.settings]\napp_name="project-app"\n'
    )
    user = tmp_path / "user.toml"
    user.write_text(
        'schema_version=1\n[profiles.cloud.engine.settings]\nfunction_name="profile-function"\n'
        '[profiles.local.engine]\nkind="docker"\n'
    )
    config = load_project_config(root, profile="cloud", user_path=user)
    assert config.controller.kind == "modal"
    assert config.controller.app_name == "project-app"
    assert config.controller.function_name == "profile-function"
    local = load_project_config(
        root,
        profile="cloud",
        user_path=user,
        overrides=ConfigOverrides(engine=EnginePatch(kind="docker")),
    )
    assert local.engine is not None and local.engine.settings == {}
    assert (local.controller.kind, local.execution.kind) == ("local", "docker")
    modal = load_project_config(
        root,
        profile="local",
        user_path=user,
        overrides=ConfigOverrides(engine=EnginePatch(kind="modal")),
    )
    assert modal.engine is not None and modal.engine.settings == {}
    assert modal.controller.kind == "modal"
    assert modal.controller.app_name == "tetrabench"
    with user.open("a") as stream:
        stream.write(
            '\n[profiles.legacy.controller]\nfunction_name="legacy-function"\n'
        )
    legacy = load_project_config(root, profile="legacy", user_path=user)
    assert legacy.controller.kind == "modal"
    assert legacy.controller.app_name == "project-app"
    assert legacy.controller.function_name == "legacy-function"


@pytest.mark.parametrize(
    "settings",
    [
        '[engine]\nkind="docker"\n[engine.settings]\napp_name="wrong"\n',
        '[engine]\nkind="modal"\n[engine.settings]\npassword="secret"\n',
        '[engine]\nkind="unknown-provider"\n',
        '[engine]\nkind="docker"\n[execution]\nkind="docker"\n',
    ],
)
def test_invalid_engine_config_fails_before_provider_access(tmp_path, settings):
    root = _project(tmp_path)
    (root / "tetrabench.toml").write_text("schema_version=1\n" + settings)
    with pytest.raises(ValueError):
        load_project_config(root)


@pytest.mark.parametrize(
    "options",
    [
        ["--detach"],
        ["--wait", "--detach"],
        ["--engine", "modal", "--output", "unused"],
    ],
)
def test_capability_refusal_precedes_sealing_allocation_or_provider(
    tmp_path, monkeypatch, options
):
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    before = set(root.iterdir())
    monkeypatch.setattr(
        "tetrabench.submission.seal_context",
        lambda *a, **k: pytest.fail("sealed after refusal"),
    )
    monkeypatch.setattr(
        "tetrabench.engines.modal.create_s3_store",
        lambda *a: pytest.fail("provider constructed"),
    )
    result = runner.invoke(app, ["run", "third-domain", *options, "--json"])
    assert result.exit_code == 2, result.stdout
    assert set(root.iterdir()) == before
    assert RunReferenceStore().list() == ()


def test_third_engine_requires_only_registry_registration(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    calls = []

    class DummyEngine(DockerEngine):
        kind = "dummy-test"
        capabilities = Capabilities(cancel=False, artifacts=False)

        def launch(self, prepared, output):
            calls.append((prepared, output))
            return LocalReport(
                run_id=prepared.request.run_id,
                state="terminal",
                outcome="succeeded",
                job_directory="dummy",
            )

    get_engine("docker")
    register_engine(DummyEngine())
    try:
        result = runner.invoke(
            app, ["run", "third-domain", "--engine", "dummy-test", "--json"]
        )
        assert result.exit_code == 0, result.stderr
        assert len(calls) == 1
        assert calls[0][0].engine_kind == "dummy-test"
        assert calls[0][0].sealed_context.files
    finally:
        from tetrabench.engines import _ENGINES

        _ENGINES.pop("dummy-test")


def test_local_and_remote_share_exact_sealed_bytes_after_source_mutation(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    local = prepare_run(root, "third-domain", run_id="local-fixture")
    with (root / "tetrabench.toml").open("a") as stream:
        stream.write(
            '\n[storage]\nprovider="aws"\nbucket="private-fixtures"\nregion="us-east-1"\n'
        )
    remote = prepare_submission(
        root,
        "third-domain",
        run_id="remote-fixture",
        overrides=ConfigOverrides(engine=EnginePatch(kind="modal")),
    )
    assert local.sealed_context == remote.sealed_context
    source = root / "benchmarks/tasks/third-domain/hello-tetrabench/instruction.md"
    old = source.read_bytes()
    source.write_bytes(b"source was mutated after sealing")
    observed = []

    class Runner(SuccessfulRunner):
        def validate_tasks(self, request, context):
            observed.append(
                (
                    context / request.plan.trials[0].harbor_task / "instruction.md"
                ).read_bytes()
            )
            super().validate_tasks(request, context)

        def run(self, request, paths, **kwargs):
            assert paths.context != root
            observed.append(
                (
                    paths.context
                    / request.plan.trials[0].harbor_task
                    / "instruction.md"
                ).read_bytes()
            )
            return super().run(request, paths, **kwargs)

    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)
    run_prepared_local(local, tmp_path / "out")
    assert observed == [old, old]
    # Remote materialization consumes request descriptors and immutable store bytes.
    from tetrabench.controller_runtime import ControllerRuntime
    from tetrabench.local_execution import local_paths

    contents = {
        item.descriptor.sha256: item.content for item in remote.sealed_context.files
    }
    context = tmp_path / "remote-context"
    paths = replace(
        local_paths(tmp_path / "remote-attempt"),
        context=context,
        jobs=tmp_path / "jobs",
    )
    owner = SimpleNamespace(
        _store=SimpleNamespace(
            read_content=lambda descriptor: contents[descriptor.sha256]
        )
    )
    ControllerRuntime._materialize(
        cast(ControllerRuntime, owner), remote.request, "attempt", paths
    )
    assert (
        context / remote.plan.trials[0].harbor_task / "instruction.md"
    ).read_bytes() == old


def test_local_failure_remains_addressable_after_project_and_profile_disappear(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    prepared = prepare_run(root, "third-domain", run_id="retained-failure")

    class Runner(SuccessfulRunner):
        def run(self, request, paths, **kwargs):
            raise RuntimeError("injected failure after materialization")

    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)
    with pytest.raises(RuntimeError, match="injected failure"):
        run_prepared_local(prepared, tmp_path / "out")
    (root / "tetrabench.toml").unlink()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "tetrabench.cli.load_project_config",
        lambda *a, **k: pytest.fail("read changed config"),
    )
    result = runner.invoke(
        app, ["status", "retained-failure", "--profile", "gone", "--json"]
    )
    assert result.exit_code == 0, result.stderr
    report = loads_canonical_json(result.stdout.strip().encode())
    assert isinstance(report, dict) and report["state"] == "failed"
    result = runner.invoke(app, ["result", "retained-failure", "--json"])
    assert result.exit_code == 1
    assert (tmp_path / "out").stat().st_mode & 0o777 == 0o700


def _remote_reference(prepared):
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


@pytest.mark.parametrize(
    "operation", ["status", "result", "cancel", "recover", "artifacts"]
)
def test_remote_lifecycle_uses_recorded_binding_not_current_project(
    tmp_path, monkeypatch, operation
):
    root = _project(tmp_path)
    with (root / "tetrabench.toml").open("a") as stream:
        stream.write(
            '\n[storage]\nprovider="aws"\nbucket="original-bucket"\nregion="us-east-1"\n'
        )
    prepared = prepare_submission(
        root,
        "third-domain",
        overrides=ConfigOverrides(engine=EnginePatch(kind="modal")),
    )
    reference = _remote_reference(prepared)
    RunReferenceStore().create(reference)
    (root / "tetrabench.toml").write_text("invalid mutable project")
    monkeypatch.chdir(tmp_path)
    seen = []

    def call(self, recorded, *args):
        seen.append(recorded)
        return LocalReport(
            run_id=recorded.run_id,
            state="terminal",
            outcome="succeeded",
            job_directory="remote",
            cleanup_complete=True,
        )

    monkeypatch.setattr(ModalEngine, operation, call)
    command = (
        ["artifacts", "pull", reference.run_id, str(tmp_path / "pull")]
        if operation == "artifacts"
        else [operation, reference.run_id]
    )
    if operation in {"cancel", "recover"}:
        command.append("--yes")
    result = runner.invoke(app, [*command, "--profile", "renamed", "--json"])
    assert result.exit_code == 0, result.stderr
    assert seen == [reference]
    assert seen[0].storage.bucket == "original-bucket"


def test_remote_wait_interrupt_only_detaches_observer(tmp_path, monkeypatch):
    root = _project(tmp_path)
    with (root / "tetrabench.toml").open("a") as stream:
        stream.write(
            '\n[storage]\nprovider="aws"\nbucket="private"\nregion="us-east-1"\n'
        )
    monkeypatch.chdir(root)
    launched = []

    def launch(self, prepared, output):
        reference = _remote_reference(prepared)
        RunReferenceStore().create(reference)
        launched.append(reference)
        return LocalReport(
            run_id=reference.run_id, state="submitted", job_directory="remote"
        )

    def interrupted(self, reference):
        raise KeyboardInterrupt

    monkeypatch.setattr(ModalEngine, "launch", launch)
    monkeypatch.setattr(ModalEngine, "result", interrupted)
    monkeypatch.setattr(
        ModalEngine, "cancel", lambda *a: pytest.fail("watcher cancelled remote")
    )
    result = runner.invoke(
        app, ["run", "third-domain", "--engine", "modal", "--wait", "--json"]
    )
    assert result.exit_code == 130, result.stderr
    report = loads_canonical_json(result.stderr.strip().encode())
    assert isinstance(report, dict)
    assert report["status"] == "observer_detached"
    assert report["run_id"] == launched[0].run_id


def test_wait_observes_terminal_without_mutating(monkeypatch):
    reports = iter(
        [SimpleNamespace(state="nonterminal"), SimpleNamespace(state="terminal")]
    )
    engine = SimpleNamespace(result=lambda ref: next(reports))
    monkeypatch.setattr("tetrabench.engines.modal.time.sleep", lambda _: None)
    reference = RunReference(run_id="observer", engine="dummy", request_sha256="a" * 64)
    assert wait_for_run(engine, reference).state == "terminal"


def test_legacy_owner_without_cooperative_control_is_not_signalled(
    tmp_path, monkeypatch
):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        identity = process_identity(process.pid)
        reference = RunReference(
            run_id="cancel-test",
            engine="docker",
            request_sha256="a" * 64,
            output_directory=str(tmp_path),
            output_identity=(tmp_path.stat().st_dev, tmp_path.stat().st_ino),
            process=identity,
        )
        report = LocalReport(
            run_id=reference.run_id, state="running", job_directory=str(tmp_path)
        )
        monkeypatch.setattr(DockerEngine, "status", lambda *a: report)
        monkeypatch.setattr("tetrabench.engines.docker._request", lambda *a: None)
        monkeypatch.setattr("tetrabench.engines.docker._state", lambda *a: "running")
        result = DockerEngine().cancel(reference)
        assert not result.cleanup_complete
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_pid_reuse_or_boot_change_refuses_signal(tmp_path, monkeypatch):
    from test_docker_lifecycle import _output, _reference

    from tetrabench.docker_lifecycle import execution_owner
    from tetrabench.local_control import initialize_owner

    output, digest = _output(tmp_path)
    reference = _reference(output, digest, os.getpid())
    identity = reference.process
    assert identity is not None
    monkeypatch.setattr(
        "tetrabench.engines.docker.process_identity",
        lambda _: identity.model_copy(update={"start_ticks": "stale"}),
    )
    monkeypatch.setattr(
        "tetrabench.run_reference.send_process_signal",
        lambda *a: pytest.fail("signalled reused PID"),
    )
    with execution_owner(output) as lock_identity:
        initialize_owner(reference, lock_identity)
        with pytest.raises(ValueError, match="identity changed"):
            DockerEngine().cancel(reference)
    assert not (output / "cancel.json").exists()


def test_legacy_request_and_receipt_bytes_and_digests_roundtrip():
    # Pinned pre-engine schema bytes, including the legacy absent reward-policy field.
    from test_submission import _prepared

    prepared = _prepared()
    old_request = canonical_model_bytes(prepared.request)
    assert b'"engine"' not in old_request
    request = parse_canonical_model(old_request, RequestRecord)
    assert canonical_model_bytes(request) == old_request
    assert request.plan_sha256 == prepared.request.plan_sha256
    old_receipt = (
        b'{"attempts":[{"attempt_id":"submit-old","controller_calls":[],"transitions":'
        b'[{"sequence":0,"type":"admission-observed"}]}],"context_manifest_sha256":"'
        + b"a" * 64
        + b'","plan_sha256":"'
        + b"b" * 64
        + b'","request_sha256":"'
        + b"c" * 64
        + b'","run_id":"old","schema_version":2}'
    )
    receipt = parse_canonical_model(old_receipt, SubmissionReceipt)
    assert canonical_model_bytes(receipt) == old_receipt
    assert sha256_hex(canonical_model_bytes(receipt)) == sha256_hex(old_receipt)


def test_submission_records_binding_before_spawn_and_refuses_rebinding(tmp_path):
    from test_submission import _MemorySubmissionStore, _prepared

    from tetrabench.controller import FakeDetachedController

    prepared = _prepared()
    receipts = ReceiptStore(tmp_path / "receipts")
    store = _MemorySubmissionStore()
    controller = FakeDetachedController()
    service = SubmissionService(store, controller, receipts)
    service.submit(prepared)
    reference = RunReferenceStore(tmp_path / "run-references").read(
        prepared.request.run_id
    )
    assert reference == _remote_reference(prepared)
    assert prepared.controller_launch is not None
    changed = replace(
        prepared,
        controller_launch=replace(
            prepared.controller_launch, environment_name="different"
        ),
    )
    before = list(store.operations)
    with pytest.raises(ReceiptConflictError, match="another engine binding"):
        service.submit(changed)
    assert store.operations == before


def test_remote_adapter_revalidates_request_and_uses_original_controller(
    tmp_path, monkeypatch
):
    from test_lifecycle import _prepared, _request, _Store

    from tetrabench.controller import FakeDetachedController

    request = _request()
    reference = RunReference(
        run_id=request.run_id,
        engine="modal",
        request_sha256=sha256_hex(canonical_model_bytes(request)),
        storage=request.plan.storage,
        app_name="tetrabench",
        function_name="controller",
        environment_name="recorded-env",
    )
    store = _Store(_prepared(request), request=request)
    controller = FakeDetachedController()
    seen = []

    def make_store(storage):
        assert storage == reference.storage
        return store

    def make_controller(app_name, function_name, *, environment_name):
        seen.append((app_name, function_name, environment_name))
        return controller

    monkeypatch.setattr("tetrabench.engines.modal.create_s3_store", make_store)
    monkeypatch.setattr(
        "tetrabench.engines.modal.ModalControllerClient", make_controller
    )
    monkeypatch.chdir(tmp_path)
    # No project or user configuration exists. Only immutable request/admission
    # reads establish state; the reference cannot establish terminal success.
    result = ModalEngine().result(reference)
    assert result.outcome is None and result.state == "nonterminal"
    cancelled = ModalEngine().cancel(reference)
    assert cancelled.cleanup_complete
    assert seen == [("tetrabench", "controller", "recorded-env")]
    assert "request" in store.operations


def test_remote_binding_mismatch_blocks_controller_construction(tmp_path, monkeypatch):
    from test_lifecycle import _prepared, _request, _Store

    request = _request()
    reference = RunReference(
        run_id=request.run_id,
        engine="modal",
        request_sha256=sha256_hex(canonical_model_bytes(request)),
        storage=request.plan.storage,
        app_name="wrong-app",
        function_name="controller",
        environment_name="recorded-env",
    )
    store = _Store(_prepared(request), request=request)
    monkeypatch.setattr("tetrabench.engines.modal.create_s3_store", lambda _: store)
    monkeypatch.setattr(
        "tetrabench.engines.modal.ModalControllerClient",
        lambda *a, **k: pytest.fail("constructed controller for wrong binding"),
    )
    with pytest.raises(ValueError, match="controller and immutable request disagree"):
        ModalEngine().cancel(reference)
    assert store.operations == ["request"]


def test_modal_default_launch_does_not_wait(tmp_path, monkeypatch):
    root = _project(tmp_path)
    with (root / "tetrabench.toml").open("a") as stream:
        stream.write(
            '\n[storage]\nprovider="aws"\nbucket="private"\nregion="us-east-1"\n'
        )
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        ModalEngine,
        "launch",
        lambda self, prepared, output: LocalReport(
            run_id=prepared.request.run_id,
            state="submitted",
            job_directory="remote",
        ),
    )
    monkeypatch.setattr(
        ModalEngine, "result", lambda *a: pytest.fail("detached default waited")
    )
    result = runner.invoke(app, ["run", "third-domain", "--engine", "modal", "--json"])
    assert result.exit_code == 0, result.stderr


def test_terminal_observation_does_not_replace_native_result_evidence(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    prepared = prepare_run(root, "third-domain", run_id="missing-native")
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", SuccessfulRunner)
    run_prepared_local(prepared, tmp_path / "out")
    reference = RunReferenceStore().read("missing-native")
    assert reference is not None
    # A terminal observation without Harbor's real native files proves nothing.
    report = DockerEngine().result(reference)
    assert report.state == "unknown" and report.outcome is None
    assert not report.cleanup_complete
