from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tetrabench.canonical_json import loads_canonical_json
from tetrabench.cli import _result_exit, app
from tetrabench.controller import FakeDetachedController
from tetrabench.engines import modal as engine_module
from tetrabench.lifecycle import FakeChildCleanupObserver
from tetrabench.models import ProjectConfig
from tetrabench.remote import RemoteResult
from tetrabench.run_reference import RunReferenceStore

runner = CliRunner()


@pytest.mark.parametrize("admission", ["failed", "cancelled"])
def test_terminal_success_dominates_stale_admission_every_exit_path(admission):
    result = RemoteResult(
        run_id="run", state="terminal", outcome="succeeded", admission_state=admission
    )
    assert _result_exit(result) == 0
    assert _result_exit(result.model_copy(update={"state": "conflict"})) == 3
    assert (
        _result_exit(
            result.model_copy(update={"state": "nonterminal", "outcome": None})
        )
        == 1
    )


@pytest.mark.parametrize("operation", ["status", "cancel", "recover"])
def test_corrupt_reference_explicit_profile_uses_validated_legacy_binding(
    tmp_path, monkeypatch, operation
):
    from test_lifecycle import _prepared, _request, _Store

    request = _request()
    assert request.plan.storage is not None
    config = ProjectConfig.model_validate(
        {
            "schema_version": 1,
            "storage": request.plan.storage.model_dump(),
            "controller": {"kind": "modal", "app_name": "new-profile-app"},
        }
    )
    store = _Store(_prepared(request), request=request)
    refs = RunReferenceStore()
    refs.root.mkdir(parents=True)
    path = refs.root / f"{request.run_id}.json"
    path.write_bytes(b"broken routing cache")
    calls = []

    def controller(app_name, function_name, *, environment_name):
        calls.append((app_name, function_name, environment_name))
        return FakeDetachedController()

    monkeypatch.setattr(engine_module, "load_project_config", lambda *a, **k: config)
    monkeypatch.setattr(engine_module, "create_s3_store", lambda *a: store)
    monkeypatch.setattr(engine_module, "ModalControllerClient", controller)
    monkeypatch.setattr(
        engine_module, "ModalChildObserver", lambda *a, **k: FakeChildCleanupObserver()
    )
    monkeypatch.chdir(tmp_path)
    command = [operation, request.run_id, "--profile", "old", "--json"]
    if operation != "status":
        command += ["--yes", "--environment", "tetrabench-old"]
    result = runner.invoke(app, command)
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert path.read_bytes() == b"broken routing cache"
    assert calls == [
        (
            "tetrabench",
            "controller",
            None if operation == "status" else "tetrabench-old",
        )
    ]


@pytest.mark.parametrize("operation", ["cancel", "recover"])
def test_legacy_mutation_never_infers_versioned_namespace(
    tmp_path, monkeypatch, operation
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        engine_module,
        "create_s3_store",
        lambda *a: pytest.fail("constructed provider before explicit namespace"),
    )
    result = runner.invoke(
        app, [operation, "legacy-run", "--profile", "old", "--yes", "--json"]
    )
    assert result.exit_code == 2
    assert "--environment" in result.stderr
    assert "no namespace was guessed" in result.stderr


def test_corrupt_reference_without_explicit_profile_cannot_fall_back(
    tmp_path, monkeypatch
):
    refs = RunReferenceStore()
    refs.root.mkdir(parents=True)
    (refs.root / "run.json").write_bytes(b"broken")
    monkeypatch.setattr(
        engine_module,
        "create_s3_store",
        lambda *a: pytest.fail("implicitly selected storage"),
    )
    result = runner.invoke(app, ["result", "run", "--json"])
    assert result.exit_code == 2
    assert "explicit --profile" in result.stderr


@pytest.mark.parametrize(
    "options", [["--no-wait"], ["--no-wait", "--detach"], ["--no-detach"]]
)
def test_run_rejects_accidental_negative_boolean_flags(monkeypatch, options):
    monkeypatch.setattr(
        "tetrabench.cli.prepare_run",
        lambda *a, **k: pytest.fail("prepared invalid flag combination"),
    )
    result = runner.invoke(app, ["run", "example", *options])
    assert result.exit_code == 2


def test_deploy_emits_exact_returned_wheel_identity(monkeypatch):
    spec = SimpleNamespace(
        app_name="app", environment_name="env", as_dict=lambda: {"app_name": "app"}
    )
    report = {
        "app_name": "app",
        "deployed": True,
        "wheel_filename": "tetrabench-0.1.0-py3-none-any.whl",
        "wheel_sha256": "a" * 64,
    }
    monkeypatch.setattr("tetrabench.cli._deployment_spec", lambda _: spec)
    monkeypatch.setattr("tetrabench.cli.deploy_controller", lambda _: report)
    result = runner.invoke(app, ["controller", "deploy", "--yes", "--json"])
    assert result.exit_code == 0, result.stderr
    assert loads_canonical_json(result.stdout.strip().encode()) == report


@pytest.mark.parametrize("admission", ["failed", "cancelled"])
def test_wait_and_result_cli_keep_terminal_success_after_admission_race(
    tmp_path, monkeypatch, admission
):
    from test_engines import _project, _remote_reference

    from tetrabench.engines.docker import LocalReport

    root = _project(tmp_path)
    with (root / "tetrabench.toml").open("a") as stream:
        stream.write(
            '\n[storage]\nprovider="aws"\nbucket="private"\nregion="us-east-1"\n'
        )
    monkeypatch.chdir(root)

    def launch(self, prepared, output):
        RunReferenceStore().create(_remote_reference(prepared))
        return LocalReport(
            run_id=prepared.request.run_id, state="submitted", job_directory="remote"
        )

    def result(self, reference):
        return RemoteResult(
            run_id=reference.run_id,
            state="terminal",
            outcome="succeeded",
            admission_state=admission,
        )

    monkeypatch.setattr(engine_module.ModalEngine, "launch", launch)
    monkeypatch.setattr(engine_module.ModalEngine, "result", result)
    waited = runner.invoke(
        app,
        [
            "run",
            "third-domain",
            "--engine",
            "modal",
            "--run-id",
            "race",
            "--wait",
            "--json",
        ],
    )
    assert waited.exit_code == 0, waited.stderr
    read = runner.invoke(app, ["result", "race", "--json"])
    assert read.exit_code == 0, read.stderr
