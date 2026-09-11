"""Pinned native transport selectors and context-window model suffixes."""

import asyncio
import json
import shlex
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from native_consumer_support import native_environment, native_modules
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.auth_config import AuthSpec, NativeAuthReference
from tetrabench.harness_config import HarnessConfig, NativeConfig, ResourceSource
from tetrabench.harnesses import compile_agent_config, seal_harness
from tetrabench.native_control import ControlProcess
from tetrabench.native_discovery import collect_installed


def transport_config(selector):
    model: dict[str, Any] = {
        "name": "Synthetic transport fixture",
        "reasoning": True,
        "limit": {"context": 32768, "output": 8192},
    }
    provider: dict[str, Any] = {"models": {"transport-fixture": model}}
    target = (
        model.setdefault("provider", {}) if selector.startswith("model.") else provider
    )
    key = selector.split(".")[-1]
    target[key] = (
        "https://synthetic.invalid/v2/responses"
        if key == "api"
        else "@ai-sdk/openai-compatible"
    )
    return {"provider": {"openai": provider}}


@pytest.mark.parametrize(
    "selector", ["provider.api", "provider.npm", "model.api", "model.npm"]
)
@pytest.mark.parametrize("resource", [False, True])
def test_explicit_auth_refuses_native_transport_override_at_every_layer(
    tmp_path, selector, resource
):
    native = transport_config(selector)
    (tmp_path / "override.json").write_text(json.dumps(native))
    with pytest.raises(ValueError, match="endpoint/SDK"):
        spec = HarnessConfig(
            name="opencode",
            version="1.18.30",
            model="openai/transport-fixture",
            auth=AuthSpec(
                mode="chatgpt_oauth",
                reference=NativeAuthReference(
                    profile="eval", binding="local", generation=1
                ),
            ),
            native_config=None if resource else NativeConfig(text=json.dumps(native)),
            resources=[
                ResourceSource(
                    source="override.json", destination="opencode/opencode.json"
                )
            ]
            if resource
            else [],
        )
        seal_harness(spec, tmp_path)


@pytest.mark.native
@pytest.mark.parametrize("resource", [False, True])
def test_native_opencode_api_url_really_uses_model_transport_override(
    tmp_path, resource
):
    modules = native_modules(required=True)
    assert modules is not None
    native = transport_config("model.api")
    (tmp_path / "override.json").write_text(json.dumps(native))
    # Legacy explicit config remains an intentional alternative transport path.
    spec = HarnessConfig(
        name="opencode",
        version="1.18.30",
        model="openai/transport-fixture",
        native_config=None if resource else NativeConfig(text=json.dumps(native)),
        resources=[
            ResourceSource(source="override.json", destination="opencode/opencode.json")
        ]
        if resource
        else [],
    )
    snapshot = collect_installed(
        spec, base=tmp_path, modules=modules, reuse_native_cache=False
    )
    assert snapshot.identity.endpoints == ("https://synthetic.invalid/v2/responses",), (
        snapshot.limitations
    )
    assert snapshot.identity.resolved_model == "transport-fixture"


def test_claude_context_suffix_preserved_in_model_aliases_and_quoted_flags(tmp_path):
    model = "claude-opus-5[1m]"
    spec = HarnessConfig(
        name="claude-code",
        version="2.1.267",
        model="anthropic/" + model,
        options={"fallback_model": model},
    )
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(spec, tmp_path)), logs_dir=tmp_path
    )
    assert "--fallback-model=" + model in shlex.split(instance.build_cli_flags())
    capture = CaptureEnvironment("claude-code", "2.1.267")
    asyncio.run(instance.run("not executed", capture, AgentContext()))
    assert any(
        row.get("env", {}).get("ANTHROPIC_MODEL") == model for row in capture.commands
    )
    assert any(
        row.get("env", {}).get("ANTHROPIC_DEFAULT_HAIKU_MODEL") == model
        for row in capture.commands
    )


@pytest.mark.parametrize(
    "selector",
    [
        "claude-opus-5[1m];touch /tmp/no",
        "claude-opus-5[$(id)]",
        "claude-opus-5[1m] --version",
        "claude-opus-5[2m]",
        "claude-opus-5[1m][1m]",
        "claude-opus-5[*]",
    ],
)
def test_context_suffix_does_not_expand_the_injection_surface(selector):
    with pytest.raises(ValueError):
        HarnessConfig(
            name="claude-code", version="2.1.267", model="anthropic/" + selector
        )


@pytest.mark.native
def test_actual_claude_initialization_model_selectors_are_accepted(tmp_path):
    modules = native_modules(required=True)
    assert modules is not None
    env = native_environment(tmp_path)
    command = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        str(modules / "@anthropic-ai/claude-code-linux-x64/claude"),
        "--print",
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--verbose",
        "--setting-sources=",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    with ControlProcess(command, tmp_path, env) as process:
        process.send(
            {
                "type": "control_request",
                "request_id": "models",
                "request": {
                    "subtype": "initialize",
                    "hooks": {},
                    "agents": {},
                    "sdkMcpServers": [],
                    "plugins": [],
                },
            }
        )
        for _ in range(50):
            message = json.loads(process.line())
            if message.get("type") == "control_response":
                rows = message["response"]["response"]["models"]
                break
        else:
            pytest.fail("native supportedModels initialization response missing")
    contextual = [row["value"] for row in rows if row["value"].endswith("[1m]")]
    assert contextual, rows
    for selector in contextual:
        spec = HarnessConfig(
            name="claude-code", version="2.1.267", model="anthropic/" + selector
        )
        assert seal_harness(spec, tmp_path).model == "anthropic/" + selector
