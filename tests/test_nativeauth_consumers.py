"""Opt-in exact installed native consumers; synthetic credentials, no accounts.

Uses the shared locked native-consumer installation. These tests do not install
packages, login to accounts, invoke models, or read ambient credentials.
"""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

import pytest
from native_consumer_support import native_environment, native_modules

from tetrabench.auth import NativeRuntime, auth_logout, auth_session
from tetrabench.auth_config import AuthSpec, EnvAuthReference
from tetrabench.auth_sessions import private_directory, read_private, write_private
from tetrabench.nativeauth import (
    PI_AUTH_BRIDGE,
    isolated_auth_environment,
    native_auth_command,
    native_auth_path,
    parse_native_status,
    run_native,
)

pytestmark = pytest.mark.native


@pytest.fixture
def installed() -> Path:
    modules = native_modules(required=True)
    assert modules is not None
    return modules.parent


def native_env(root: Path, installed: Path, harness: str):
    private_directory(root, create=True)
    return isolated_auth_environment(
        harness,
        root,
        base=native_environment(root),
    )


@pytest.mark.parametrize(
    "harness,executable", [("codex", "codex"), ("claude-code", "claude")]
)
def test_real_native_api_key_status_only(installed, tmp_path, harness, executable):
    spec = AuthSpec(
        mode="api_key", reference=EnvAuthReference(name="SYNTHETIC_TEST_KEY")
    )
    with auth_session(
        harness,
        spec,
        executable=str(
            installed
            / "node_modules"
            / (
                "@anthropic-ai/claude-code-linux-x64/claude"
                if harness == "claude-code"
                else ".bin/codex"
            )
        ),
        runtime_parent=tmp_path / "private-runtime",
        artifact_roots=[tmp_path / "artifacts"],
        environment={
            "SYNTHETIC_TEST_KEY": "SYNTHETIC_NOT_AN_ACCOUNT_KEY",
            "PATH": native_environment(tmp_path)["PATH"],
        },
    ) as runtime:
        assert runtime.status().mode == "api_key"


def test_real_claude_setup_token_status_only(installed, tmp_path):
    spec = AuthSpec(
        mode="claude_setup_token", reference=EnvAuthReference(name="SYNTHETIC_TOKEN")
    )
    with auth_session(
        "claude-code",
        spec,
        executable=str(
            installed / "node_modules/@anthropic-ai/claude-code-linux-x64/claude"
        ),
        runtime_parent=tmp_path / "private-runtime",
        artifact_roots=[tmp_path / "artifacts"],
        environment={
            "SYNTHETIC_TOKEN": "SYNTHETIC_NOT_AN_ACCOUNT_TOKEN",
            "PATH": native_environment(tmp_path)["PATH"],
        },
    ) as runtime:
        assert runtime.status().mode == "claude_setup_token"


def test_real_opencode_native_file_status_and_logout(installed, tmp_path):
    env = native_env(tmp_path, installed, "opencode")
    path = native_auth_path("opencode", tmp_path)
    write_private(
        path,
        json.dumps(
            {
                "openai": {
                    "type": "oauth",
                    "access": "SYNTHETIC_ACCESS",
                    "refresh": "SYNTHETIC_REFRESH",
                    "expires": 4000000000000,
                    "accountId": "SYNTHETIC_ACCOUNT",
                }
            }
        ).encode(),
    )
    executable = str(installed / "node_modules/opencode-linux-x64/bin/opencode")
    result = run_native(
        native_auth_command("opencode", "status", executable=executable),
        environment=env,
        cwd=tmp_path,
    )
    assert parse_native_status("opencode", result).mode == "chatgpt_oauth"
    result = run_native(
        native_auth_command("opencode", "logout", executable=executable),
        environment=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert json.loads(read_private(path)) == {}


def test_real_codex_native_oauth_status_and_logout(installed, tmp_path):
    env = native_env(tmp_path, installed, "codex")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "email": "synthetic@example.invalid",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "SYNTHETIC_ACCOUNT",
                        "chatgpt_plan_type": "plus",
                    },
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    path = native_auth_path("codex", tmp_path)
    write_private(
        path,
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "e30." + payload + ".SYNTHETIC_SIGNATURE",
                    "access_token": "SYNTHETIC_ACCESS",
                    "refresh_token": "SYNTHETIC_REFRESH",
                    "account_id": "SYNTHETIC_ACCOUNT",
                },
            }
        ).encode(),
    )
    executable = str(installed / "node_modules/.bin/codex")
    result = run_native(
        native_auth_command("codex", "status", executable=executable),
        environment=env,
        cwd=tmp_path,
    )
    assert parse_native_status("codex", result).mode == "chatgpt_oauth"
    result = run_native(
        native_auth_command("codex", "logout", executable=executable),
        environment=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert not path.exists()


def test_real_codex_status_warning_and_private_tmp_sibling(installed, tmp_path):
    env = native_env(tmp_path, installed, "codex")
    write_private(
        native_auth_path("codex", tmp_path), b'{"OPENAI_API_KEY":"SYNTHETIC_API_KEY"}'
    )
    binary = installed / "node_modules/.bin/codex"
    wrapper = tmp_path / "codex-offline"
    wrapper.write_text(
        "#!/bin/sh\nexec "
        + shlex.join(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--net",
                "--pid",
                "--fork",
                "--kill-child",
                str(binary),
            ]
        )
        + ' "$@"\n'
    )
    wrapper.chmod(0o700)
    spec = AuthSpec(
        mode="api_key", reference=EnvAuthReference(name="UNUSED_SYNTHETIC_REFERENCE")
    )
    # Reproduce the retained live-failure shape: CODEX_HOME under temp_dir().
    env["TMPDIR"] = str(tmp_path)
    runtime = NativeRuntime("codex", spec, tmp_path, str(wrapper), env)
    result = runtime.run(
        native_auth_command("codex", "status", executable=str(wrapper))
    )
    assert result.returncode == 0 and result.stdout == b""
    assert result.stderr is not None
    assert result.stderr.startswith(
        b"WARNING: proceeding, even though we could not create PATH aliases:"
    )
    assert runtime.status().mode == "api_key"
    assert "SYNTHETIC" not in repr(runtime.status())
    # The owned layout fix removes the warning rather than suppressing stderr.
    env["TMPDIR"] = str(tmp_path / "tmp")
    fixed = runtime.run(native_auth_command("codex", "status", executable=str(wrapper)))
    assert fixed.returncode == 0
    assert fixed.stderr is not None and fixed.stderr.startswith(
        b"Logged in using an API key - "
    )
    assert b"PATH aliases" not in fixed.stderr
    assert parse_native_status("codex", fixed).mode == "api_key"


@pytest.mark.parametrize("harness", ["opencode", "pi"])
def test_real_native_alternative_api_key_status(installed, tmp_path, harness):
    executable = (
        str(installed / "node_modules/opencode-linux-x64/bin/opencode")
        if harness == "opencode"
        else "node"
    )
    module = installed / "node_modules/@earendil-works/pi-coding-agent/dist/index.js"
    spec = AuthSpec(mode="api_key", reference=EnvAuthReference(name="EVAL_KEY"))
    with auth_session(
        harness,
        spec,
        executable=executable,
        model="openrouter/openai/gpt-5",
        pi_module=module if harness == "pi" else None,
        runtime_parent=tmp_path / "runtime",
        artifact_roots=[],
        environment={
            "PATH": native_environment(tmp_path)["PATH"],
            "EVAL_KEY": "SYNTHETIC_NOT_AN_ACCOUNT_KEY",
        },
    ) as runtime:
        assert runtime.status().mode == "api_key"
        assert (
            runtime.environment["OPENROUTER_API_KEY"] == "SYNTHETIC_NOT_AN_ACCOUNT_KEY"
        )
        assert "OPENAI_API_KEY" not in runtime.environment


def test_real_opencode_logout_has_private_cli_owner_on_s3(installed, tmp_path):
    from test_nativeauth_s3 import FakePrivateS3, native, reference, store

    from tetrabench.auth_sessions import seed_session

    client = FakePrivateS3()
    authority = store(client)
    ref = reference()
    seed_session(authority, ref, "opencode", native())
    result = auth_logout(
        "opencode",
        AuthSpec(mode="chatgpt_oauth", reference=ref),
        store=authority,
        executable=str(installed / "node_modules/opencode-linux-x64/bin/opencode"),
        runtime_parent=tmp_path / "runtime",
        artifact_roots=[],
        environment={"PATH": native_environment(tmp_path)["PATH"]},
    )
    assert result.state == "logged_out"
    states = [json.loads(write["Body"]) for write in client.writes]
    claimed = next(state for state in states if state["phase"] == "claimed")
    assert claimed["owner"].startswith("cli-logout-")
    operation = json.loads(
        (tmp_path / "runtime/operations" / (claimed["owner"] + ".json")).read_bytes()
    )
    assert operation["state"] == "stopped"
    assert "SYNTHETIC_ACCESS" not in json.dumps(operation)
    assert states[-1]["phase"] == "logged_out"


def test_real_pi_exports_refresh_persist_and_next_process_reads(installed, tmp_path):
    env = native_env(tmp_path, installed, "pi")
    module = installed / "node_modules/@earendil-works/pi-coding-agent/dist/index.js"
    env["TETRABENCH_PI_MODULE"] = str(module)
    path = native_auth_path("pi", tmp_path)
    write_private(
        path,
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "SYNTHETIC_OLD_ACCESS",
                    "refresh": "SYNTHETIC_OLD_REFRESH",
                    "expires": 1,
                    "accountId": "SYNTHETIC_ACCOUNT",
                }
            }
        ).encode(),
    )
    payload = base64.b64encode(
        json.dumps(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "SYNTHETIC_ACCOUNT"
                },
            }
        ).encode()
    ).decode()
    access = "SYNTHETIC." + payload + ".SYNTHETIC_SIGNATURE"
    # Exercise the real provider's refresh implementation against an in-process
    # fake transport. Every actual network attempt is replaced before import.
    fake_transport = """
globalThis.fetch = async (url, init) => {
  if (String(url) !== 'https://auth.openai.com/oauth/token'
      || init.body.get('grant_type') !== 'refresh_token'
      || init.body.get('refresh_token') !== 'SYNTHETIC_OLD_REFRESH') throw new Error();
  return new Response(JSON.stringify(RESPONSE), {status: 200});
};
""".replace(
        "RESPONSE",
        json.dumps(
            {
                "access_token": access,
                "refresh_token": "SYNTHETIC_NATIVE_ROTATED_REFRESH",
                "expires_in": 3600,
            }
        ),
    )
    node = "node"
    result = run_native(
        [node, "--input-type=module", "-e", fake_transport + PI_AUTH_BRIDGE, "refresh"],
        environment=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert parse_native_status("pi", result).mode == "chatgpt_oauth"
    current = json.loads(read_private(path))["openai-codex"]
    assert current["refresh"] == "SYNTHETIC_NATIVE_ROTATED_REFRESH"
    assert current["access"] == access
    deny_network = (
        "globalThis.fetch = async () => {throw new Error('network forbidden');};\n"
    )
    result = run_native(
        [node, "--input-type=module", "-e", deny_network + PI_AUTH_BRIDGE, "status"],
        environment=env,
        cwd=tmp_path,
    )
    assert parse_native_status("pi", result).mode == "chatgpt_oauth"
