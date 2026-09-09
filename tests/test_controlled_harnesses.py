from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from typer.testing import CliRunner

from tetrabench.authoring import initialize_project
from tetrabench.cli import app
from tetrabench.config import load_harness_override, load_project_config
from tetrabench.harness_agents import HarnessVersionError
from tetrabench.harness_config import HarnessConfig, NativeConfig, ResolvedHarness
from tetrabench.harnesses import (
    compile_agent_config,
    normalized_options,
    parse_native,
    registered_harnesses,
    seal_harness,
    validate_credentials,
)
from tetrabench.models import ConfigOverrides
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.records import RequestRecord
from tetrabench.submission import prepare_run


class CaptureEnvironment:
    default_user = None

    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        self.commands: list[dict] = []
        self.uploads: dict[str, bytes] = {}

    async def exec(self, **kwargs):
        self.commands.append(kwargs)
        stdout = ""
        if "--version" in kwargs["command"]:
            stdout = {
                "opencode": self.version,
                "codex": f"codex-cli {self.version}",
                "claude-code": f"{self.version} (Claude Code)",
                "pi": self.version,
            }[self.name]
        return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

    async def upload_file(self, source, destination):
        self.uploads[str(destination)] = Path(source).read_bytes()

    async def download_dir(self, *args, **kwargs):
        pass

    async def download_file(self, *args, **kwargs):
        pass


@pytest.mark.parametrize(
    ("name", "version", "model", "options", "native", "flag"),
    [
        (
            "opencode",
            "1.18.29",
            "openai/gpt-5",
            {"variant": "high"},
            {
                "compaction": {"auto": True},
                "provider": {"openai": {"models": {"gpt-5": {"temperature": 0.1}}}},
            },
            "--variant=high",
        ),
        (
            "codex",
            "0.114.0",
            "openai/gpt-5",
            {"reasoning_effort": "medium", "web_search": "disabled"},
            {"model_context_window": 100000},
            "model_reasoning_effort=medium",
        ),
        (
            "claude-code",
            "2.1.63",
            "anthropic/claude-sonnet-4-6",
            {"max_turns": 3, "max_budget_usd": "1.50"},
            {"permissions": {"allow": ["Read"]}},
            "--max-turns=3",
        ),
        (
            "pi",
            "0.74.0",
            "openai/gpt-5",
            {"thinking": "high"},
            {"settings": {"compaction": {"enabled": True}}},
            "--thinking high",
        ),
    ],
)
def test_real_factory_setup_and_native_run_capture(
    tmp_path, monkeypatch, name, version, model, options, native, flag
):
    target = "ANTHROPIC_API_KEY" if name == "claude-code" else "OPENAI_API_KEY"
    monkeypatch.setenv("TB_TEST_MODEL_KEY", "fixture-credential-do-not-persist")
    monkeypatch.setenv("CODEX_FORCE_AUTH_JSON", "true")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    spec = HarnessConfig(
        name=name,
        version=version,
        model=model,
        options=options,
        native_config=NativeConfig(text=json.dumps(native)),
        env={target: "${TB_TEST_MODEL_KEY}"},
    )
    resolved = seal_harness(spec, tmp_path)
    config = compile_agent_config(resolved)
    before = config.model_dump_json()
    assert config.kwargs["version"] == version
    agent: Any = AgentFactory.create_agent_from_config(config, logs_dir=tmp_path / name)
    environment = CaptureEnvironment(name, version)
    asyncio.run(agent.setup(environment))
    asyncio.run(agent.run("solve this exact fixture", environment, AgentContext()))
    commands = "\n".join(item["command"] for item in environment.commands)
    assert flag in commands
    assert any(
        item.get("env", {}).get(target) == "fixture-credential-do-not-persist"
        for item in environment.commands
    )
    assert config.model_dump_json() == before
    assert "fixture-credential-do-not-persist" not in before
    provenance = (tmp_path / name / "tetrabench-harness.json").read_text()
    assert "fixture-credential-do-not-persist" not in provenance
    parsed = json.loads(provenance)
    assert parsed["observed_version"] == version
    assert parsed["version_status"] == "matched"
    assert parsed["credential_variables"] == [target]
    assert parsed["effective_native_configs"]
    if name == "opencode":
        assert "--title=tetrabench" in commands
        assert (
            json.loads(parsed["effective_native_configs"]["opencode.json"])[
                "small_model"
            ]
            == model
        )
    elif name == "pi":
        assert "@earendil-works/pi-coding-agent@0.74.0" in commands
        assert "@mariozechner" not in commands
        assert "/tmp/harbor-pi-agent/settings.json" in environment.uploads
        assert any(
            item.get("env", {}).get("PI_CODING_AGENT_DIR") == "/tmp/harbor-pi-agent"
            for item in environment.commands
        )
    elif name == "claude-code":
        assert any(
            item.get("env", {}).get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
            == "claude-sonnet-4-6"
            for item in environment.commands
        )
        assert "--effort low" not in commands
    else:
        assert agent._resolve_auth_json_path() is None


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code", "pi"])
def test_observed_version_mismatch_never_echoes_requested_version(tmp_path, name):
    version = "1.18.29" if name == "opencode" else "1.0.0"
    spec = seal_harness(
        HarnessConfig(name=name, version=version, model="openai/test"), tmp_path
    )
    agent: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(spec), logs_dir=tmp_path
    )

    # Isolate the post-install probe from the native installer's own version guard.
    async def installed(_environment):
        pass

    agent.install = installed
    with pytest.raises(HarnessVersionError):
        asyncio.run(agent.setup(CaptureEnvironment(name, "9.9.9")))
    result = json.loads((tmp_path / "tetrabench-harness.json").read_text())
    assert result["requested_version"] == version
    assert result["observed_version"] == "9.9.9"
    assert result["version_status"] == "mismatch"


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "unknown"},
        {"version": "latest"},
        {"version": "^1.0.0"},
        {"options": {"ignored_by_harbor": "bad"}},
        {"args": ["--unknown", "bad"]},
        {"args": ["--variant", "high"], "options": {"variant": "low"}},
        {"model": "openai/model; touch /oops"},
        {"env": {"HOME": "${OTHER}"}},
        {"env": {"OPENAI_API_KEY": "${AWS_SECRET_ACCESS_KEY}"}},
        {"env": {"OPENAI_API_KEY": "literal-credential"}},
        {"options": {"title": None}},
        {"native_config": NativeConfig(text='{"small_model":"openai/other"}')},
        {
            "native_config": NativeConfig(
                text='{"provider":{"openai":{"options":{"apiKey":"literal"}}}}'
            )
        },
        {"native_config": NativeConfig(text='{"env":{"OPENAI_API_KEY":"literal"}}')},
        {"native_config": NativeConfig(text='{"temperature":NaN}')},
        {"native_config": NativeConfig(text='{"a":1,"a":2}')},
    ],
)
def test_unsupported_conflicting_or_secret_inputs_fail(patch):
    with pytest.raises(ValueError):
        HarnessConfig(
            **(
                {"name": "opencode", "version": "1.18.29", "model": "openai/gpt-5"}
                | patch
            )
        )


@pytest.mark.parametrize("name", ["codex", "claude-code"])
def test_lossy_nested_models_rejected(name):
    with pytest.raises(ValueError, match="truncates"):
        HarnessConfig(name=name, version="1.0.0", model="openrouter/openai/gpt-5")


def test_pi_current_package_and_custom_models(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Earendil"):
        HarnessConfig(name="pi", version="0.73.0", model="openai/gpt-5")
    monkeypatch.setenv("MODEL_KEY", "fixture-key")
    spec = HarnessConfig(
        name="pi",
        version="0.74.0",
        model="custom/nested/model",
        env={"OPENAI_API_KEY": "${MODEL_KEY}"},
        native_config=NativeConfig(
            text=json.dumps(
                {
                    "models": {
                        "providers": {
                            "custom": {
                                "baseUrl": "https://example.test/v1",
                                "apiKey": "OPENAI_API_KEY",
                                "api": "openai-responses",
                                "models": [
                                    {"id": "nested/model", "cost": {"input": 0.25}}
                                ],
                            }
                        }
                    }
                }
            )
        ),
    )
    agent: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(spec, tmp_path)), logs_dir=tmp_path
    )
    env = CaptureEnvironment("pi", "0.74.0")
    asyncio.run(agent.run("solve", env, AgentContext()))
    assert "OPENAI_API_KEY" in env.uploads["/tmp/harbor-pi-agent/models.json"].decode()
    assert any(
        "--provider custom --model nested/model" in item["command"]
        for item in env.commands
    )


def test_args_translate_supported_codex_config_flags():
    spec = HarnessConfig(
        name="codex",
        version="0.114.0",
        model="openai/gpt-5",
        args=["-c", 'model_reasoning_effort="high"', "--web-search=disabled"],
    )
    assert normalized_options(spec) == {
        "reasoning_effort": "high",
        "web_search": "disabled",
    }


def test_task_digest_separate_legacy_bytes_and_portable_native_snapshot(tmp_path):
    root = initialize_project(tmp_path / "project")
    legacy = prepare_run(root, "example", run_id="same")
    legacy_bytes = canonical_model_bytes(legacy.request)
    assert b'"harness"' not in legacy_bytes
    assert (
        canonical_model_bytes(parse_canonical_model(legacy_bytes, RequestRecord))
        == legacy_bytes
    )
    native = root / "native.json"
    native.write_text(
        '{"provider":{"openai":{"models":{"gpt-5":{"temperature":0.25}}}}}'
    )
    spec = HarnessConfig(
        name="opencode",
        version="1.18.29",
        model="openai/gpt-5",
        native_config=NativeConfig(path="native.json"),
        env={"OPENAI_API_KEY": "${MODEL_KEY}"},
    )
    controlled = prepare_run(
        root, "example", run_id="same", overrides=ConfigOverrides(harness=spec)
    )
    native.unlink()
    assert (
        controlled.request.context_manifest_sha256
        == legacy.request.context_manifest_sha256
    )
    assert controlled.plan.context == legacy.plan.context
    assert controlled.plan.trials == legacy.plan.trials
    assert controlled.request.plan_sha256 != legacy.request.plan_sha256
    data = canonical_model_bytes(controlled.request)
    assert b'"path"' not in data
    restored = parse_canonical_model(data, RequestRecord)
    assert canonical_model_bytes(restored) == data
    assert restored.plan.harness is not None
    assert (
        parse_native(restored.plan.harness.native_config)["provider"]["openai"][
            "models"
        ]["gpt-5"]["temperature"]
        == 0.25
    )


def test_public_runconfig_and_relative_native_path(tmp_path):
    (tmp_path / "native.toml").write_text("model_context_window = 100000\n")
    path = tmp_path / "model.toml"
    path.write_text(
        '[harness]\nname="codex"\nversion="0.114.0"\nmodel="openai/gpt-5"\n[harness.native_config]\nformat="toml"\npath="native.toml"\n[harness.env]\nOPENAI_API_KEY="${MODEL_KEY}"\n'
    )
    spec = load_harness_override(path)
    assert spec.native_config.path is None
    assert spec.env == {"OPENAI_API_KEY": "${MODEL_KEY}"}


def test_credentials_require_reference_without_echoing_value(tmp_path, monkeypatch):
    spec = seal_harness(
        HarnessConfig(
            name="opencode",
            version="1.18.29",
            model="openai/gpt-5",
            env={"OPENAI_API_KEY": "${MODEL_KEY}"},
        ),
        tmp_path,
    )
    monkeypatch.delenv("MODEL_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_credentials(spec)
    monkeypatch.setenv("MODEL_KEY", "fixture-key")
    validate_credentials(spec)
    assert "fixture-key" not in json.dumps(spec.model_dump())


def test_agents_discovery_and_sanitized_error(tmp_path, monkeypatch):
    result = CliRunner().invoke(app, ["agents", "--json"])
    assert result.exit_code == 0
    assert {item["name"] for item in json.loads(result.stdout)["harnesses"]} == {
        adapter.name for adapter in registered_harnesses()
    }
    root = initialize_project(tmp_path / "project")
    path = root / "bad.toml"
    path.write_text(
        '[harness]\nname="opencode"\nversion="1.18.29"\nmodel="openai/gpt-5"\n[harness.env]\nOPENAI_API_KEY="do-not-emit-this-secret"\n'
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "example",
            "--harness",
            str(path),
            "--output",
            str(root / "no-output"),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "do-not-emit-this-secret" not in result.output
    assert not (root / "no-output").exists()


def test_legacy_project_config_and_explicit_conflict(tmp_path):
    root = initialize_project(tmp_path / "project")
    assert load_project_config(root).harness is None
    with (root / "tetrabench.toml").open("a") as stream:
        stream.write(
            '\n[harness]\nname="opencode"\nversion="1.18.29"\nmodel="openai/gpt-5"\n'
        )
    with pytest.raises(ValueError, match="legacy"):
        load_project_config(root)


def test_snapshot_digest_mismatch_rejected():
    with pytest.raises(ValueError, match="digest"):
        ResolvedHarness.model_validate(
            {
                "name": "opencode",
                "version": "1.18.29",
                "model": "openai/gpt-5",
                "native_config": {"format": "json", "text": "{}", "sha256": "0" * 64},
            }
        )


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code", "pi"])
def test_unused_factory_kwargs_rejected(tmp_path, name):
    spec = seal_harness(
        HarnessConfig(
            name=name,
            version="1.18.29" if name == "opencode" else "1.0.0",
            model="openai/model",
        ),
        tmp_path,
    )
    config = compile_agent_config(spec)
    config.kwargs["unused_option"] = "quietly ignored upstream"
    with pytest.raises(ValueError, match="unsupported"):
        AgentFactory.create_agent_from_config(config, logs_dir=tmp_path)


def test_codex_lossy_native_toml_rejected_before_output(tmp_path):
    spec = HarnessConfig(
        name="codex",
        version="1.0.0",
        model="openai/model",
        native_config=NativeConfig(text='{"model_reasoning_effort":null}'),
    )
    with pytest.raises(ValueError, match="losslessly"):
        seal_harness(spec, tmp_path)
    assert not list(tmp_path.iterdir())


def test_native_policy_preserves_explicit_small_model(tmp_path):
    spec = seal_harness(
        HarnessConfig(
            name="opencode",
            version="1.18.29",
            model="openai/gpt-5",
            ancillary_models="native",
            native_config=NativeConfig(
                text='{"small_model":"openai/small","compaction":{"auto":true}}'
            ),
        ),
        tmp_path,
    )
    agent: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(spec), logs_dir=tmp_path
    )
    command = agent._build_register_config_command()
    assert '"small_model": "openai/small"' in command
    assert '"auto": true' in command


def test_runtime_mismatch_before_any_output_or_provider(tmp_path, monkeypatch):
    from tetrabench.diagnostics import PreflightError

    root = initialize_project(tmp_path / "project")

    def refuse(*_args, **_kwargs):
        raise PreflightError("unsupported_python", operation="run")

    monkeypatch.setattr("tetrabench.preflight.check_runtime", refuse)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        app, ["run", "example", "--output", str(root / "no-output"), "--json"]
    )
    assert result.exit_code == 2
    assert '"code":"unsupported_python"' in result.stderr
    assert "--python 3.12" in result.stderr
    assert not (root / "no-output").exists()


def test_pi_native_custom_endpoint_api_translation(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_KEY", "fixture-key")
    monkeypatch.setenv("MODEL_ENDPOINT", "https://example.test/v1")
    spec = seal_harness(
        HarnessConfig(
            name="pi",
            version="0.74.0",
            model="openai/vendor/model",
            options={"model_api": "openai-responses"},
            env={
                "OPENAI_API_KEY": "${MODEL_KEY}",
                "OPENAI_BASE_URL": "${MODEL_ENDPOINT}",
            },
        ),
        tmp_path,
    )
    agent: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(spec), logs_dir=tmp_path
    )
    env = CaptureEnvironment("pi", "0.74.0")
    asyncio.run(agent.run("solve", env, AgentContext()))
    models = json.loads(env.uploads["/tmp/harbor-pi-agent/models.json"])
    assert models["providers"]["harbor-endpoint"]["api"] == "openai-responses"
    assert models["providers"]["harbor-endpoint"]["apiKey"] == "OPENAI_API_KEY"
    assert "fixture-key" not in (tmp_path / "tetrabench-harness.json").read_text()
    assert (
        "https://example.test/v1"
        not in (tmp_path / "tetrabench-harness.json").read_text()
    )
    assert any(
        "--provider harbor-endpoint --model vendor/model" in item["command"]
        for item in env.commands
    )


def test_escaped_runtime_values_are_not_serialized_in_effective_config(
    tmp_path, monkeypatch
):
    value = 'fixture-with-"quotes"-and-\\slashes'
    monkeypatch.setenv("MODEL_KEY", value)
    spec = seal_harness(
        HarnessConfig(
            name="opencode",
            version="1.18.29",
            model="openai/model",
            env={"OPENAI_API_KEY": "${MODEL_KEY}"},
        ),
        tmp_path,
    )
    agent: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(spec), logs_dir=tmp_path
    )
    rendered = agent._safe_text(json.dumps({"apiKey": value, "other": "unaltered"}))
    assert json.loads(rendered) == {"apiKey": "${OPENAI_API_KEY}", "other": "unaltered"}


@pytest.mark.parametrize(
    ("name", "native"),
    [
        (
            "opencode",
            {
                "provider": {
                    "custom": {
                        "options": {
                            "apiKey": "{env:CUSTOM_TOKEN}",
                            "baseURL": "https://example.test/v1",
                        }
                    }
                }
            },
        ),
        (
            "codex",
            {
                "model_provider": "custom",
                "model_providers": {
                    "custom": {
                        "env_key": "CUSTOM_TOKEN",
                        "base_url": "https://example.test/v1",
                    }
                },
            },
        ),
        (
            "pi",
            {
                "models": {
                    "providers": {
                        "custom": {
                            "apiKey": "CUSTOM_TOKEN",
                            "baseUrl": "https://example.test/v1",
                            "api": "openai-responses",
                            "models": [{"id": "model"}],
                        }
                    }
                }
            },
        ),
    ],
)
def test_custom_provider_credentials_supported_from_sealed_native_config(
    tmp_path, name, native
):
    path = tmp_path / "native.json"
    path.write_text(json.dumps(native))
    spec = seal_harness(
        HarnessConfig(
            name=name,
            version="1.18.29" if name == "opencode" else "1.0.0",
            model="custom/model",
            env={"CUSTOM_TOKEN": "${HOST_MODEL_TOKEN}"},
            native_config=NativeConfig(path=str(path)),
        ),
        tmp_path,
    )
    assert compile_agent_config(spec).env == {"CUSTOM_TOKEN": "${HOST_MODEL_TOKEN}"}
    assert "HOST_MODEL_TOKEN" in canonical_model_bytes(spec).decode()


def test_unused_custom_environment_variable_is_rejected():
    with pytest.raises(ValueError, match="reserved or unsupported"):
        HarnessConfig(
            name="opencode",
            version="1.18.29",
            model="openai/model",
            env={"UNUSED_TOKEN": "${HOST_KEY}"},
        )


@pytest.mark.parametrize(
    "command", [["doctor", "--json"], ["controller", "deploy", "--yes", "--json"]]
)
def test_cli_runtime_preflight_is_sanitized_before_configuration_or_providers(
    monkeypatch, command
):
    from tetrabench.diagnostics import PreflightError

    def refuse(operation):
        raise PreflightError("unsupported_python", operation=operation)

    monkeypatch.setattr("tetrabench.preflight.check_runtime", refuse)
    monkeypatch.setattr(
        "tetrabench.cli.load_project_config",
        lambda *args, **kwargs: pytest.fail("config read before compatibility guard"),
    )
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 2
    assert '"code":"unsupported_python"' in result.stderr
    assert "--python 3.12" in result.stderr
