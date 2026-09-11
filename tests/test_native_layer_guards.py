"""Run-level native overlays share validation before any auth handoff."""

import asyncio
import json
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from native_consumer_support import native_modules, native_run
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.auth_config import AuthSpec, NativeAuthReference
from tetrabench.harness_config import HarnessConfig, NativeConfig, ResourceSource
from tetrabench.harnesses import (
    compile_agent_config,
    native_configuration_layers,
    seal_harness,
    validate_explicit_auth_configuration,
)


def harness(
    tmp_path, overlay, *, native=None, auth=False, policy="native", options=None
):
    (tmp_path / "overlay.jsonc").write_text(json.dumps(overlay))
    return HarnessConfig(
        name="opencode",
        version="1.18.30",
        model="openai/model",
        ancillary_models=policy,
        options=options or {},
        auth=AuthSpec(
            mode="chatgpt_oauth",
            reference=NativeAuthReference(
                profile="eval", binding="local", generation=1
            ),
        )
        if auth
        else None,
        native_config=NativeConfig(text=json.dumps(native or {})),
        resources=[
            ResourceSource(
                source="overlay.jsonc", destination="opencode/opencode.jsonc"
            )
        ],
    )


@pytest.mark.parametrize(
    "overlay",
    [
        {"provider": {"openai": {"options": {"baseURL": "https://other.example/v1"}}}},
        {"provider": {"openai": {"options": {"apiKey": "literal"}}}},
        {"model": "other/model"},
    ],
)
def test_overlay_cannot_replace_explicit_auth_route_or_primary_model(tmp_path, overlay):
    with pytest.raises(ValueError):
        seal_harness(harness(tmp_path, overlay, auth=True), tmp_path)


def test_primary_policy_checks_resource_agent_models(tmp_path):
    with pytest.raises(ValueError, match="primary"):
        seal_harness(
            harness(
                tmp_path,
                {"agent": {"compaction": {"model": "openai/other"}}},
                policy="primary",
            ),
            tmp_path,
        )


def test_resource_disabling_main_variant_is_rejected_before_handoff(tmp_path):
    def variants(disabled):
        return {
            "provider": {
                "openai": {
                    "models": {
                        "model": {"variants": {"custom": {"disabled": disabled}}}
                    }
                }
            }
        }

    with pytest.raises(ValueError, match="disabled"):
        seal_harness(
            harness(
                tmp_path,
                variants(True),
                native=variants(False),
                options={"variant": "custom"},
            ),
            tmp_path,
        )


def test_merge_description_and_execution_share_native_layer_precedence(tmp_path):
    spec = seal_harness(
        harness(
            tmp_path,
            {"compaction": {"auto": False}},
            native={"compaction": {"auto": True, "prune": False}},
        ),
        tmp_path,
    )
    description = native_configuration_layers(spec)
    assert [layer.source for layer in description.layers] == [
        "native_config",
        "opencode/opencode.jsonc",
    ]
    assert description.effective_config["compaction"] == {"auto": False, "prune": False}
    validate_explicit_auth_configuration(spec)
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(spec), logs_dir=tmp_path / "logs"
    )
    environment = CaptureEnvironment("opencode", "1.18.30")
    asyncio.run(instance.run("not executed", environment, AgentContext()))
    merged = json.loads(instance._effective_configs["opencode.sealed-layers.json"])
    assert merged["compaction"] == description.effective_config["compaction"]
    assert any(
        row.get("env", {}).get("OPENCODE_CONFIG_DIR") == description.config_directory
        for row in environment.commands
    )


@pytest.mark.native
def test_current_opencode_consumer_observes_resource_override(tmp_path):
    modules = native_modules(required=True)
    assert modules is not None
    spec = seal_harness(
        harness(
            tmp_path,
            {"compaction": {"auto": False}},
            native={"compaction": {"auto": True, "prune": False}},
        ),
        tmp_path,
    )
    description = native_configuration_layers(spec)
    config = tmp_path / "injected.json"
    config.write_text(json.dumps(description.main_config))
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "opencode.jsonc").write_text(spec.resources[0].text)
    result = native_run(
        [str(modules / "opencode-linux-x64/bin/opencode"), "debug", "config"],
        tmp_path,
        {
            "OPENCODE_CONFIG": str(config),
            "OPENCODE_CONFIG_DIR": str(directory),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual["compaction"] == description.effective_config["compaction"]


def test_native_effective_guard_runs_before_inference_and_can_refuse(
    tmp_path, monkeypatch
):
    from test_models_cli import snapshot

    from tetrabench.harness_config import bind_capability_adoption

    source = HarnessConfig(name="codex", version="0.154.0", model="openai/model")
    bound = bind_capability_adoption(
        source, snapshot(source), control="effort", select="high"
    )
    config = compile_agent_config(seal_harness(bound, tmp_path))
    instance: Any = AgentFactory.create_agent_from_config(
        config, logs_dir=tmp_path / "logs"
    )
    environment = CaptureEnvironment("codex", "0.154.0")
    calls = []

    async def guard(actual_environment, *, env, cwd):
        assert actual_environment is environment
        calls.append((env, cwd))
        raise ValueError("effective native variant changed")

    monkeypatch.setattr(instance, "validate_native_capability", guard)
    with pytest.raises(ValueError, match="effective native"):
        asyncio.run(instance.run("must not execute", environment, AgentContext()))
    assert len(calls) == 1
    assert not any("codex exec " in row["command"] for row in environment.commands)
    assert "guard" not in config.model_dump_json()


@pytest.mark.parametrize(
    ("name", "version"), [("opencode", "1.18.30"), ("pi", "0.85.1")]
)
def test_explicit_api_key_cli_forwards_declared_native_provider(
    tmp_path, monkeypatch, name, version
):
    from typer.testing import CliRunner

    from tetrabench.auth import AuthStatus
    from tetrabench.cli import app

    path = tmp_path / "run.toml"
    path.write_text(
        f'[harness]\nname="{name}"\nversion="{version}"\nmodel="openrouter/openai/model"\n[harness.auth]\nmode="api_key"\n[harness.auth.reference]\nkind="env"\nname="EVAL_KEY"\n'
    )
    seen = []

    def login(harness, auth, **kwargs):
        seen.append(kwargs["model"])
        return AuthStatus(harness=harness, mode=auth.mode, state="ready")

    monkeypatch.setattr("tetrabench.auth.auth_login", login)
    result = CliRunner().invoke(
        app,
        [
            "auth",
            "login",
            "--harness",
            str(path),
            "--executable",
            "/pinned/native",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == ["openrouter/openai/model"]
