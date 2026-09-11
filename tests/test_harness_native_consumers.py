"""Historical configuration fixtures and harmless shell argv consumers.

Current installed packages are exercised in test_stable_native_consumers.py.
"""

import asyncio
import json
import shlex
import subprocess
import sys
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.costs import native_cost_evidence
from tetrabench.harness_config import HarnessConfig, NativeConfig
from tetrabench.harnesses import compile_agent_config, seal_harness


def agent(tmp_path, name, options=None, native=None, env=None, policy="primary") -> Any:
    spec = HarnessConfig(
        name=name,
        version="1.18.29"
        if name == "opencode"
        else "0.74.0"
        if name == "pi"
        else "0.114.0"
        if name == "codex"
        else "2.1.63",
        model="openai/model",
        options=options or {},
        env=env or {},
        ancillary_models=policy,
        native_config=NativeConfig(text=json.dumps(native))
        if native is not None
        else None,
    )
    return AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(spec, tmp_path)), logs_dir=tmp_path
    )


@pytest.mark.parametrize(
    ("name", "options", "expected"),
    [
        (
            "claude-code",
            {
                "allowed_tools": "Bash(git:*)",
                "append_system_prompt": "two words $(printf injected); *",
            },
            [
                "--allowedTools",
                "Bash(git:*)",
                "--append-system-prompt",
                "two words $(printf injected); *",
            ],
        ),
        (
            "opencode",
            {"variant": "high --model=openai/other", "title": "$(printf injected)"},
            ["--variant", "high --model=openai/other", "--title", "$(printf injected)"],
        ),
        ("pi", {"thinking": "high"}, ["--thinking", "high"]),
        (
            "codex",
            {"reasoning_summary": "detailed"},
            ["--config", "model_reasoning_summary=detailed"],
        ),
    ],
)
def test_descriptor_values_reach_real_shell_consumer_as_single_arguments(
    tmp_path, name, options, expected
):
    instance = agent(tmp_path, name, options)
    consumer = shlex.join(
        [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))"]
    )
    actual = json.loads(
        subprocess.check_output(
            ["/bin/sh", "-c", f"{consumer} {instance.build_cli_flags()}"], cwd=tmp_path
        )
    )
    for index in range(0, len(expected), 2):
        flag, value = expected[index : index + 2]
        if name == "pi":
            assert [flag, value] in [actual[i : i + 2] for i in range(len(actual) - 1)]
        else:
            assert f"{flag}={value}" in actual
    assert "--model=openai/other" not in actual
    assert "injected" not in actual


def test_formatted_permission_and_boolean_flags_are_not_double_quoted(tmp_path):
    from harbor.agents.installed.base import CliFlag

    instance = agent(tmp_path, "claude-code", {"permission_mode": "acceptEdits"})
    instance.CLI_FLAGS = [
        *instance.CLI_FLAGS,
        CliFlag("enabled", cli="--enabled", type="bool"),
        CliFlag("disabled", cli="--disabled", type="bool"),
    ]
    instance._resolved_flags.update(enabled=True, disabled=False)
    command = shlex.join(
        [sys.executable, "-c", "import json,sys;print(json.dumps(sys.argv[1:]))"]
    )
    argv = json.loads(
        subprocess.check_output(
            ["/bin/sh", "-c", command + " " + instance.build_cli_flags()]
        )
    )
    assert "--permission-mode=acceptEdits" in argv
    assert "--enabled" in argv and "--disabled" not in argv


@pytest.mark.parametrize(
    "native",
    [
        {
            "model_providers": {
                "custom": {"experimental_bearer_token": "literal-secret"}
            }
        },
        {"model_providers": {"custom": {"experimental_bearer_token": "${TOKEN}"}}},
        {"mcp_servers": {"example": {"bearer_token": "literal-secret"}}},
        {
            "mcp_servers": {
                "example": {"http_headers": {"Authorization": "Bearer literal-secret"}}
            }
        },
    ],
)
def test_codex_native_literal_credential_fields_are_rejected(tmp_path, native):
    with pytest.raises(ValueError):
        agent(tmp_path, "codex", native=native)


def test_codex_native_environment_selectors_deliver_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_TOKEN", "fixture-token")
    native = {
        "model_providers": {"custom": {"env_key": "TOKEN"}},
        "mcp_servers": {
            "example": {
                "url": "https://example.test/mcp",
                "bearer_token_env_var": "TOKEN",
                "env_http_headers": {"X-Api-Key": "TOKEN"},
            }
        },
    }
    instance = agent(tmp_path, "codex", native=native, env={"TOKEN": "${HOST_TOKEN}"})
    effective = instance._build_effective_config()
    assert (
        instance.extra_env[effective["model_providers"]["custom"]["env_key"]]
        == "fixture-token"
    )
    assert (
        instance.extra_env[effective["mcp_servers"]["example"]["bearer_token_env_var"]]
        == "fixture-token"
    )
    assert "fixture-token" not in json.dumps(effective)


@pytest.mark.parametrize(
    "reference", ["$TOKEN", "${TOKEN}", "literal-token", "!printf unsafe"]
)
def test_pi_rejects_values_its_resolver_would_treat_as_literal_or_command(
    tmp_path, monkeypatch, reference
):
    monkeypatch.setenv("HOST_TOKEN", "fixture-token")
    with pytest.raises(ValueError):
        agent(
            tmp_path,
            "pi",
            native={"models": {"providers": {"custom": {"apiKey": reference}}}},
            env={"TOKEN": "${HOST_TOKEN}"},
        )


@pytest.mark.parametrize("generated", [False, True])
def test_pi_uploaded_credentials_resolve_with_074_consumer(
    tmp_path, monkeypatch, generated
):
    monkeypatch.setenv("HOST_TOKEN", "fixture-token")
    monkeypatch.setenv("HOST_ENDPOINT", "https://example.test/v1")
    instance = (
        agent(
            tmp_path,
            "pi",
            options={"model_api": "openai-responses"},
            env={
                "OPENAI_API_KEY": "${HOST_TOKEN}",
                "OPENAI_BASE_URL": "${HOST_ENDPOINT}",
            },
        )
        if generated
        else agent(
            tmp_path,
            "pi",
            native={
                "models": {
                    "providers": {
                        "custom": {
                            "apiKey": "TOKEN",
                            "api": "openai-responses",
                            "baseUrl": "https://example.test/v1",
                            "models": [{"id": "model"}],
                        }
                    }
                }
            },
            env={"TOKEN": "${HOST_TOKEN}"},
        )
    )
    environment = CaptureEnvironment("pi", "0.74.0")
    asyncio.run(instance.run("no real execution", environment, AgentContext()))
    models = json.loads(environment.uploads["/tmp/harbor-pi-agent/models.json"])
    selector = models["providers"]["harbor-endpoint" if generated else "custom"][
        "apiKey"
    ]
    # Pi 0.74 resolve-config-value.js: process.env[config] || config.
    assert (instance.extra_env.get(selector) or selector) == "fixture-token"


def test_opencode_11829_full_native_command_has_supported_permission_flag(tmp_path):
    instance = agent(
        tmp_path,
        "opencode",
        {"title": "$(printf injected)", "variant": "high --model=openai/other"},
    )
    environment = CaptureEnvironment("opencode", "1.18.29")
    asyncio.run(instance.run("fixture prompt", environment, AgentContext()))
    command = next(
        item["command"]
        for item in environment.commands
        if " run --format=json " in item["command"]
    )
    tokens = shlex.split(command)
    tokens = tokens[tokens.index("opencode") : tokens.index("2>&1")]
    assert "--auto" in tokens
    assert "--dangerously-skip-permissions" not in tokens
    assert "--model=openai/other" not in tokens
    assert "--title=$(printf injected)" in tokens


@pytest.mark.parametrize("version", ["1.2.15", "1.18.28", "999.0.0"])
def test_unverified_opencode_versions_fail_before_install(version):
    with pytest.raises(ValueError, match="supported controlled OpenCode"):
        HarnessConfig(name="opencode", version=version, model="openai/model")


def test_opencode_mode_migration_uses_native_precedence(tmp_path):
    # v1.18.29 config.ts merges mode over agent, not the reverse.
    with pytest.raises(ValueError, match="primary"):
        agent(
            tmp_path,
            "opencode",
            native={
                "agent": {"compaction": {"model": "openai/model"}},
                "mode": {"compaction": {"model": "openai/other"}},
            },
        )
    agent(
        tmp_path,
        "opencode",
        native={
            "agent": {"compaction": {"model": "openai/other"}},
            "mode": {"compaction": {"model": "openai/model"}},
        },
    )
    agent(
        tmp_path,
        "opencode",
        native={"mode": {"compaction": {"model": "openai/other"}}},
        policy="native",
    )


def test_codex_role_file_requires_native_policy_without_removing_role(tmp_path):
    native = {"agents": {"worker": {"config_file": "/workspace/worker.toml"}}}
    with pytest.raises(ValueError, match="config_file"):
        agent(tmp_path, "codex", native=native)
    with pytest.raises(ValueError):
        agent(tmp_path, "codex", native=native, policy="native")
    from tetrabench.resources import AGENT_RESOURCE_ROOT

    (tmp_path / "worker.toml").write_text('model="model"\n')
    spec = HarnessConfig(
        name="codex",
        version="0.154.0",
        model="openai/model",
        ancillary_models="native",
        native_config=NativeConfig(
            text=json.dumps({"agents": {"worker": {"config_file": "worker.toml"}}})
        ),
    )
    resolved = seal_harness(spec, tmp_path)
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(resolved), logs_dir=tmp_path
    )
    assert instance._build_effective_config()["agents"]["worker"][
        "config_file"
    ].startswith(AGENT_RESOURCE_ROOT)


@pytest.mark.parametrize("advertised", [False, True])
def test_pi_default_zero_prices_are_not_a_known_zero_charge(advertised):
    from tetrabench.costs import pi_configured_pricing

    cost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    definition = {"id": "model", **({"cost": cost} if advertised else {})}
    config = {
        "providers": {
            "custom": {
                "api": "openai-responses",
                "baseUrl": "https://example.test",
                "models": [definition],
            }
        }
    }
    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "provider": "custom",
            "model": "model",
            "usage": {"input": 123, "output": 50, "cost": {"total": 0}},
        },
    }
    rows = native_cost_evidence(
        harness="pi",
        requested_model="custom/model",
        scope="one",
        result={"agent_result": {"cost_usd": 0}},
        streams={"pi.txt": json.dumps(event).encode()},
        result_artifact="result.json",
        pi_pricing=pi_configured_pricing({"models": config}),
    )
    assert rows[0].amount_usd == ("0" if advertised else None)
    if not advertised:
        assert rows[0].reported_amount_usd == "0"
        assert any("unpriced" in note for note in rows[0].limitations)
        assert not any("missing or malformed" in note for note in rows[0].limitations)


def test_claude_oversized_raw_stream_does_not_prove_estimation():
    from tetrabench.costs import MAX_COST_ARTIFACT_BYTES

    rows = native_cost_evidence(
        harness="claude-code",
        requested_model="anthropic/model",
        scope="one",
        result={"agent_result": {"cost_usd": 1.25}},
        streams={"claude-code.txt": b"x" * (MAX_COST_ARTIFACT_BYTES + 1)},
        result_artifact="result.json",
    )
    assert rows[0].amount_usd == "1.25"
    assert rows[0].source == "harness_reported"
    assert any("native-reported or estimated" in note for note in rows[0].limitations)
