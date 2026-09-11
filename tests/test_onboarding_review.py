"""Public error, native login method, and explicit Secret transport regressions."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from test_auth_onboarding import onboarding as onboarding
from typer.testing import CliRunner

from tetrabench.auth_config import authentication_environment_names
from tetrabench.authoring import initialize_project
from tetrabench.cli import app


@pytest.fixture(autouse=True)
def no_provider_access(monkeypatch):
    for name in authentication_environment_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TETRABENCH_AUTH_CONFIG_CONTENT", raising=False)
    monkeypatch.delenv("TETRABENCH_AUTH_CONFIG_FILE", raising=False)
    monkeypatch.setattr(
        "socket.socket.connect", lambda *a, **kw: pytest.fail("unexpected network")
    )


@pytest.mark.parametrize("agent", ["codex", "opencode"])
@pytest.mark.parametrize("browser", [False, True])
def test_public_profile_reseed_preserves_native_login_method(
    onboarding, monkeypatch, agent, browser
):
    from tetrabench import auth

    login, config, env, _, _, _ = onboarding
    login(agent=agent)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    captured = []
    synthetic_native = auth.run_native

    def native(argv, **kwargs):
        captured.append(argv)
        return synthetic_native(argv, **kwargs)

    monkeypatch.setattr(auth, "run_native", native)
    args = [
        "auth",
        "reseed",
        "--profile",
        "eval",
        "--auth-config",
        str(config),
        "--executable",
        "synthetic-native",
        "--json",
    ]
    if browser:
        args.append("--browser-auth")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["generation"] == 2
    command = next(
        argv for argv in captured if "login" in argv and "status" not in argv
    )
    if agent == "codex":
        assert ("--device-auth" in command) is not browser
    else:
        assert (
            command[-1] == f"ChatGPT Pro/Plus ({'browser' if browser else 'headless'})"
        )


def test_doctor_invalid_modal_starter_is_one_canonical_error(tmp_path, monkeypatch):
    root = initialize_project(tmp_path / "project")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["doctor", "--engine", "modal", "--json"])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    report = json.loads(result.stderr)
    assert report["mutation_attempted"] is False
    assert "storage" in report["error"]


@pytest.mark.parametrize(
    "version,accepted",
    [("22.18.9", False), ("22.19.0", True), ("23.0.0", True), ("24.21.0", True)],
)
def test_login_pi_prerequisite_matches_diagnostic_engine_boundary(
    tmp_path, monkeypatch, version, accepted
):
    from tetrabench.auth_sessions import AuthError
    from tetrabench.nativeauth import NativeResult, preflight_native_auth

    package = tmp_path / "pi"
    module = package / "dist/index.js"
    module.parent.mkdir(parents=True)
    module.write_text("// synthetic native entrypoint")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@earendil-works/pi-coding-agent",
                "version": "0.85.1",
                "engines": {"node": ">=22.19.0"},
            }
        )
    )
    monkeypatch.setattr(
        "tetrabench.nativeauth.run_native",
        lambda *a, **kw: NativeResult(0, f"v{version}\n".encode()),
    )
    if accepted:
        result = preflight_native_auth(
            "pi", executable="synthetic-node", pi_module=module, environment={}
        )
        assert result.version == "0.85.1"
    else:
        with pytest.raises(AuthError, match=r"Node >=22\.19\.0"):
            preflight_native_auth(
                "pi", executable="synthetic-node", pi_module=module, environment={}
            )


def api_harness(root):
    path = root / "api.toml"
    path.write_text(
        '[harness]\nname="codex"\nversion="0.154.0"\nmodel="openai/model"\n'
        '[harness.auth]\nmode="api_key"\n'
        'reference={kind="env",name="SYNTHETIC_CHECK_KEY"}\n'
    )
    return path


@pytest.mark.parametrize("failure", ["exit", "timeout", "malformed"])
@pytest.mark.parametrize("json_output", [False, True])
def test_public_provider_check_fails_when_native_version_probe_fails(
    tmp_path, monkeypatch, failure, json_output
):
    from tetrabench.native_discovery import NativeInstallation
    from tetrabench.nativeauth import NativeResult

    root = initialize_project(tmp_path / "project")
    path = api_harness(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SYNTHETIC_CHECK_KEY", "synthetic-private-value")
    monkeypatch.setattr(
        "tetrabench.native_discovery.native_installation",
        lambda *a, **kw: NativeInstallation(("/test/codex",), "/test/node"),
    )
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.shutil.which", lambda name, **kw: "/test/" + name
    )
    probes = []

    def native(argv, **kwargs):
        assert argv[-1] == "--version" and "--net" in argv
        probes.append(argv)
        if failure == "timeout":
            raise TimeoutError("synthetic-private-value")
        return NativeResult(1 if failure == "exit" else 0, b"synthetic-private-value")

    def forbidden(*args, **kwargs):
        pytest.fail("failed prerequisite attempted auth custody or metadata")

    monkeypatch.setattr("tetrabench.nativeauth.run_native", native)
    monkeypatch.setattr("tetrabench.discovery_auth.metadata_auth_context", forbidden)
    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", forbidden
    )
    args = ["doctor", "--harness", str(path), "--check-provider"]
    if json_output:
        args.append("--json")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output
    assert len(probes) == 1
    assert "synthetic-private-value" not in result.output
    if json_output:
        report = json.loads(result.stdout)["authentication"]
        assert report["native_ready"]["status"] == "unproven"
        assert report["native_ready"]["observed_version"] is None
        assert report["provider_checked"]["status"] == "failed"
        assert report["provider_checked"]["metadata_status"] == "not_attempted"
        assert report["account_verified"]["status"] == "unproven"
    else:
        assert "provider_checked: failed" in result.stdout
        assert "account_verified: unproven" in result.stdout


@pytest.mark.parametrize("failure", [None, "enter", "collect", "exit"])
def test_public_provider_check_failure_is_not_unknown_success(
    tmp_path, monkeypatch, failure
):
    root = initialize_project(tmp_path / "project")
    path = api_harness(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SYNTHETIC_CHECK_KEY", "synthetic-private-value")
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.check_native_prerequisites",
        lambda *a, **kw: {"status": "ok", "action": "Already installed."},
    )

    @contextmanager
    def context(*args, **kwargs):
        if failure == "enter":
            raise RuntimeError("synthetic-private-value")
        yield object()
        if failure == "exit":
            raise RuntimeError("synthetic-private-value")

    def inspect(*args, **kwargs):
        if failure == "collect":
            raise RuntimeError("synthetic-private-value")
        return {"capability": {"status": "unsupported"}}

    monkeypatch.setattr("tetrabench.discovery_auth.metadata_auth_context", context)
    monkeypatch.setattr(
        "tetrabench.discovery_auth.assert_safe_metadata", lambda *a: None
    )
    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = CliRunner().invoke(
        app, ["doctor", "--harness", str(path), "--check-provider", "--json"]
    )
    assert result.exit_code == (2 if failure else 0), result.output
    report = json.loads(result.stdout)["authentication"]
    assert report["account_verified"]["status"] == "unproven"
    provider = report["provider_checked"]
    assert provider["status"] == ("failed" if failure else "unproven")
    assert provider["metadata_status"] == ("failed" if failure else "unsupported")
    assert "synthetic-private-value" not in result.output


@pytest.mark.parametrize("status", ["ok", "blocked"])
def test_public_doctor_controller_report_agrees_in_human_and_json(
    tmp_path, monkeypatch, status
):
    from test_cli import _DoctorClient
    from test_controller_configure import project

    from tetrabench.config import load_harness_override
    from tetrabench.s3 import S3Store

    root = initialize_project(tmp_path / "project")
    config = project(harness=load_harness_override(api_harness(root)))
    monkeypatch.chdir(root)
    monkeypatch.setattr("tetrabench.cli.load_project_config", lambda *a, **kw: config)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda storage: S3Store(storage, _DoctorClient()),
    )
    action = "Check the selected Secret and deployed Function."
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.check_controller_metadata",
        lambda *a, **kw: {
            "status": status,
            "action": action,
            "remote_runtime_checked": {
                "status": "unproven",
                "action": "No invocation.",
            },
        },
    )
    runner = CliRunner()
    human = runner.invoke(app, ["doctor", "--online"])
    machine = runner.invoke(app, ["doctor", "--online", "--json"])
    assert human.exit_code == machine.exit_code == (2 if status == "blocked" else 0)
    assert f"cloud_controller: {status}" in human.stdout
    assert action in human.stdout
    report = json.loads(machine.stdout)
    cloud = next(
        check for check in report["checks"] if check["name"] == "cloud_controller"
    )
    assert cloud["status"] == report["controller"]["status"] == status
    assert report["controller"]["action"] == action


def test_public_configure_transports_only_selected_process_variables(
    tmp_path, monkeypatch
):
    from test_controller_configure import FakeModal, project

    from tetrabench import controller_configure
    from tetrabench.modal_app import controller_deployment_spec

    root = initialize_project(tmp_path / "project")
    config = project()
    fake = FakeModal()
    real_configure = controller_configure.configure_controller
    monkeypatch.chdir(root)
    monkeypatch.setattr("tetrabench.cli.load_project_config", lambda *a, **kw: config)
    monkeypatch.setattr(
        controller_configure,
        "configure_controller",
        lambda *a, **kw: real_configure(*a, modal_module=fake, **kw),
    )
    values = {
        "AWS_ACCESS_KEY_ID": "synthetic-id",
        "AWS_SECRET_ACCESS_KEY": "synthetic-private-value",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-forward")
    result = CliRunner().invoke(
        app,
        [
            "controller",
            "configure",
            "--profile",
            "cloud",
            "--env",
            "AWS_ACCESS_KEY_ID",
            "--env",
            "AWS_SECRET_ACCESS_KEY",
            "--write",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.contents == values
    assert (
        fake.environment_name
        == controller_deployment_spec(config, "cloud").environment_name
    )
    assert "must-not-forward" not in result.stdout
    assert "synthetic-private-value" not in result.stdout
