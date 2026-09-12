"""Adapters verify native startup without modifying the published Pi entrypoint."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from native_consumer_support import native_environment, native_modules
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.auth_config import EnvAuthReference
from tetrabench.canonical_json import sha256_hex
from tetrabench.capabilities import (
    Binding,
    CapabilityIdentity,
    CapabilitySnapshot,
    CapabilitySnapshotRef,
    Choice,
    Control,
    Setting,
    make_evidence,
)
from tetrabench.harness_config import HarnessConfig, NativeConfig
from tetrabench.harnesses import compile_agent_config, seal_harness
from tetrabench.native_control import ControlProcess
from tetrabench.native_execution import native_shell_command
from tetrabench.reasoning import harness_config_digest


def startup_report(probe):
    return SimpleNamespace(
        return_code=0,
        stdout=json.dumps(
            {
                "scope": "native-startup",
                "snapshot_sha256": probe["snapshot_sha256"],
                "future_dispatch_verified": False,
                "inference_validated": False,
            }
        ),
        stderr="",
    )


def is_startup_probe_command(command: str) -> bool:
    tokens = shlex.split(command)
    return (
        len(tokens) >= 3
        and tokens[-3] == "python3"
        and Path(tokens[-2]).name == "runtime_metadata.py"
        and Path(tokens[-1]) == Path(tokens[-2]).with_name("startup-probe.json")
    )


@pytest.mark.parametrize("wrapped", [False, True])
def test_startup_fake_recognizes_helper_argv_independently_of_shell_prefix(wrapped):
    command = "python3 /fixture/runtime_metadata.py /fixture/startup-probe.json"
    if wrapped:
        command = native_shell_command(command)
    assert is_startup_probe_command(command)
    assert not is_startup_probe_command(
        "python3 /fixture/auth-io.py verify-helper /fixture/runtime_metadata.py digest"
    )


class StartupCaptureEnvironment(CaptureEnvironment):
    def probe(self):
        return json.loads(
            next(
                content
                for path, content in self.uploads.items()
                if path.endswith("/startup-probe.json")
            )
        )

    async def exec(self, **kwargs):
        if "runtime_metadata.py" in kwargs["command"]:
            self.commands.append(kwargs)
            return startup_report(self.probe())
        return await super().exec(**kwargs)


def adopted_pi(tmp_path, *, settings=True, source=None):
    source = source or HarnessConfig(
        name="pi",
        version="0.85.1",
        model="openai/model",
        options={"thinking": "high"},
        native_config=NativeConfig(
            text=json.dumps({"settings": {"compaction": {"enabled": True}}})
        )
        if settings
        else None,
    )
    provider, model = source.model.split("/", 1)
    capture = {
        "model": {
            "id": model,
            "provider": provider,
            "api": "openai-responses",
            "baseUrl": "https://example.test/v1",
            "reasoning": True,
            "maxTokens": 8192,
            "contextWindow": 32768,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "compat": {},
            "hasModelHeaders": False,
            "hasOpaqueMetadata": False,
        },
        "supportedThinkingLevels": ["off", "low", "medium", "high"],
        "normalizations": {"high": "high"},
    }
    identity = CapabilityIdentity(
        harness="pi",
        harness_version="0.85.1",
        native_adapter_version="0.85.1",
        requested_model=source.model,
        resolved_model=model,
        provider_id=provider,
        route_id="fixture",
        protocol="openai-responses",
        endpoints=("https://example.test/v1",),
        fallback_policy_json="{}",
        auth_mode=source.auth.mode if source.auth else "none",
        profile_ref=(
            "env:" + source.auth.reference.name
            if isinstance(source.auth.reference, EnvAuthReference)
            else source.auth.reference.profile
        )
        if source.auth
        else None,
        config_digest=harness_config_digest(source),
        route_status="bound",
    )
    snapshot = CapabilitySnapshot(
        identity=identity,
        status="supported",
        controls=(
            Control(
                name="thinking",
                kind="choices",
                status="supported",
                choices=(
                    Choice(
                        name="high",
                        settings=(
                            Setting(
                                binding=Binding(surface="options", path=("thinking",)),
                                value_json='"high"',
                            ),
                        ),
                        normalization="identity",
                    ),
                ),
                evidence=(
                    make_evidence(
                        "https://example.test/models",
                        "fixture native metadata",
                        "2026-09-10T00:00:00Z",
                        "native-model-list",
                    ),
                ),
            ),
        ),
        metadata_json=json.dumps({"runtime_capture": capture}),
    )
    bound = HarnessConfig.model_validate(
        source.model_dump(mode="python")
        | {"capability_snapshot": CapabilitySnapshotRef.from_snapshot(snapshot)}
    )
    config = compile_agent_config(seal_harness(bound, tmp_path))
    instance: Any = AgentFactory.create_agent_from_config(
        config, logs_dir=tmp_path / "logs"
    )
    return instance, config


def test_guard_binds_raw_uploaded_files_not_redacted_provenance(tmp_path, monkeypatch):
    instance, config = adopted_pi(tmp_path)
    original = config.model_dump_json()
    monkeypatch.setattr(instance, "_safe_text", lambda _text: "REDACTED PROVENANCE")
    environment = StartupCaptureEnvironment("pi", "0.85.1")
    asyncio.run(instance.run("not executed", environment, AgentContext()))
    invocation = next(
        row
        for row in environment.commands
        if "pi --print --mode json" in row["command"]
    )
    env = invocation["env"]
    probe = environment.probe()
    settings = "/tmp/harbor-pi-agent/settings.json"
    assert any(
        row["path"] == settings
        and row["sha256"] == sha256_hex(environment.uploads[settings])
        for row in probe["files"]
    )
    assert (
        settings.removesuffix("settings.json") + "models.json" in probe["absent_files"]
    )
    assert sha256_hex(b"REDACTED PROVENANCE") not in json.dumps(probe)
    assert "NODE_OPTIONS" not in env
    assert "TETRABENCH_PI_CAPABILITY_PROBE" not in env
    assert probe["command"] == ["pi"]
    assert config.model_dump_json() == original
    assert "NODE_OPTIONS" not in original and "CAPABILITY_PROBE" not in original
    assert {Path(path).name for path in environment.uploads} >= {
        "startup-probe.json",
        "native_control.py",
        "runtime_metadata.py",
    }
    assert instance._capability_verification["scope"] == "native-startup"
    assert instance._capability_verification["future_dispatch_verified"] is False


def test_absent_native_files_are_bound_in_a_private_state_directory(tmp_path):
    instance, _ = adopted_pi(tmp_path, settings=False)
    environment = StartupCaptureEnvironment("pi", "0.85.1")
    asyncio.run(instance.run("not executed", environment, AgentContext()))
    invocation = next(
        row
        for row in environment.commands
        if "pi --print --mode json" in row["command"]
    )
    env = invocation["env"]
    probe = environment.probe()
    assert probe["files"] == []
    assert set(probe["absent_files"]) == {
        env["PI_CODING_AGENT_DIR"] + "/" + name
        for name in ("settings.json", "models.json")
    }
    assert any(
        "mkdir -m 700 /tmp/tetrabench-capability-" in row["command"]
        for row in environment.commands
    )


def test_unadopted_pi_keeps_native_behavior_without_probe_or_preload(tmp_path):
    source = HarnessConfig(name="pi", version="0.85.1", model="openai/model")
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(source, tmp_path)), logs_dir=tmp_path / "logs"
    )
    environment = CaptureEnvironment("pi", "0.85.1")
    asyncio.run(instance.run("not executed", environment, AgentContext()))
    assert not any("capability" in path for path in environment.uploads)
    for row in environment.commands:
        assert "NODE_OPTIONS" not in row.get("env", {})
        assert "TETRABENCH_PI_CAPABILITY_PROBE" not in row.get("env", {})


def test_guard_binds_auth_private_config_copies_without_credential_bytes(
    tmp_path, monkeypatch
):
    from test_runtime_auth import FakeHarborEnvironment, new_scope

    from tetrabench.harness_config import capability_config

    scope, _, _ = new_scope(tmp_path, "pi", mode="api_key")
    source = capability_config(scope.harness).model_copy(
        update={
            "options": {"thinking": "high"},
            "native_config": NativeConfig(
                text='{"settings":{"compaction":{"enabled":true}}}'
            ),
        }
    )
    instance, config = adopted_pi(tmp_path, source=source)
    scope.harness = instance.harness
    monkeypatch.setattr(
        "tetrabench.runtime_auth.current_runtime_auth",
        lambda harness: scope.new_hook(harness),
    )

    class Environment(FakeHarborEnvironment):
        async def exec(self, command, env=None, **kwargs):
            if is_startup_probe_command(command):
                self.commands.append(command)
                probe = json.loads(
                    next(
                        content
                        for path, content in self.files.items()
                        if path.endswith("/startup-probe.json")
                    )
                )
                return startup_report(probe)
            if "auth-io.py pi-config " in command:
                root = shlex.split(command)[3]
                for name in ("settings.json", "models.json"):
                    source = "/tmp/harbor-pi-agent/" + name
                    if source in self.files:
                        self.files[root + "/pi/" + name] = self.files[source]
            return await super().exec(command, env=env, **kwargs)

    environment = Environment("pi")
    environment.mode = "api_key"

    async def run():
        await instance.run("SYNTHETIC_NATIVE_MODEL", environment, AgentContext())
        await environment.stop(delete=True)

    asyncio.run(run())
    scope.finalize(require_consumer=True)
    probe = json.loads(
        next(
            content
            for path, content in environment.files.items()
            if path.endswith("/startup-probe.json")
        )
    )
    target = scope.hooks[0].root + "/pi/settings.json"
    assert any(
        row["path"] == target and row["sha256"] == sha256_hex(environment.files[target])
        for row in probe["files"]
    )
    assert not any("auth.json" in item["path"] for item in probe["files"])
    assert "SYNTHETIC_API_KEY" not in json.dumps(probe)
    assert "SYNTHETIC_API_KEY" not in config.model_dump_json()


@pytest.mark.native
def test_adapter_selection_reaches_published_pi_bin_rpc_consumer(tmp_path):
    modules = native_modules(required=True)
    assert modules is not None
    instance, _ = adopted_pi(tmp_path)
    environment = StartupCaptureEnvironment("pi", "0.85.1")
    asyncio.run(instance.run("not executed", environment, AgentContext()))
    invocation = next(
        row
        for row in environment.commands
        if "pi --print --mode json" in row["command"]
    )
    argv = shlex.split(invocation["command"])
    selected = [
        token
        for flag in ("--provider", "--model", "--thinking")
        for token in (flag, argv[argv.index(flag) + 1])
    ]
    executable = modules / ".bin/pi"
    package = modules / "@earendil-works/pi-coding-agent"
    manifest = json.loads((package / "package.json").read_text())
    assert executable.resolve() == (package / manifest["bin"]["pi"]).resolve()
    assert manifest["bin"]["pi"] == "dist/bundle/cli.js"
    env = native_environment(tmp_path)
    root = Path(env["PI_CODING_AGENT_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    model = environment.probe()["capture"]["model"]
    (root / "models-store.json").write_text(
        json.dumps({"openai": {"lastModified": 4102444800000, "models": [model]}})
    )
    command = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        str(executable),
        "--mode",
        "rpc",
        "--offline",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        *selected,
    ]
    assert "NODE_OPTIONS" not in env
    with ControlProcess(command, tmp_path, env) as process:
        process.send({"id": "state", "type": "get_state"})
        for _ in range(50):
            response = json.loads(process.line())
            if response.get("id") == "state":
                break
        else:
            pytest.fail("published Pi get_state response missing")
    assert response["success"] is True
    assert response["data"]["model"]["id"] == argv[argv.index("--model") + 1]
    assert response["data"]["model"]["provider"] == argv[argv.index("--provider") + 1]
    assert response["data"]["thinkingLevel"] == argv[argv.index("--thinking") + 1]
