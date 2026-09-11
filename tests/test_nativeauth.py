from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest

from tetrabench.auth import (
    auth_login,
    auth_logout,
    auth_reseed,
    auth_session,
    auth_status,
)
from tetrabench.auth_config import AuthSpec, EnvAuthReference, NativeAuthReference
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    AuthStateError,
    LocalSessionStore,
    claim_session,
    read_private,
    seed_session,
    write_private,
)
from tetrabench.nativeauth import (
    NativeResult,
    assert_auth_outside_artifacts,
    inspect_native_store,
    isolated_auth_environment,
    native_auth_command,
    parse_native_status,
    run_native,
    verify_native_version,
)


def native_bytes(harness="codex", marker="SYNTHETIC_ACCESS"):
    if harness == "codex":
        return json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": marker,
                    "refresh_token": "SYNTHETIC_REFRESH_ONLY",
                    "id_token": "SYNTHETIC_ID_ONLY",
                },
            }
        ).encode()
    provider = "openai" if harness == "opencode" else "openai-codex"
    return json.dumps(
        {
            provider: {
                "type": "oauth",
                "access": marker,
                "refresh": "SYNTHETIC_REFRESH_ONLY",
                "expires": 4000000000000,
            }
        }
    ).encode()


@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
def test_native_formats_not_cross_harness_blobs(harness):
    data = native_bytes(harness)
    assert inspect_native_store(harness, data).mode == "chatgpt_oauth"
    for other in {"codex", "opencode", "pi"} - {harness}:
        with pytest.raises(AuthStateError):
            inspect_native_store(other, data)


@pytest.mark.parametrize(
    "data",
    [
        b'{"openai":{"type":"oauth"},"anthropic":{"type":"oauth"}}',
        b'{"openai":{"type":"api","key":"SYNTHETIC"}}',
        b'{"openai":{},"openai":{}}',
        b'{"openai":{"type":"oauth","refresh":"SYNTHETIC","access":"SYNTHETIC","expires":true}}',
    ],
)
def test_mixed_or_invalid_native_stores_fail_without_leaking(data):
    with pytest.raises(AuthStateError) as error:
        inspect_native_store("opencode", data)
    assert "SYNTHETIC" not in str(error.value)


def test_expiry_is_source_bounded_metadata():
    value = json.loads(native_bytes("pi"))
    value["openai-codex"]["expires"] = 50
    metadata = inspect_native_store("pi", json.dumps(value).encode(), now_ms=100)
    assert metadata.validity == "expired"
    assert metadata.expires_at_ms == 50
    assert metadata.expiry_source == "native_store"
    assert "SYNTHETIC" not in repr(metadata)
    assert inspect_native_store("codex", native_bytes()).expires_at_ms is None


def test_status_uses_native_output_not_env_presence():
    metadata = parse_native_status(
        "codex", NativeResult(0, b"Logged in using an API key - SYNTHETIC_SUFFIX")
    )
    assert metadata.mode == "api_key"
    assert "SYNTHETIC" not in repr(metadata)
    assert (
        parse_native_status("codex", NativeResult(0, b"Logged in using ChatGPT")).mode
        == "chatgpt_oauth"
    )
    assert (
        parse_native_status("codex", NativeResult(1, b"SYNTHETIC_SECRET_ERROR")).mode
        == "none"
    )
    assert (
        parse_native_status(
            "claude-code", NativeResult(0, b'{"loggedIn":true,"authMethod":"api_key"}')
        ).mode
        == "api_key"
    )
    assert (
        parse_native_status(
            "claude-code",
            NativeResult(0, b'{"loggedIn":true,"authMethod":"oauth_token"}'),
        ).mode
        == "claude_setup_token"
    )
    assert (
        parse_native_status("pi", NativeResult(0, b'{"type":"oauth"}')).mode
        == "chatgpt_oauth"
    )
    assert (
        parse_native_status(
            "opencode", NativeResult(0, b"OpenAI \x1b[2moauth\x1b[0m")
        ).mode
        == "chatgpt_oauth"
    )


def test_native_commands_delegate_not_custom_oauth():
    assert native_auth_command("codex", "login", executable="codex")[-2:] == [
        "login",
        "--device-auth",
    ]
    assert native_auth_command(
        "claude-code", "login", executable="claude", mode="claude_setup_token"
    ) == ["claude", "setup-token"]
    command = native_auth_command("opencode", "login", executable="opencode")
    assert command[-4:] == [
        "--provider",
        "openai",
        "--method",
        "ChatGPT Pro/Plus (headless)",
    ]
    assert native_auth_command("pi", "refresh", executable="node")[-1] == "refresh"
    for harness in ("codex", "opencode"):
        with pytest.raises(AuthError, match="request"):
            native_auth_command(harness, "refresh", executable=harness)


def test_versions_fail_closed():
    verify_native_version("codex", b"codex-cli 0.154.0\n")
    with pytest.raises(AuthError):
        verify_native_version("codex", b"codex-cli 0.114.0")


def test_environment_scopes_native_files_and_disables_stale_content(tmp_path):
    tmp_path.chmod(0o700)
    env = isolated_auth_environment(
        "opencode",
        tmp_path,
        base={
            "PATH": "/usr/bin",
            "OPENCODE_AUTH_CONTENT": "SYNTHETIC_OLD_STATE",
            "OPENAI_API_KEY": "SYNTHETIC_KEY",
            "HOME": "/nonexistent-global-home",
            "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_STORAGE_KEY",
        },
    )
    assert "OPENCODE_AUTH_CONTENT" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["XDG_DATA_HOME"] == str(tmp_path / "data")


def test_artifact_exclusion_checks_symlink_aliases(tmp_path):
    auth = tmp_path / "authority"
    auth.mkdir(mode=0o700)
    alias = tmp_path / "artifact-alias"
    alias.symlink_to(auth, target_is_directory=True)
    for root in (tmp_path, auth, alias):
        with pytest.raises(AuthStateError, match="artifacts"):
            assert_auth_outside_artifacts(auth, [root])
    assert_auth_outside_artifacts(auth, [tmp_path / "real-artifacts"])


# All credentials are obvious synthetic strings; this native-shaped subprocess
# fixture never opens an account or network connection.
STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.154.0")
    sys.exit(0)
path = pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json"
if "--with-api-key" in args:
    path.write_text(json.dumps({"OPENAI_API_KEY": sys.stdin.read().strip()}))
    path.chmod(0o600)
elif args[-2:] == ["login", "status"]:
    data = json.loads(path.read_text())
    method = "an API key" if data.get("OPENAI_API_KEY") else "ChatGPT"
    print("Logged in using " + method)
elif args[-1:] == ["logout"]:
    path.unlink()
elif args[0] in {"rotate", "fail", "hang", "crash"}:
    data = json.loads(path.read_text())
    data["tokens"]["access_token"] = "SYNTHETIC_ROTATED"
    path.write_text(json.dumps(data))
    if args[0] == "crash":
        os.kill(os.getpid(), 9)
    if args[0] == "fail":
        print("SYNTHETIC_SECRET_IN_NATIVE_ERROR")
        sys.exit(5)
    if args[0] == "hang":
        time.sleep(60)
"""


@pytest.fixture
def setup(tmp_path):
    executable = tmp_path / "codex-stub"
    executable.write_text(STUB)
    executable.chmod(0o700)
    store = LocalSessionStore(
        tmp_path / "authority", binding="test-local", local_filesystem=True
    )
    ref = NativeAuthReference(profile="test", binding="test-local", generation=1)
    seed_session(store, ref, "codex", native_bytes())
    spec = AuthSpec(mode="chatgpt_oauth", reference=ref)
    options = dict(
        executable=str(executable),
        runtime_parent=tmp_path / "runtime",
        environment={"PATH": os.defpath},
        artifact_roots=[tmp_path / "artifacts"],
        store=store,
    )
    return spec, options


@pytest.mark.parametrize("action", ["rotate", "fail"])
def test_native_refresh_writeback_on_success_and_ordinary_error(setup, action):
    spec, options = setup
    with auth_session("codex", spec, **options) as runtime:
        root = runtime.root
        result = runtime.run([runtime.executable, action])
        assert result.returncode == (5 if action == "fail" else 0)
    assert not root.exists()
    current = options["store"].read("test")
    assert current.state.phase == "ready"
    assert b"SYNTHETIC_ROTATED" in current.state.native
    with auth_session("codex", spec, **options) as successor:
        assert b"SYNTHETIC_ROTATED" in successor.credential_path.read_bytes()


def test_exception_in_caller_still_saves_native_refresh(setup):
    spec, options = setup
    with pytest.raises(ValueError, match="application failed"):
        with auth_session("codex", spec, **options) as runtime:
            runtime.run([runtime.executable, "rotate"])
            raise ValueError("application failed")
    assert b"SYNTHETIC_ROTATED" in options["store"].read("test").state.native


def test_timeout_cleans_runtime_preserves_observed_state_but_blocks_reuse(setup):
    spec, options = setup
    with pytest.raises(AuthStateError, match="ambiguous"):
        with auth_session("codex", spec, **options) as runtime:
            root = runtime.root
            runtime.run([runtime.executable, "hang"], timeout=0.3)
    assert not root.exists()
    assert options["store"].read("test").state.phase == "claimed"
    assert b"SYNTHETIC_ROTATED" in options["store"].read("test").state.native
    with pytest.raises(AuthBusyError):
        claim_session(options["store"], spec.reference, "codex")


def test_native_crash_even_with_valid_file_cannot_release_old_lineage(setup):
    spec, options = setup
    with pytest.raises(AuthStateError, match="ambiguous"):
        with auth_session("codex", spec, **options) as runtime:
            assert runtime.run([runtime.executable, "crash"]).returncode == -9
    assert options["store"].read("test").state.phase == "claimed"
    with pytest.raises(AuthBusyError):
        claim_session(options["store"], spec.reference, "codex")


def test_handoff_requires_real_child_stop_and_writeback(setup):
    spec, options = setup
    with pytest.raises(AuthStateError, match="ambiguous"):
        with auth_session("codex", spec, **options) as runtime:
            runtime.handoff()
            with pytest.raises(AuthError, match="stopped"):
                runtime.consumer_stopped(
                    prove_stopped=lambda: False,
                    refreshed_native=native_bytes(),
                    refresh_outcome_known=True,
                )
    assert options["store"].read("test").state.phase == "claimed"


def test_handoff_completed_preserves_collected_native_bytes(setup):
    spec, options = setup
    with auth_session("codex", spec, **options) as runtime:
        runtime.handoff()
        runtime.consumer_stopped(
            prove_stopped=lambda: True,
            refreshed_native=native_bytes(marker="SYNTHETIC_CHILD_REFRESH"),
            refresh_outcome_known=True,
        )
    assert b"SYNTHETIC_CHILD_REFRESH" in options["store"].read("test").state.native


def test_api_key_runtime_is_native_and_does_not_claim_subscription_authority(setup):
    _, options = setup
    spec = AuthSpec(mode="api_key", reference=EnvAuthReference(name="MY_KEY"))
    options["environment"] = {"PATH": os.defpath, "MY_KEY": "SYNTHETIC_API_KEY"}
    with auth_session("codex", spec, **options) as runtime:
        assert runtime.status().mode == "api_key"
        assert runtime.claim is None
        assert "OPENAI_API_KEY" not in runtime.environment
        assert "SYNTHETIC" not in repr(runtime)
    assert options["store"].read("test").state.revision == 0


def test_offline_profile_status_no_commands_or_refresh(setup, monkeypatch):
    spec, options = setup

    def forbidden(*args, **kwargs):
        pytest.fail("status must not perform native login/refresh")

    monkeypatch.setattr("tetrabench.auth.run_native", forbidden)
    status = auth_status("codex", spec, store=options["store"])
    assert status.state == "ready"
    assert status.native is not None
    assert status.native.source == "native_store"
    assert status.account_verified is False
    assert options["store"].read("test").state.revision == 0


def test_native_process_output_is_bounded(tmp_path):
    with pytest.raises(AuthError, match="capture limit"):
        run_native(
            [sys.executable, "-c", "print('SYNTHETIC' * 10000)"],
            environment={"PATH": os.defpath},
            cwd=tmp_path,
        )


def test_native_stdin_backpressure_cannot_bypass_timeout(tmp_path):
    with pytest.raises(AuthError, match="timed out"):
        run_native(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=b"S" * 128 * 1024,
            timeout=0.1,
            environment={"PATH": os.defpath},
            cwd=tmp_path,
        )


def test_native_logout_is_profile_scoped_and_idempotent(setup):
    spec, options = setup
    status = auth_logout("codex", spec, **options)
    assert status.state == "logged_out"
    assert options["store"].read("test").state.native == b""
    assert auth_logout("codex", spec, **options) == status


@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
def test_login_uses_native_bootstrap_and_only_publishes_native_auth(
    harness,
    tmp_path,
    monkeypatch,
):
    native = native_bytes(harness)
    events = []

    def fake_native(argv, *, environment, cwd, **kwargs):
        events.append(argv[-1])
        if argv[-1] == "--version":
            version = {"codex": "0.154.0", "opencode": "1.18.30"}[harness]
            return NativeResult(0, version.encode())
        if "login" in argv and "status" not in argv:
            assert kwargs["interactive"] is True
            path = (
                cwd / "data/opencode/auth.json"
                if harness == "opencode"
                else cwd / "native/auth.json"
            )
            write_private(path, native)
            (cwd / "unrelated-transcript").write_text("SYNTHETIC_NOT_AUTH")
            return NativeResult(0)
        output = {
            "codex": b"Logged in using ChatGPT",
            "opencode": b"OpenAI oauth",
            "pi": b'{"type":"oauth"}',
        }[harness]
        return NativeResult(0, output)

    monkeypatch.setattr("tetrabench.auth.run_native", fake_native)
    store = LocalSessionStore(
        tmp_path / "authority", binding="local", local_filesystem=True
    )
    reference = NativeAuthReference(profile="fresh", binding="local", generation=1)
    spec = AuthSpec(mode="chatgpt_oauth", reference=reference)
    module = tmp_path / "fake-package/dist/index.js"
    module.parent.mkdir(parents=True)
    module.write_text("// synthetic package marker")
    (module.parent.parent / "package.json").write_text(
        json.dumps(
            {
                "name": "@earendil-works/pi-coding-agent",
                "version": "0.85.1",
            }
        )
    )
    options: dict[str, Any] = dict(
        executable="synthetic-native",
        runtime_parent=tmp_path / "runtime",
        environment={},
        artifact_roots=[tmp_path / "artifacts"],
        store=store,
        pi_module=module if harness == "pi" else None,
    )
    status = auth_login(harness, spec, **options)
    assert status.state == "ready"
    current = store.read("fresh")
    assert current is not None and current.state.native == native
    assert b"SYNTHETIC_NOT_AUTH" not in read_private(store.root / "fresh.json")
    before = len(events)
    with pytest.raises(AuthStateError, match="next credential generation"):
        auth_login(harness, spec, **options)
    assert len(events) == before
    native = native_bytes(harness, marker="SYNTHETIC_FRESH_LOGIN_TWO")
    next_spec = AuthSpec(
        mode="chatgpt_oauth", reference=reference.model_copy(update={"generation": 2})
    )
    assert auth_reseed(harness, next_spec, **options).generation == 2
    current = store.read("fresh")
    assert current is not None and current.state.native == native


def test_setup_token_never_imports_or_persists_a_global_keychain(tmp_path, monkeypatch):
    def fake_native(argv, **kwargs):
        if argv[-1] == "--version":
            return NativeResult(0, b"2.1.267 (Claude Code)")
        assert argv[-1] == "setup-token"
        assert kwargs["interactive"] is True
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["environment"]
        return NativeResult(0)

    monkeypatch.setattr("tetrabench.auth.run_native", fake_native)
    spec = AuthSpec(
        mode="claude_setup_token", reference=EnvAuthReference(name="USER_TOKEN")
    )
    status = auth_login(
        "claude-code",
        spec,
        executable="synthetic-native",
        runtime_parent=tmp_path / "runtime",
        environment={},
        artifact_roots=[tmp_path / "artifacts"],
    )
    assert status.state == "setup_required"
    assert list((tmp_path / "runtime").iterdir()) == []
