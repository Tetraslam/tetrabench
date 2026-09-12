"""Public metadata commands acquire only explicit auth and retain no key bytes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tetrabench.auth import NativeRuntime
from tetrabench.auth_config import (
    AuthSpec,
    EnvAuthReference,
    credential_env_name,
)
from tetrabench.auth_sessions import (
    read_private,
    write_private,
)
from tetrabench.cli import app
from tetrabench.harness_config import HarnessConfig
from tetrabench.native_discovery import NativeInstallation, collect_installed
from tetrabench.nativeauth import NativeResult

SECRET = 'SYNTHETIC_METADATA_KEY_"quoted"'
VERSIONS = {
    "codex": "0.154.0",
    "claude-code": "2.1.267",
    "opencode": "1.18.30",
    "pi": "0.85.1",
}


@pytest.fixture(autouse=True)
def no_ambient_auth(monkeypatch):
    from tetrabench.auth_config import authentication_environment_names

    for name in authentication_environment_names():
        monkeypatch.delenv(name, raising=False)


def configure(tmp_path, name="codex", *, mode="api_key"):
    model = (
        "anthropic/claude-opus-5[1m]"
        if name == "claude-code"
        else "openrouter/openai/model"
        if name in {"opencode", "pi"} and mode == "api_key"
        else "openai/model"
    )
    spec = HarnessConfig(
        name=name,
        version=VERSIONS[name],
        model=model,
        auth=AuthSpec(
            mode=mode, reference=EnvAuthReference(name="SELECTED_METADATA_KEY")
        ),
    )
    file = tmp_path / "run.toml"
    file.write_text(
        f'[harness]\nname="{name}"\nversion="{spec.version}"\nmodel="{model}"\n[harness.auth]\nmode="{mode}"\n[harness.auth.reference]\nkind="env"\nname="SELECTED_METADATA_KEY"\n'
    )
    return spec, file


def fake_native(monkeypatch, tmp_path, name, *, mode="api_key"):
    package = tmp_path / "pi-package"
    (package / "dist").mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(
        '{"name":"@earendil-works/pi-coding-agent","version":"0.85.1"}'
    )
    installation = NativeInstallation(
        ("/fixture/" + name,), "/fixture/node", package if name == "pi" else None
    )
    monkeypatch.setattr(
        "tetrabench.native_discovery.native_installation", lambda *a, **k: installation
    )
    monkeypatch.setattr(
        "tetrabench.discovery_auth.user_runtime_path", lambda _: tmp_path / "runtime"
    )
    calls = []

    def run(argv, *, environment, cwd, stdin=None, **kwargs):
        assert SECRET not in repr(argv)
        calls.append((argv, dict(environment)))
        if argv[-1] == "--version":
            return NativeResult(0, VERSIONS[name].encode())
        if "--with-api-key" in argv:
            assert stdin == (SECRET + "\n").encode()
            write_private(
                Path(environment["CODEX_HOME"]) / "auth.json",
                json.dumps({"OPENAI_API_KEY": SECRET}).encode(),
            )
            return NativeResult(0, b"")
        if name == "codex":
            return NativeResult(
                0,
                b"Logged in using ChatGPT"
                if mode == "chatgpt_oauth"
                else b"Logged in using an API key - ***",
            )
        if name == "claude-code":
            return NativeResult(
                0,
                json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "oauth_token"
                        if mode == "claude_setup_token"
                        else "api_key",
                    }
                ).encode(),
            )
        if name == "opencode":
            return NativeResult(
                0,
                b"OpenAI oauth"
                if mode == "chatgpt_oauth"
                else b"Environment OpenRouter OPENROUTER_API_KEY",
            )
        return NativeResult(
            0, b'{"type":"oauth"}' if mode == "chatgpt_oauth" else b'{"type":"api_key"}'
        )

    monkeypatch.setattr("tetrabench.auth.run_native", run)
    return calls


@pytest.mark.parametrize("name", VERSIONS)
def test_public_inspect_supplies_actual_runtime_and_observed_mode(
    tmp_path, monkeypatch, name
):
    spec, file = configure(tmp_path, name)
    monkeypatch.setenv("SELECTED_METADATA_KEY", SECRET)
    calls = fake_native(monkeypatch, tmp_path, name)
    monkeypatch.setattr(
        "tetrabench.auth_profiles.load_auth_config_file",
        lambda *a, **k: pytest.fail("API key required an OAuth profile"),
    )

    def inspect(config, **options):
        runtime = options["runtime"]
        assert isinstance(runtime, NativeRuntime)
        assert runtime.last_auth_status.mode == spec.auth.mode
        assert runtime.model == spec.model
        assert options["allow_authenticated_read"] is True
        assert options["reuse_native_cache"] is False
        if name == "codex":
            assert (
                json.loads(read_private(runtime.credential_path))["OPENAI_API_KEY"]
                == SECRET
            )
        else:
            assert (
                runtime.environment[
                    credential_env_name(name, spec.auth.mode, model=spec.model)
                ]
                == SECRET
            )
        return {
            "capability": {"status": "supported", "controls": [], "limitations": []},
            "inference_validated": False,
        }

    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = CliRunner().invoke(
        app,
        [
            "models",
            "inspect",
            "--harness",
            str(file),
            "--allow-authenticated-read",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["authentication"]
    assert report["provided"] is True and report["observed"]["mode"] == "api_key"
    assert report["provider_metadata_fetch"] == "not-observed"
    assert report["account_capability_checked"] is False
    assert SECRET not in result.output and "SYNTHETIC_METADATA_KEY" not in result.output
    assert calls


def test_default_inspection_does_not_read_auth_or_execute_helpers(
    tmp_path, monkeypatch
):
    _, file = configure(tmp_path)
    monkeypatch.setattr(
        "tetrabench.discovery_auth.auth_session",
        lambda *a, **k: pytest.fail("auth acquired offline"),
    )

    def inspect(config, **options):
        assert "runtime" not in options and "allow_authenticated_read" not in options
        return {
            "capability": {"status": "supported", "controls": [], "limitations": []}
        }

    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = CliRunner().invoke(
        app, ["models", "inspect", "--harness", str(file), "--json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["authentication"]["provided"] is False


def test_explicit_missing_key_refuses_before_profile_lookup_or_collection(
    tmp_path, monkeypatch
):
    _, file = configure(tmp_path)
    monkeypatch.delenv("SELECTED_METADATA_KEY", raising=False)
    monkeypatch.setattr(
        "tetrabench.auth_profiles.load_auth_config_file",
        lambda *a, **k: pytest.fail("profile read"),
    )
    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler",
        lambda *a, **k: pytest.fail("collection started"),
    )
    result = CliRunner().invoke(
        app,
        [
            "models",
            "inspect",
            "--harness",
            str(file),
            "--allow-authenticated-read",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "SELECTED_METADATA_KEY" in result.output


def test_metadata_echo_of_selected_key_never_reaches_output(tmp_path, monkeypatch):
    _, file = configure(tmp_path, "opencode")
    fake_native(monkeypatch, tmp_path, "opencode")
    monkeypatch.setenv("SELECTED_METADATA_KEY", SECRET)
    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler",
        lambda *a, **k: {"description": SECRET},
    )
    result = CliRunner().invoke(
        app,
        [
            "models",
            "inspect",
            "--harness",
            str(file),
            "--allow-authenticated-read",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "SYNTHETIC_METADATA_KEY" not in result.output


@pytest.mark.parametrize("mutate_resource", [False, True])
def test_adoption_uses_same_runtime_then_checks_resource_drift_before_write(
    tmp_path, monkeypatch, mutate_resource
):
    from test_models_cli import snapshot

    _, file = configure(tmp_path)
    fake_native(monkeypatch, tmp_path, "codex")
    monkeypatch.setenv("SELECTED_METADATA_KEY", SECRET)
    resource = tmp_path / "rules.md"
    resource.write_text("original")
    with file.open("a") as stream:
        stream.write(
            '[[harness.resources]]\nsource="rules.md"\ndestination="rules.md"\n'
        )
    before = file.read_bytes()

    def collect(config, **options):
        assert isinstance(options["runtime"], NativeRuntime)
        result = snapshot(config)
        result = result.model_copy(
            update={
                "identity": result.identity.model_copy(
                    update={
                        "auth_mode": "api_key",
                        "profile_ref": "env:SELECTED_METADATA_KEY",
                    }
                )
            }
        )
        if mutate_resource:
            resource.write_text("changed during collection")
        return result

    monkeypatch.setattr("tetrabench.native_discovery.collect_installed", collect)
    result = CliRunner().invoke(
        app,
        [
            "models",
            "adopt",
            "--harness",
            str(file),
            "--allow-authenticated-read",
            "--control",
            "effort",
            "--select",
            "high",
            "--write",
            "--json",
        ],
    )
    assert result.exit_code == (2 if mutate_resource else 0), result.output
    if mutate_resource:
        assert "resources changed" in result.output
        assert file.read_bytes() == before
    else:
        assert json.loads(result.stdout)["authentication"]["provided"] is True
        assert "SYNTHETIC_METADATA_KEY" not in file.read_text()


def test_collector_refuses_auth_permission_without_native_runtime(tmp_path):
    spec, _ = configure(tmp_path)
    with pytest.raises(ValueError, match="NativeRuntime"):
        collect_installed(spec, base=tmp_path, allow_authenticated_read=True)


def test_claude_setup_token_metadata_has_no_refresh_claim_or_profile(
    tmp_path, monkeypatch
):
    spec, _ = configure(tmp_path, "claude-code", mode="claude_setup_token")
    fake_native(monkeypatch, tmp_path, "claude-code", mode="claude_setup_token")
    monkeypatch.setenv("SELECTED_METADATA_KEY", SECRET)
    monkeypatch.setattr(
        "tetrabench.auth_profiles.load_auth_config_file",
        lambda *a, **k: pytest.fail("setup token loaded OAuth backend"),
    )
    from tetrabench.discovery_auth import metadata_auth_context

    with metadata_auth_context(spec, allow_authenticated_read=True) as runtime:
        assert runtime is not None and runtime.claim is None
        assert runtime.last_auth_status is not None
        assert runtime.last_auth_status.mode == "claude_setup_token"


def oauth_setup(tmp_path, monkeypatch):
    from tetrabench.auth_config import NativeAuthReference
    from tetrabench.auth_sessions import LocalSessionStore, seed_session

    fake_native(monkeypatch, tmp_path, "opencode", mode="chatgpt_oauth")
    ref = NativeAuthReference(profile="metadata", binding="local", generation=1)
    store = LocalSessionStore(
        tmp_path / "state", binding="local", local_filesystem=True
    )

    def native(access):
        return json.dumps(
            {
                "openai": {
                    "type": "oauth",
                    "access": access,
                    "refresh": "SYNTHETIC_REFRESH",
                    "expires": 4000000000000,
                }
            }
        ).encode()

    seed_session(store, ref, "opencode", native("SYNTHETIC_INITIAL"))
    path = tmp_path / "run.toml"
    path.write_text(
        '[harness]\nname="opencode"\nversion="1.18.30"\nmodel="openai/model"\n[harness.auth]\nmode="chatgpt_oauth"\n[harness.auth.reference]\nkind="native_session"\nprofile="metadata"\nbinding="local"\ngeneration=1\n'
    )
    private = tmp_path / "auth.toml"
    private.write_text(
        f'schema_version=1\nruntime_directory="{tmp_path / "runtime"}"\n'
        '[profiles.metadata]\nharness="opencode"\nbinding="local"\ngeneration=1\n'
        '[profiles.metadata.backend]\nkind="local"\napproved_private_backend=true\n'
        f'state_directory="{tmp_path / "state"}"\n'
    )
    private.chmod(0o600)
    return store, path, private, native


def test_oauth_metadata_has_private_operation_owner_and_native_writeback(
    tmp_path, monkeypatch
):
    store, file, private, native = oauth_setup(tmp_path, monkeypatch)
    owners = []

    def inspect(config, **options):
        runtime = options["runtime"]
        current = store.read("metadata")
        assert current is not None and current.state.phase == "claimed"
        owners.append(current.state.owner)
        assert runtime.claim is not None
        write_private(runtime.credential_path, native("SYNTHETIC_ROTATED"))
        return {
            "capability": {"status": "supported", "controls": [], "limitations": []}
        }

    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = CliRunner().invoke(
        app,
        [
            "models",
            "inspect",
            "--harness",
            str(file),
            "--allow-authenticated-read",
            "--auth-config",
            str(private),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert owners[0].startswith("cli-metadata-")
    state = store.read("metadata")
    assert state is not None and state.state.phase == "ready"
    assert state.state.native == native("SYNTHETIC_ROTATED")
    operation = json.loads(
        (tmp_path / "runtime/operations" / (owners[0] + ".json")).read_text()
    )
    assert (
        operation["operation"] == "native-metadata" and operation["state"] == "stopped"
    )
    assert "SYNTHETIC" not in result.output


def test_oauth_metadata_crash_blocks_the_lineage_with_recoverable_owner(
    tmp_path, monkeypatch
):
    from tetrabench.auth_sessions import AuthError

    store, file, private, _ = oauth_setup(tmp_path, monkeypatch)
    base_run = __import__("tetrabench.auth", fromlist=["run_native"]).run_native

    def run(argv, **kwargs):
        if argv == ["/fixture/crash"]:
            raise AuthError("native metadata process timed out")
        return base_run(argv, **kwargs)

    monkeypatch.setattr("tetrabench.auth.run_native", run)

    def inspect(config, **options):
        options["runtime"].run(["/fixture/crash"])

    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = CliRunner().invoke(
        app,
        [
            "models",
            "inspect",
            "--harness",
            str(file),
            "--allow-authenticated-read",
            "--auth-config",
            str(private),
            "--json",
        ],
    )
    assert result.exit_code == 2
    state = store.read("metadata")
    assert state is not None and state.state.phase == "claimed"
    assert state.state.owner.startswith("cli-metadata-")
    operation = json.loads(
        (tmp_path / "runtime/operations" / (state.state.owner + ".json")).read_text()
    )
    assert operation["state"] == "ambiguous"
    assert "SYNTHETIC" not in result.output


@pytest.mark.native
@pytest.mark.parametrize("action", ["inspect", "adopt"])
def test_public_authenticated_codex_cli_can_use_bundled_metadata_without_network(
    tmp_path,
    action,
):
    import sys

    from native_consumer_support import native_environment, native_modules, native_run

    from tetrabench.native_control import ControlProcess

    modules = native_modules(required=True)
    assert modules is not None
    (tmp_path / "catalog").mkdir()
    catalog_env = native_environment(tmp_path / "catalog")
    catalog_env["OPENAI_API_KEY"] = "SYNTHETIC_NOT_A_LIVE_KEY"
    with ControlProcess(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "node",
            str(modules / "@openai/codex/bin/codex.js"),
            "app-server",
        ],
        tmp_path,
        catalog_env,
    ) as process:
        process.rpc(
            "initialize", {"clientInfo": {"name": "metadata-test", "version": "1"}}, 1
        )
        process.send({"method": "initialized", "params": {}})
        rows = process.rpc("model/list", {"limit": 100, "includeHidden": True}, 2)[
            "data"
        ]
    selected = "gpt-6-astra"
    assert any(row["model"] == selected for row in rows)
    _, path = configure(tmp_path)
    path.write_text(
        path.read_text().replace('model="openai/model"', f'model="openai/{selected}"')
    )
    arguments = [
        "models",
        action,
        "--harness",
        str(path),
        "--native-modules",
        str(modules),
        "--allow-authenticated-read",
        "--json",
    ]
    if action == "adopt":
        arguments.extend(["--control", "effort", "--select", "high"])
    before = path.read_bytes()
    program = (
        "import tetrabench.auth as auth; native=auth.run_native;"
        "auth.run_native=lambda argv,**kw: native(['unshare','--user',"
        "'--map-root-user','--net',*argv],**kw);"
        "from typer.testing import CliRunner; from tetrabench.cli import app; "
        f"r=CliRunner().invoke(app, {arguments!r});"
        "print(r.stdout if r.exit_code == 0 else r.output);"
        "raise SystemExit(r.exit_code)"
    )
    result = native_run(
        [
            sys.executable,
            "-c",
            program,
        ],
        tmp_path,
        {
            "SELECTED_METADATA_KEY": "SYNTHETIC_NOT_A_LIVE_KEY",
            "XDG_RUNTIME_DIR": str(tmp_path / "private-runtime"),
            "CODEX_HOME": "",
            "CLAUDE_CONFIG_DIR": "",
            "PI_CODING_AGENT_DIR": "",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["authentication"]["provided"] is True
    assert report["authentication"]["observed"]["mode"] == "api_key"
    assert report["authentication"]["provider_metadata_fetch"] == "not-observed"
    assert report["authentication"]["account_capability_checked"] is False
    if action == "adopt":
        assert report["written"] is False
        assert path.read_bytes() == before
        assert 'reasoning_effort = "high"' in report["diff"]
        assert "native-version-default" in report["diff"]
        assert "bundled-native-catalog" in report["diff"]
        assert "SYNTHETIC_NOT_A_LIVE_KEY" not in result.stdout
        return
    capability = report["capability"]
    assert capability["status"] == "supported"
    identity = capability["identity"]
    assert identity["route_status"] == "bound"
    assert identity["provider_id"] == "openai"
    assert identity["protocol"] == "responses"
    assert identity["endpoints"] == ["https://api.openai.com/v1"]
    assert identity["requested_model"] == "openai/gpt-6-astra"
    assert identity["resolved_model"] == "gpt-6-astra"
    assert identity["auth_mode"] == "api_key"
    assert json.loads(capability["metadata_json"])["route_provenance"]["kind"] == (
        "native-version-default"
    )
    assert (
        json.loads(capability["metadata_json"])["catalog_origin"]
        == "bundled-native-catalog"
    )
    assert "SYNTHETIC_NOT_A_LIVE_KEY" not in result.stdout


def test_oauth_collector_refuses_unproven_inner_consumer_completion(
    tmp_path, monkeypatch
):
    from tetrabench.auth_sessions import AuthError
    from tetrabench.config import load_harness_override
    from tetrabench.discovery_auth import metadata_auth_context

    store, file, private, _ = oauth_setup(tmp_path, monkeypatch)
    config = load_harness_override(file)
    base_run = __import__("tetrabench.auth", fromlist=["run_native"]).run_native

    def run(argv, **kwargs):
        if "tetrabench.native_discovery_worker" in argv:
            assert kwargs["environment"]["XDG_DATA_HOME"]
            return NativeResult(
                0,
                b'{"auth_custody":{"schema_version":1,"credential_processes":1,"graceful_credential_processes":0}}',
            )
        return base_run(argv, **kwargs)

    monkeypatch.setattr("tetrabench.auth.run_native", run)
    with pytest.raises(AuthError):
        with metadata_auth_context(
            config, allow_authenticated_read=True, auth_config=private
        ) as runtime:
            collect_installed(
                config, base=tmp_path, runtime=runtime, allow_authenticated_read=True
            )
    state = store.read("metadata")
    assert state is not None and state.state.phase == "claimed"
