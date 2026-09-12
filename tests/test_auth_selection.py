"""Public authoring references resolve once, without weakening immutable auth."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_auth_onboarding import onboarding as onboarding
from typer.testing import CliRunner

from tetrabench.auth_config import (
    AuthSpec,
    NativeAuthReference,
    ProfileAuthSpec,
    authentication_environment_names,
)
from tetrabench.auth_selection import resolve_harness_auth
from tetrabench.auth_sessions import AuthError, claim_session
from tetrabench.authoring import initialize_project
from tetrabench.cli import app
from tetrabench.config import load_harness_override
from tetrabench.harness_config import HarnessConfig, ResolvedHarness
from tetrabench.models import ConfigOverrides, ResolvedPlan
from tetrabench.plan import canonical_model_bytes, parse_canonical_model, resolve_plan
from tetrabench.submission import prepare_run, prepare_submission


def harness_file(root: Path) -> Path:
    path = root / "codex.toml"
    path.write_text(
        '[harness]\nname="codex"\nversion="0.154.0"\nmodel="openai/model"\n'
        '[harness.auth]\nmode="chatgpt_oauth"\n'
        'reference={kind="profile",profile="eval"}\n'
    )
    return path


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch):
    for name in authentication_environment_names():
        monkeypatch.delenv(name, raising=False)
    for name in ("TETRABENCH_AUTH_CONFIG_FILE", "TETRABENCH_AUTH_CONFIG_CONTENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "tetrabench.auth_profiles.boto3.client",
        lambda *a, **kw: pytest.fail("unexpected provider construction"),
    )


def test_authoring_load_is_offline_but_immutable_schema_rejects_alias(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "tetrabench.auth_profiles.resolve_profile_reference",
        lambda *a, **kw: pytest.fail("validation resolved credentials"),
    )
    spec = load_harness_override(harness_file(tmp_path))
    assert isinstance(spec.auth, ProfileAuthSpec)
    with pytest.raises(ValidationError):
        AuthSpec.model_validate(spec.auth.model_dump())
    with pytest.raises(ValidationError):
        ResolvedHarness.model_validate(spec.model_dump(exclude={"args"}))
    assert isinstance(
        HarnessConfig.model_validate(spec.model_dump()).auth, ProfileAuthSpec
    )


def test_login_logout_relogin_public_commands_and_generation_pinning(
    onboarding, tmp_path, monkeypatch
):
    _, config, env, _, _, authority = onboarding
    project = initialize_project(tmp_path / "project")
    path = harness_file(project)
    runner = CliRunner()
    monkeypatch.chdir(project)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TETRABENCH_AUTH_CONFIG_FILE", str(config))
    args = ["--profile", "eval", "--executable", "synthetic-native", "--json"]
    first = runner.invoke(app, ["auth", "login", *args, "--agent", "codex"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["generation"] == 1
    old = resolve_plan(
        project,
        "example",
        overrides=ConfigOverrides(harness=load_harness_override(path)),
    )
    before = canonical_model_bytes(old)
    assert old.harness is not None and old.harness.auth is not None
    assert isinstance(old.harness.auth.reference, NativeAuthReference)
    status = runner.invoke(app, ["auth", "status", "--profile", "eval", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["account_verified"] is False
    logout = runner.invoke(app, ["auth", "logout", *args, "--yes"])
    assert logout.exit_code == 0, logout.output
    login = runner.invoke(app, ["auth", "login", *args])
    assert login.exit_code == 0, login.output
    assert json.loads(login.stdout)["generation"] == 2
    planned = runner.invoke(app, ["plan", "example", "--harness", str(path), "--json"])
    assert planned.exit_code == 0, planned.output
    newer = parse_canonical_model(planned.stdout.strip().encode(), ResolvedPlan)
    assert newer.harness is not None and newer.harness.auth is not None
    assert isinstance(newer.harness.auth.reference, NativeAuthReference)
    assert newer.harness.auth.reference.generation == 2
    assert canonical_model_bytes(old) == before
    assert canonical_model_bytes(newer) != before
    with pytest.raises(AuthError, match="stale"):
        claim_session(authority(), old.harness.auth.reference, "codex")
    assert 'kind="profile"' in path.read_text()


@pytest.mark.parametrize("entry", ["plan", "prepare_run", "prepare_submission"])
def test_all_preparation_paths_resolve_one_reference(tmp_path, monkeypatch, entry):
    project = initialize_project(tmp_path / "project")
    spec = load_harness_override(harness_file(project))
    calls = []

    def reference(name, **kwargs):
        calls.append((name, kwargs["allow_online"]))
        return NativeAuthReference(profile=name, generation=7, binding="cloud-evals")

    monkeypatch.setattr("tetrabench.auth_profiles.resolve_profile_reference", reference)
    overrides = ConfigOverrides(harness=spec)
    if entry == "plan":
        plan = resolve_plan(project, "example", overrides=overrides)
        assert calls == [("eval", False)]
    else:
        prepared = (
            prepare_run(project, "example", overrides=overrides)
            if entry == "prepare_run"
            else prepare_submission(
                project, "example", overrides=overrides, require_remote=False
            )
        )
        plan = prepared.plan
        assert calls == [("eval", True)]
        from tetrabench.plan import plan_digest

        assert prepared.request.plan_sha256 == plan_digest(plan)
    assert plan.harness is not None and plan.harness.auth is not None
    assert isinstance(plan.harness.auth.reference, NativeAuthReference)
    assert plan.harness.auth.reference.generation == 7
    assert plan.harness.auth.reference.binding == "cloud-evals"
    assert isinstance(spec.auth, ProfileAuthSpec)


def test_plan_remote_read_requires_explicit_online(tmp_path, monkeypatch):
    project = initialize_project(tmp_path / "project")
    path = harness_file(project)
    calls = []

    def reference(name, **kwargs):
        calls.append(kwargs["allow_online"])
        if not kwargs["allow_online"]:
            raise AuthError("S3 auth selection requires --online")
        return NativeAuthReference(profile=name, generation=3, binding="cloud")

    monkeypatch.setattr("tetrabench.auth_profiles.resolve_profile_reference", reference)
    monkeypatch.chdir(project)
    runner = CliRunner()
    args = ["plan", "example", "--harness", str(path), "--json"]
    assert runner.invoke(app, args).exit_code == 2
    online = runner.invoke(app, [*args, "--online"])
    assert online.exit_code == 0, online.output
    assert calls == [False, True]


@pytest.mark.parametrize("version", ["2.1.267", "2.1.269"])
def test_explicit_claude_versions_preserve_canonical_auth(version):
    harness = ResolvedHarness(
        name="claude-code",
        version=version,
        model="anthropic/claude-sonnet-4-6",
        auth=AuthSpec.model_validate(
            {"mode": "api_key", "reference": {"kind": "env", "name": "EVAL_KEY"}}
        ),
    )
    encoded = canonical_model_bytes(harness)
    assert (
        canonical_model_bytes(parse_canonical_model(encoded, ResolvedHarness))
        == encoded
    )


def test_api_key_selection_never_reads_private_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tetrabench.auth_profiles.resolve_profile_reference",
        lambda *a, **kw: pytest.fail("API key used profile resolution"),
    )
    spec = HarnessConfig(
        name="codex",
        version="0.154.0",
        model="openai/model",
        auth=AuthSpec.model_validate(
            {"mode": "api_key", "reference": {"kind": "env", "name": "EVAL_KEY"}}
        ),
    )
    assert resolve_harness_auth(spec) is spec
    from tetrabench.auth_diagnostics import doctor_auth_report

    report = doctor_auth_report(spec, engine="modal", environment={})
    assert report["configured"]["status"] == "ok"
    assert report["remote_runtime_checked"]["status"] == "not_attempted"


def test_offline_inspection_keeps_alias_without_authority_reads(tmp_path, monkeypatch):
    path = harness_file(tmp_path)
    monkeypatch.setattr(
        "tetrabench.auth_profiles.resolve_profile_reference",
        lambda *a, **kw: pytest.fail("offline inspection resolved authority"),
    )
    seen = []

    def inspect(config, **kwargs):
        seen.append(config)
        assert "runtime" not in kwargs
        return {"capability": {"status": "unknown", "controls": [], "limitations": []}}

    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = CliRunner().invoke(
        app, ["models", "inspect", "--harness", str(path), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert isinstance(seen[0].auth, ProfileAuthSpec)
    adopt = CliRunner().invoke(
        app,
        [
            "models",
            "adopt",
            "--harness",
            str(path),
            "--control",
            "reasoning_effort",
            "--select",
            "high",
            "--json",
        ],
    )
    assert adopt.exit_code == 2
    assert "--allow-authenticated-read" in adopt.output


def test_authenticated_adoption_resolves_then_pins_profile(
    onboarding, tmp_path, monkeypatch
):
    from test_authenticated_discovery import fake_native
    from test_models_cli import snapshot

    login, private, env, _, _, _ = onboarding
    login(agent="codex")
    path = harness_file(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TETRABENCH_AUTH_CONFIG_FILE", str(private))
    fake_native(monkeypatch, tmp_path, "codex", mode="chatgpt_oauth")

    def collect(config, **options):
        assert config.auth.reference.kind == "native_session"
        assert config.auth.reference.generation == 1
        assert options["runtime"].spec == config.auth
        result = snapshot(config)
        return result.model_copy(
            update={
                "identity": result.identity.model_copy(
                    update={"auth_mode": "chatgpt_oauth", "profile_ref": "eval"}
                )
            }
        )

    monkeypatch.setattr("tetrabench.native_discovery.collect_installed", collect)
    result = CliRunner().invoke(
        app,
        [
            "models",
            "adopt",
            "--harness",
            str(path),
            "--allow-authenticated-read",
            "--control",
            "effort",
            "--select",
            "high",
            "--write",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    adopted = load_harness_override(path)
    assert isinstance(adopted.auth, AuthSpec)
    assert isinstance(adopted.auth.reference, NativeAuthReference)
    assert adopted.auth.reference.generation == 1
    assert adopted.capability_snapshot is not None
    assert "SYNTHETIC" not in path.read_text()


def test_doctor_offline_profile_has_no_provider_or_login(tmp_path, monkeypatch):
    project = initialize_project(tmp_path / "project")
    path = harness_file(project)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    config = private / "auth.toml"
    config.write_text(
        '[profiles.eval]\nharness="codex"\nbinding="cloud"\n'
        '[profiles.eval.backend]\nkind="s3"\napproved_private_backend=true\n'
        'access_key={kind="env",name="TETRABENCH_AUTH_ID"}\n'
        'secret_key={kind="env",name="TETRABENCH_AUTH_KEY"}\n'
        '[profiles.eval.backend.storage]\nprovider="tigris"\nbucket="auth-only"\n'
    )
    config.chmod(0o600)
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "tetrabench.auth_profiles.resolve_profile_reference",
        lambda *a, **kw: pytest.fail("offline doctor resolved authority"),
    )
    monkeypatch.setattr(
        "tetrabench.auth.run_native",
        lambda *a, **kw: pytest.fail("offline doctor ran native login"),
    )
    result = CliRunner().invoke(
        app, ["doctor", "--harness", str(path), "--auth-config", str(config), "--json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["authentication"]
    assert report["configured"]["status"] == "unproven"
    assert report["provider_checked"]["status"] == "not_attempted"
    assert report["auth_profile"]["generation"] is None


@pytest.mark.parametrize(
    "write,yes,ok,code",
    [
        (False, False, True, 0),
        (True, False, True, 2),
        (True, True, True, 0),
        (True, True, False, 2),
    ],
)
def test_controller_configure_public_callout(
    tmp_path, monkeypatch, write, yes, ok, code
):
    project = initialize_project(tmp_path / "project")
    monkeypatch.chdir(project)
    from tetrabench.models import ProjectConfig

    monkeypatch.setattr(
        "tetrabench.cli.load_project_config",
        lambda *a, **kw: ProjectConfig(schema_version=1),
    )
    calls = []

    def configure(config, **kwargs):
        calls.append(kwargs)
        return {"ok": ok, "secret_state": "unknown" if not ok else "created"}

    monkeypatch.setattr(
        "tetrabench.controller_configure.configure_controller", configure
    )
    args = [
        "controller",
        "configure",
        "--profile",
        "cloud",
        "--env",
        "API_KEY",
        "--json",
    ]
    if write:
        args.append("--write")
    if yes:
        args.append("--yes")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == code, result.output
    if write and not yes:
        assert calls == []
    else:
        assert calls[0]["env_names"] == ["API_KEY"]
        assert calls[0]["profile"] == "cloud"
        assert calls[0]["write"] is write
        assert calls[0]["confirmed"] is write
        assert json.loads(result.stdout)["ok"] is ok


def test_controller_alias_requires_explicit_matching_profile(tmp_path):
    from test_controller_configure import AUTH_KEYS, auth_file, project

    from tetrabench.controller_configure import STORAGE_ENV_NAMES, configure_controller

    spec = HarnessConfig.model_validate(
        {
            "name": "codex",
            "version": "0.154.0",
            "model": "openai/model",
            "auth": {
                "mode": "chatgpt_oauth",
                "reference": {"kind": "profile", "profile": "chosen"},
            },
        }
    )
    config = project(harness=spec)
    path = auth_file(tmp_path)
    with pytest.raises(AuthError, match="--auth-profile"):
        configure_controller(config, profile="cloud", env_names=STORAGE_ENV_NAMES)
    result = configure_controller(
        config,
        profile="cloud",
        auth_profiles=["chosen"],
        auth_config_path=path,
        env_names=STORAGE_ENV_NAMES | AUTH_KEYS,
    )
    assert result["auth_profiles"] == ["chosen"]
    assert result["write"] is False
