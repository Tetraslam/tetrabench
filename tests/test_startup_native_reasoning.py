"""Real startup control consumers, private homes, no model turns or live accounts."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit
from native_consumer_support import (
    VERSIONS,
    native_environment,
    native_modules,
    native_run,
)
from test_reasoning_installed import native_config

from tetrabench.canonical_json import sha256_hex
from tetrabench.harness_config import (
    HarnessConfig,
    NativeConfig,
    bind_capability_adoption,
    capability_config,
)
from tetrabench.harnesses import native_configuration_layers, seal_harness
from tetrabench.native_discovery import collect_installed
from tetrabench.runtime_startup import (
    startup_capability_assets,
    startup_capability_probe,
    validate_runtime_capability,
)

pytestmark = pytest.mark.native


@pytest.fixture
def installed():
    root = native_modules(required=True)
    assert root is not None
    return root


def command(modules, name):
    return {
        "opencode": (str(modules / "opencode-linux-x64/bin/opencode"),),
        "codex": ("node", str(modules / "@openai/codex/bin/codex.js")),
        "claude-code": (str(modules / "@anthropic-ai/claude-code-linux-x64/claude"),),
    }[name]


def setup(installed, tmp_path, name):
    config = native_config(name)
    if name == "claude-code":
        config = HarnessConfig.model_validate(
            config.model_dump()
            | {
                "native_config": NativeConfig(text='{"effortLevel":"low"}'),
                "discovery": "isolated",
            }
        )
    config = capability_config(seal_harness(config, tmp_path))
    snapshot = collect_installed(
        config, base=tmp_path, modules=installed, reuse_native_cache=False
    )
    assert snapshot.status == "supported", snapshot.limitations
    control, choice = (
        ("variant", "deliberate") if name == "opencode" else ("effort", "high")
    )
    bound = bind_capability_adoption(config, snapshot, control=control, select=choice)
    resolved = seal_harness(bound, tmp_path)
    env = native_environment(tmp_path)
    body = native_configuration_layers(resolved).main_config
    if name == "opencode":
        path = Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
        text = json.dumps(body)
    elif name == "codex":
        path = Path(env["CODEX_HOME"]) / "config.toml"
        text = tomlkit.dumps(body)
    else:
        path = tmp_path / "settings.json"
        text = json.dumps(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    probe = startup_capability_probe(
        resolved,
        native_files={str(path): sha256_hex(text.encode())},
        resource_root=str(tmp_path / "resources"),
        command=command(installed, name),
        settings_file=str(path) if name == "claude-code" else None,
    )
    probe["test_resolved_harness"] = resolved.model_dump(mode="json")
    return probe, env, path


def run_probe(tmp_path, probe, env):
    for name, content in startup_capability_assets(probe).items():
        (tmp_path / name).write_text(content)
    return native_run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--pid",
            "--fork",
            "--kill-child",
            "python3",
            str(tmp_path / "runtime_metadata.py"),
            str(tmp_path / "startup-probe.json"),
            "--isolated-network",
        ],
        tmp_path,
        env,
    )


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code"])
def test_actual_startup_sees_adopted_model_and_native_option(installed, tmp_path, name):
    probe, env, path = setup(installed, tmp_path, name)
    result = run_probe(tmp_path, probe, env)
    assert result.returncode == 0, (result.stdout, str(path))
    report = json.loads(result.stdout)
    assert "native_model" in report["verified_fields"], report
    assert "native_selection" in report["verified_fields"], report
    assert report["selected"] == probe["selected"]
    if name == "claude-code":
        assert report["observed_selection"] == probe["selected"]
        assert report["native_settings_effort"] == "low"
        assert "effective_selection" not in report["unverified_fields"]
        assert report["selection_source"] == "native get_settings.applied"
    else:
        assert report["observed_selection"] == probe["selected"], report
    assert report["inference_validated"] is False


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code"])
def test_actual_startup_refuses_changed_config_before_inference(
    installed, tmp_path, name
):
    probe, env, path = setup(installed, tmp_path, name)
    path.write_text(path.read_text() + "\n ")
    result = run_probe(tmp_path, probe, env)
    assert result.returncode == 78
    assert "drift" in result.stdout


def test_actual_opencode_changed_variant_meaning_is_rejected(installed, tmp_path):
    probe, env, path = setup(installed, tmp_path, "opencode")
    data = json.loads(path.read_text())
    data["provider"]["fixture"]["models"]["m"]["variants"]["deliberate"] = {
        "reasoningEffort": "low"
    }
    path.write_text(json.dumps(data))
    # Even a new file manifest cannot bless a different native meaning.
    probe["files"][0]["sha256"] = sha256_hex(path.read_bytes())
    result = run_probe(tmp_path, probe, env)
    assert result.returncode == 78


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code"])
@pytest.mark.parametrize("owned_metadata", [False, True])
def test_callable_execution_hook_runs_actual_native_consumer(
    installed, tmp_path, name, owned_metadata
):
    from tetrabench.harness_config import ResolvedHarness

    probe, env, path = setup(installed, tmp_path, name)
    resolved = ResolvedHarness.model_validate_json(
        json.dumps(probe.pop("test_resolved_harness"))
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for cli, binary in (
        ("opencode", installed / "opencode-linux-x64/bin/opencode"),
        ("claude", installed / "@anthropic-ai/claude-code-linux-x64/claude"),
    ):
        (binaries / cli).symlink_to(binary)
    env["PATH"] = str(binaries) + ":" + str(installed / ".bin") + ":" + env["PATH"]
    directory = tmp_path / "guard"
    directory.mkdir()
    if name == "opencode":
        path.write_text(path.read_text().rstrip("\n") + "\n")
    content = path.read_text().rstrip("\n")

    class Actor:
        _capability_verification = None
        harness = resolved
        _guard_directory = str(directory)
        _uploaded_native_files = (
            {} if name == "opencode" else {str(path): sha256_hex(path.read_bytes())}
        )
        _REMOTE_SETTINGS_PATH = path
        _runtime_auth_hook = None

        def _build_register_config_command(self):
            return "echo " + shlex.quote(content) + " > " + shlex.quote(str(path))

        async def _upload_config_text(
            self, environment, *, content, remote_path, filename
        ):
            Path(remote_path).write_text(content)

        async def exec_as_agent(
            self, environment, *, command, env=None, cwd=None, timeout_sec=None
        ):
            bootstrap = (
                "import fcntl,socket,struct,os,sys; "
                "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "fcntl.ioctl(s,0x8914,struct.pack('16sH14s',b'lo',1,b'')); "
                "os.execv('/bin/bash',['bash','-c',sys.argv[1]])"
            )
            result = native_run(
                [
                    "unshare",
                    "--user",
                    "--map-root-user",
                    "--net",
                    "--pid",
                    "--fork",
                    "--kill-child",
                    "python3",
                    "-c",
                    bootstrap,
                    command,
                ],
                tmp_path,
                env,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return SimpleNamespace(
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    actor = Actor()
    environment = object()
    owner_calls = []
    if owned_metadata:

        async def execute_metadata(actual_environment, command, **kwargs):
            assert actual_environment is environment
            assert kwargs["env"] is env
            owner_calls.append("metadata")
            return await actor.exec_as_agent(
                actual_environment, command=command, **kwargs
            )

        actor._runtime_auth_hook = SimpleNamespace(execute_metadata=execute_metadata)
    report = asyncio.run(
        validate_runtime_capability(actor, environment, env=env, cwd=str(tmp_path))
    )
    assert "native_selection" in report["verified_fields"], report
    assert report["selected"] == probe["selected"]
    assert report["metadata_complete"] is True
    assert owner_calls == (["metadata"] if owned_metadata else [])
    assert actor._capability_verification == report


def test_explicit_startup_policy_reports_undisclosed_fields(installed, tmp_path):
    probe, env, _ = setup(installed, tmp_path, "claude-code")
    probe["metadata_allowed"] = False
    report = json.loads(run_probe(tmp_path, probe, env).stdout)
    assert report["metadata_attempted"] is False
    assert "native_model" in report["unverified_fields"]
    assert "native_selection" not in report["verified_fields"]
    probe["policy"] = "strict-startup"
    assert run_probe(tmp_path, probe, env).returncode == 78


@pytest.mark.parametrize("selector", ["default", "claude-opus-5[1m]"])
def test_actual_claude_alias_selection_is_not_resolved_id_ambiguity(
    installed, tmp_path, selector
):
    config = HarnessConfig(
        name="claude-code",
        version=VERSIONS["claude-code"],
        model="anthropic/" + selector,
        discovery="isolated",
    )
    snapshot = collect_installed(
        config, base=tmp_path, modules=installed, reuse_native_cache=False
    )
    assert snapshot.status == "supported", snapshot.limitations
    bound = bind_capability_adoption(config, snapshot, control="effort", select="high")
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    probe = startup_capability_probe(
        seal_harness(bound, tmp_path),
        native_files={str(settings): sha256_hex(b"{}")},
        resource_root=str(tmp_path / "resources"),
        command=command(installed, "claude-code"),
        settings_file=str(settings),
    )
    result = run_probe(tmp_path, probe, native_environment(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert "native_model" in report["verified_fields"], report
    assert "native_selection" in report["verified_fields"], report


@pytest.mark.parametrize(
    "change", [None, "custom-incomplete", "custom-explicit", "auth"]
)
def test_codex_api_key_default_route_through_discovery_and_actual_startup(
    installed, tmp_path, monkeypatch, change
):
    from tetrabench import auth
    from tetrabench.auth_config import AuthSpec, EnvAuthReference, NativeAuthReference
    from tetrabench.capabilities import MetadataError
    from tetrabench.discovery_auth import metadata_auth_context
    from tetrabench.reasoning import validate_snapshot_for_harness

    config = HarnessConfig(
        name="codex",
        version="0.154.0",
        model="openai/gpt-6-astra",
        auth=AuthSpec(mode="api_key", reference=EnvAuthReference(name="TEST_KEY")),
    )
    monkeypatch.setattr(
        "tetrabench.discovery_auth.user_runtime_path", lambda _: tmp_path / "runtime"
    )
    run = auth.run_native

    def offline(argv, **kwargs):
        return run(["unshare", "--user", "--map-root-user", "--net", *argv], **kwargs)

    monkeypatch.setattr(auth, "run_native", offline)
    with metadata_auth_context(
        config,
        allow_authenticated_read=True,
        modules=installed,
        environment={"TEST_KEY": "SYNTHETIC_NOT_A_LIVE_KEY"},
    ) as runtime:
        assert runtime is not None
        captured = collect_installed(
            config,
            base=tmp_path,
            modules=installed,
            runtime=runtime,
            allow_authenticated_read=True,
            reuse_native_cache=False,
        )
        assert captured.status == "supported", captured.limitations
        assert captured.identity.protocol == "responses"
        assert captured.identity.endpoints == ("https://api.openai.com/v1",)
        metadata = json.loads(captured.metadata_json)
        assert metadata["route_provenance"]["kind"] == "native-version-default"
        assert metadata["catalog_origin"] == "bundled-native-catalog"
        assert metadata["authentication"]["account_verified"] is False
        bound = bind_capability_adoption(
            config, captured, control="effort", select="high"
        )
        resolved = seal_harness(bound, tmp_path)
        assert resolved.capability_snapshot is not None
        validate_snapshot_for_harness(resolved.capability_snapshot, resolved)
        changed_auth = bound.model_copy(
            update={
                "auth": AuthSpec(
                    mode="chatgpt_oauth",
                    reference=NativeAuthReference(
                        profile="unused", binding="unused", generation=1
                    ),
                )
            }
        )
        with pytest.raises(MetadataError, match="does not bind"):
            validate_snapshot_for_harness(resolved.capability_snapshot, changed_auth)
        path = Path(runtime.environment["CODEX_HOME"]) / "config.toml"
        path.write_text(
            tomlkit.dumps(native_configuration_layers(resolved).main_config)
        )
        probe = startup_capability_probe(
            resolved,
            native_files={str(path): sha256_hex(path.read_bytes())},
            resource_root=str(tmp_path / "resources"),
            command=command(installed, "codex"),
            policy="strict-startup",
        )
        if change and change.startswith("custom"):
            provider = {"name": "custom"}
            if change == "custom-explicit":
                provider.update(
                    base_url="https://example.test/v1", wire_api="responses"
                )
            path.write_text(
                tomlkit.dumps(
                    {
                        "model_provider": "custom",
                        "model_providers": {"custom": provider},
                    }
                )
            )
            # Rebinding file hashes cannot bless a changed or incomplete route.
            probe["files"][0]["sha256"] = sha256_hex(path.read_bytes())
        elif change == "auth":
            runtime.credential_path.unlink()
        result = run_probe(tmp_path, probe, runtime.environment)
        report = json.loads(result.stdout)
        if change:
            assert result.returncode == 78, report
            assert "drift" in report["error"] or "auth mode differs" in report["error"]
        else:
            assert result.returncode == 0, report
            assert {
                "native_provider",
                "protocol",
                "endpoint",
                "native_selection",
            }.issubset(report["verified_fields"])
            assert report["route_provenance"]["kind"] == "native-version-default"
            assert report["observed_selection"] == "high"
            assert report["inference_validated"] is False
            assert report["future_dispatch_verified"] is False
