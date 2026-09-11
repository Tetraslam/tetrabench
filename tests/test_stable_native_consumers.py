"""Real, offline consumers for the advertised pins, not historical fixtures.

CI provisions the locked official packages before pytest and requires this suite.
Only native help/parsers and in-memory helpers execute; no login or LLM calls.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from native_consumer_support import (
    VERSIONS,
    native_environment,
    native_modules,
    native_run,
)
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.harness_config import HarnessConfig, NativeConfig
from tetrabench.harnesses import capabilities, compile_agent_config, seal_harness

pytestmark = pytest.mark.native


@pytest.fixture
def modules() -> Path:
    root = native_modules(required=True)
    assert root is not None
    return root


def executable(modules: Path, name: str) -> list[str]:
    if name == "opencode":
        return [str(modules / "opencode-linux-x64/bin/opencode")]
    if name == "claude-code":
        return [str(modules / "@anthropic-ai/claude-code-linux-x64/claude")]
    entrypoint = (
        "@openai/codex/bin/codex.js"
        if name == "codex"
        else "@earendil-works/pi-coding-agent/dist/cli.js"
    )
    return ["node", str(modules / entrypoint)]


def make_agent(tmp_path, name, *, options=None, native=None, env=None) -> Any:
    spec = HarnessConfig(
        name=name,
        version=VERSIONS[name],
        model="anthropic/claude-sonnet-4-6"
        if name == "claude-code"
        else "openai/gpt-5",
        options=options or {},
        native_config=NativeConfig(text=json.dumps(native)) if native else None,
        env=env or {},
    )
    return AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(spec, tmp_path)), logs_dir=tmp_path
    )


@pytest.mark.parametrize("name", VERSIONS)
def test_official_consumer_version_and_help(modules, tmp_path, name):
    advertised = next(entry for entry in capabilities() if entry["name"] == name)
    assert advertised["stable_version"] == VERSIONS[name]
    command = executable(modules, name)
    result = native_run([*command, "--version"], tmp_path)
    assert result.returncode == 0, result.stderr
    expected = {
        "opencode": VERSIONS[name],
        "codex": f"codex-cli {VERSIONS[name]}",
        "claude-code": f"{VERSIONS[name]} (Claude Code)",
        "pi": VERSIONS[name],
    }[name]
    assert result.stdout.strip() == expected
    subcommand = {"opencode": ["run"], "codex": ["exec"]}.get(name, [])
    result = native_run([*command, *subcommand, "--help"], tmp_path)
    assert result.returncode == 0, result.stderr
    expected_flags = {
        "opencode": ["--auto", "--variant", "--session"],
        "codex": ["--config", "--dangerously-bypass-approvals-and-sandbox"],
        "claude-code": ["--effort", "--autocompact", "--settings"],
        "pi": ["--thinking", "--session", "--offline"],
    }
    for flag in expected_flags[name]:
        assert flag in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code"])
def test_compiled_command_reaches_native_parser(modules, tmp_path, name):
    options = {
        "opencode": {"variant": "high", "title": "a quoted title"},
        "codex": {"reasoning_effort": "high", "web_search": "disabled"},
        "claude-code": {
            "reasoning_effort": "high",
            "append_system_prompt": "two words",
        },
    }[name]
    instance = make_agent(tmp_path, name, options=options)
    capture = CaptureEnvironment(name, VERSIONS[name])
    asyncio.run(instance.run("fixture prompt", capture, AgentContext()))
    binary = {"opencode": "opencode", "codex": "codex", "claude-code": "claude"}[name]
    commands = [shlex.split(item["command"]) for item in capture.commands]
    # Only inspect the solve command. Never execute captured shell/setup commands.
    tokens = next(parts for parts in commands if binary in parts and "2>&1" in parts)
    arguments = tokens[tokens.index(binary) + 1 : tokens.index("2>&1")]
    for index, (remote, payload) in enumerate(capture.uploads.items()):
        local = tmp_path / f"uploaded-{index}-{Path(remote).name}"
        local.write_bytes(payload)
        arguments = [str(local) if value == remote else value for value in arguments]
    arguments.insert(arguments.index("--") if "--" in arguments else 0, "--help")
    result = native_run([*executable(modules, name), *arguments], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout or "opencode run" in result.stdout + result.stderr
    assert "Unknown argument" not in result.stderr


def test_pi_compiled_flags_are_consumed_without_starting_agent(modules, tmp_path):
    instance = make_agent(tmp_path, "pi", options={"thinking": "high"})
    module = modules / "@earendil-works/pi-coding-agent/dist/cli/args.js"
    program = (
        f"import {{parseArgs}} from {json.dumps(module.as_uri())};"
        "const parsed = parseArgs(JSON.parse(process.argv[1]));"
        "console.log(JSON.stringify({"
        "...parsed, unknownFlags:[...parsed.unknownFlags]}));"
    )
    result = native_run(
        [
            "node",
            "--input-type=module",
            "-e",
            program,
            json.dumps(shlex.split(instance.build_cli_flags())),
        ],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["thinking"] == "high"
    assert parsed["unknownFlags"] == []
    assert parsed["diagnostics"] == []


@pytest.mark.parametrize("enabled", [False, True])
def test_codex_rust_loads_uploaded_config_and_rejects_invalid_types(
    modules, tmp_path, enabled
):
    instance = make_agent(
        tmp_path, "codex", native={"features": {"multi_agent": enabled}}
    )
    capture = CaptureEnvironment("codex", VERSIONS["codex"])
    asyncio.run(instance.run("not executed", capture, AgentContext()))
    payload = next(
        data for name, data in capture.uploads.items() if name.endswith("/config.toml")
    )
    home = Path(native_environment(tmp_path)["CODEX_HOME"])
    config = home / "config.toml"
    config.write_bytes(payload)
    command = [*executable(modules, "codex"), "features", "list"]
    result = native_run(command, tmp_path)
    assert result.returncode == 0, result.stderr
    row = next(
        line.split()
        for line in result.stdout.splitlines()
        if line.split()[0] == "multi_agent"
    )
    assert row[-1] == str(enabled).lower()
    # `--help` alone exits before config loading. This negative control proves
    # the Rust consumer really parsed the uploaded TOML, without starting a run.
    config.write_bytes(b'model_context_window = "not-an-integer"\n' + payload)
    result = native_run(command, tmp_path)
    assert result.returncode != 0
    assert "model_context_window" in result.stderr


@pytest.mark.parametrize("advertised", [False, True])
def test_pi_current_native_default_price_is_indistinguishable_from_zero(
    modules, tmp_path, advertised
):
    cost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    config = {
        "api": "openai-responses",
        "baseUrl": "https://example.test/v1",
        "apiKey": "${TOKEN}",
        "models": [{"id": "fixture", **({"cost": cost} if advertised else {})}],
    }
    module = modules / "@earendil-works/pi-coding-agent/dist/core/provider-composer.js"
    program = (
        f"import {{composeModelProvider}} from {json.dumps(module.as_uri())};"
        "const config = JSON.parse(process.argv[1]);"
        "const provider = composeModelProvider('custom', undefined, "
        "{getProvider: () => config}, undefined);"
        "console.log(JSON.stringify(provider.getModels()[0].cost));"
    )
    result = native_run(
        ["node", "--input-type=module", "-e", program, json.dumps(config)], tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == cost


def test_packaged_reasoning_helper_consumes_actual_pi_sdk(modules, tmp_path):
    helper = files("tetrabench").joinpath("native_reasoning.mjs")
    composer = (
        modules / "@earendil-works/pi-coding-agent/dist/core/provider-composer.js"
    )
    package = modules / "@earendil-works/pi-coding-agent/package.json"
    program = (
        f"import {{capturePiModel}} from {json.dumps(Path(str(helper)).as_uri())};"
        f"import {{composeModelProvider}} from {json.dumps(composer.as_uri())};"
        "const sdk = await import(import.meta.resolve('@earendil-works/pi-ai',"
        f"{json.dumps(package.as_uri())}));"
        "const config = {api:'openai-responses', baseUrl:'https://example.test/v1',"
        "apiKey:'${TOKEN}', models:[{id:'fixture', reasoning:true,"
        "thinkingLevelMap:{minimal:null, xhigh:'extreme'}}]};"
        "const provider = composeModelProvider('custom', undefined,"
        "{getProvider:()=>config}, undefined);"
        "const runtime = sdk.createModels(); runtime.setProvider(provider);"
        "console.log(capturePiModel(runtime, sdk, 'custom', 'fixture'));"
    )
    result = native_run(
        [
            "node",
            "--experimental-import-meta-resolve",
            "--input-type=module",
            "-e",
            program,
        ],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    observation = json.loads(result.stdout)
    assert observation["model"]["id"] == "fixture"
    assert "minimal" not in observation["supportedThinkingLevels"]
    assert "xhigh" in observation["supportedThinkingLevels"]
    assert observation["normalizations"]["minimal"] != "minimal"
    assert "apiKey" not in result.stdout


@pytest.mark.parametrize("generated", [False, True])
def test_pi_uploaded_credentials_reach_current_native_resolver(
    modules, tmp_path, monkeypatch, generated
):
    monkeypatch.setenv("HOST_TOKEN", "fixture-token")
    monkeypatch.setenv("HOST_ENDPOINT", "https://example.test/v1")
    instance = make_agent(
        tmp_path,
        "pi",
        options={"model_api": "openai-responses"} if generated else {},
        native=None
        if generated
        else {
            "models": {
                "providers": {
                    "custom": {
                        "apiKey": "${TOKEN}",
                        "api": "openai-responses",
                        "baseUrl": "https://example.test/v1",
                        "models": [{"id": "gpt-5"}],
                    }
                }
            }
        },
        env={"OPENAI_API_KEY": "${HOST_TOKEN}", "OPENAI_BASE_URL": "${HOST_ENDPOINT}"}
        if generated
        else {"TOKEN": "${HOST_TOKEN}"},
    )
    capture = CaptureEnvironment("pi", VERSIONS["pi"])
    asyncio.run(instance.run("not executed", capture, AgentContext()))
    models = json.loads(capture.uploads["/tmp/harbor-pi-agent/models.json"])
    selector = models["providers"]["harbor-endpoint" if generated else "custom"][
        "apiKey"
    ]
    module = (
        modules / "@earendil-works/pi-coding-agent/dist/core/resolve-config-value.js"
    )
    program = (
        f"import {{resolveConfigValue}} from {json.dumps(module.as_uri())};"
        "console.log(JSON.stringify(resolveConfigValue(process.argv[1])));"
    )
    result = native_run(
        ["node", "--input-type=module", "-e", program, selector],
        tmp_path,
        instance.extra_env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == "fixture-token"
    assert "fixture-token" not in json.dumps(models)
