"""Auth commands use explicit references and existing private authorities."""

import json

import pytest
from typer.testing import CliRunner

from tetrabench.auth import AuthStatus
from tetrabench.cli import app


def config(tmp_path, mode="chatgpt_oauth", name="codex", generation=1):
    path = tmp_path / "run.toml"
    reference = (
        f'kind="native_session"\nprofile="eval-only"\nbinding="local"\ngeneration={generation}\n'
        if mode == "chatgpt_oauth"
        else 'kind="env"\nname="SELECTED_TOKEN"\n'
    )
    path.write_text(
        f'[harness]\nname="{name}"\n'
        f'version="{"2.1.267" if name == "claude-code" else "0.154.0"}"\n'
        f'model="openai/model"\n[harness.auth]\nmode="{mode}"\n'
        f"[harness.auth.reference]\n{reference}"
    )
    return path


@pytest.mark.parametrize("action", ["login", "status", "logout", "reseed"])
def test_local_auth_dispatches_existing_handler_with_explicit_authority(
    tmp_path, monkeypatch, action
):
    path = config(tmp_path, generation=2 if action == "reseed" else 1)
    calls = []

    def handler(harness, spec, **kwargs):
        calls.append((harness, spec, kwargs))
        return AuthStatus(
            harness=harness,
            mode=spec.mode,
            state="ready",
            generation=spec.reference.generation,
        )

    monkeypatch.setattr("tetrabench.auth.auth_" + action, handler)
    args = [
        "auth",
        action,
        "--harness",
        str(path),
        "--authority",
        str(tmp_path / "authority"),
        "--local-filesystem",
        "--executable",
        "/pinned/codex",
        "--json",
    ]
    if action == "logout":
        args.append("--yes")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["account_verified"] is False
    assert len(calls) == 1
    assert calls[0][2]["store"].binding == "local"
    assert calls[0][2]["executable"] == "/pinned/codex"
    assert calls[0][2]["artifact_roots"] == ()


def test_auth_rejects_unconfirmed_shared_authority_before_login(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setattr(
        "tetrabench.auth.auth_login", lambda *a, **k: pytest.fail("unexpected login")
    )
    result = CliRunner().invoke(
        app,
        [
            "auth",
            "login",
            "--harness",
            str(path),
            "--authority",
            str(tmp_path / "authority"),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert not (tmp_path / "authority").exists()


def test_native_status_never_runs_a_credential_helper(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setattr(
        "tetrabench.auth._prepare_runtime",
        lambda *a, **k: pytest.fail("helper invoked"),
    )
    result = CliRunner().invoke(
        app,
        [
            "auth",
            "status",
            "--harness",
            str(path),
            "--authority",
            str(tmp_path / "authority"),
            "--local-filesystem",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "absent"


def test_claude_setup_token_uses_env_reference_without_copying_store(
    tmp_path, monkeypatch
):
    path = config(tmp_path, "claude_setup_token", "claude-code")

    def login(name, spec, **kwargs):
        assert spec.reference.name == "SELECTED_TOKEN"
        assert kwargs["store"] is None
        return AuthStatus(harness=name, mode=spec.mode, state="setup_required")

    monkeypatch.setattr("tetrabench.auth.auth_login", login)
    result = CliRunner().invoke(
        app,
        [
            "auth",
            "login",
            "--harness",
            str(path),
            "--executable",
            "/pinned/claude",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "setup_required"


def test_private_profile_status_uses_auth_owner_config_and_never_native_cli(
    tmp_path, monkeypatch
):
    path = tmp_path / "auth.toml"
    path.write_text(
        f'schema_version=1\nruntime_directory="{tmp_path / "runtime"}"\n'
        '[profiles.codex-eval]\nharness="codex"\nbinding="local"\ngeneration=1\n'
        '[profiles.codex-eval.backend]\nkind="local"\napproved_private_backend=true\n'
        f'state_directory="{tmp_path / "authority"}"\n'
    )
    path.chmod(0o600)
    monkeypatch.setattr(
        "tetrabench.auth._prepare_runtime",
        lambda *a, **k: pytest.fail("native status helper ran"),
    )
    result = CliRunner().invoke(
        app,
        [
            "auth",
            "status",
            "--profile",
            "codex-eval",
            "--auth-config",
            str(path),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "absent"
