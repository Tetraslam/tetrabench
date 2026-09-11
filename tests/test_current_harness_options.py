"""Current native flags, context defaults, and argv boundaries."""

import asyncio
import json
import shlex
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from native_consumer_support import native_modules, native_run
from test_controlled_harnesses import CaptureEnvironment
from test_harness_resources import spec

from tetrabench.harness_config import NativeConfig
from tetrabench.harnesses import (
    STABLE_VERSIONS,
    capabilities,
    compile_agent_config,
    seal_harness,
)


def make(tmp_path, name, **kwargs):
    return AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(spec(name, **kwargs), tmp_path)),
        logs_dir=tmp_path,
    )


def test_claude_controls_are_typed_and_distinguish_tools_from_permissions(tmp_path):
    instance = make(
        tmp_path,
        "claude-code",
        options={
            "tools": "",
            "allowed_tools": "Bash(git *)",
            "max_output_tokens": 32768,
            "disable_adaptive_thinking": False,
            "autocompact": "200k",
            "reasoning_effort": "max",
            "permission_mode": "manual",
        },
        discovery="isolated",
    )
    argv = shlex.split(instance.build_cli_flags())
    assert "--tools=" in argv
    assert "--allowedTools=Bash(git *)" in argv
    assert "--setting-sources=" in argv
    assert "--autocompact=200k" in argv
    assert "--permission-mode=manual" in argv
    capture = CaptureEnvironment("claude-code", "2.1.267")
    asyncio.run(instance.run("instruction", capture, AgentContext()))
    environments = [item.get("env", {}) for item in capture.commands]
    assert any(
        env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "32768" for env in environments
    )
    assert not any(
        env.get("CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING") == "1" for env in environments
    )
    assert not any("DISABLE_AUTO_COMPACT" in env for env in environments)


@pytest.mark.parametrize(
    "options",
    [
        {"reasoning_effort": "ultracode"},
        {"permission_mode": "default"},
        {"max_output_tokens": "100"},
        {"disable_adaptive_thinking": "true"},
        {"autocompact": "5k"},
        {"autocompact": "200k", "disable_auto_compact": True},
    ],
)
def test_invalid_current_claude_options_rejected(options):
    with pytest.raises(ValueError):
        spec("claude-code", options=options)


def test_native_policy_does_not_force_claude_auxiliary_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("ENDPOINT", "https://example.test")
    instance = make(
        tmp_path,
        "claude-code",
        ancillary_models="native",
        env={"ANTHROPIC_BASE_URL": "${ENDPOINT}"},
    )
    capture = CaptureEnvironment("claude-code", "2.1.267")
    asyncio.run(instance.run("instruction", capture, AgentContext()))
    for item in capture.commands:
        env = item.get("env", {})
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
        assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env


def test_current_codex_preserves_nested_model_and_native_context_defaults(tmp_path):
    harness = spec("codex").model_copy(update={"model": "openrouter/vendor/model"})
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(harness, tmp_path)), logs_dir=tmp_path
    )
    assert "model_reasoning_effort" not in instance.build_cli_flags()
    assert not any("compact" in key for key in instance._build_effective_config())
    capture = CaptureEnvironment("codex", "0.154.0")
    asyncio.run(instance.run("instruction", capture, AgentContext()))
    command = next(
        item["command"] for item in capture.commands if "codex exec " in item["command"]
    )
    argv = shlex.split(command)
    assert "--model=vendor/model" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_codex_efforts_are_not_a_global_six_value_enum(tmp_path):
    instance = make(tmp_path, "codex", options={"reasoning_effort": "native-future"})
    assert '--config=model_reasoning_effort="native-future"' in shlex.split(
        instance.build_cli_flags()
    )
    descriptor: Any = next(item for item in capabilities() if item["name"] == "codex")
    assert descriptor["option_details"]["reasoning_effort"]["choices"] is None


def test_opencode_native_policy_and_isolation_preserve_compaction(tmp_path):
    instance = make(
        tmp_path,
        "opencode",
        ancillary_models="native",
        discovery="isolated",
        native_config=NativeConfig(
            format="jsonc",
            text='{// native\n"compaction":{"auto":true,"prune":true,},}',
        ),
    )
    assert "--title=" not in instance.build_cli_flags()
    capture = CaptureEnvironment("opencode", "1.18.30")
    asyncio.run(instance.run("instruction", capture, AgentContext()))
    env = next(
        item["env"]
        for item in capture.commands
        if " run --format=json " in item["command"]
    )
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["XDG_CONFIG_HOME"] == "/tmp/tetrabench-opencode-config"
    effective = json.loads(instance._effective_configs["opencode.json"])
    assert effective["compaction"] == {"auto": True, "prune": True}
    assert "small_model" not in effective


@pytest.mark.native
def test_pi_real_parser_receives_new_options_and_bound_prompt(tmp_path):
    modules = native_modules(required=True)
    assert modules is not None
    module = modules / "@earendil-works/pi-coding-agent/dist/cli/args.js"
    instance = make(
        tmp_path,
        "pi",
        discovery="isolated",
        options={
            "thinking": "max",
            "no_builtin_tools": True,
            "offline": True,
            "system_prompt": "--version two words",
            "tools": "read,grep",
            "name": "native session",
        },
    )
    argv = shlex.split(instance.build_cli_flags())
    assert "--thinking=max" not in argv
    program = (
        f"import {{parseArgs}} from {json.dumps(module.as_uri())};"
        "const p=parseArgs(JSON.parse(process.argv[1]));"
        "console.log(JSON.stringify({...p,unknownFlags:[...p.unknownFlags]}));"
    )
    result = native_run(
        ["node", "--input-type=module", "-e", program, json.dumps(argv)], tmp_path
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["thinking"] == "max"
    assert parsed["systemPrompt"] == "--version two words"
    assert parsed["noBuiltinTools"] and parsed["offline"]
    assert parsed["noContextFiles"] and parsed["noExtensions"]
    assert parsed["unknownFlags"] == [] and parsed["diagnostics"] == []


@pytest.mark.parametrize("name", STABLE_VERSIONS)
def test_capability_listing_never_resolves_environment_or_runs_helper(
    monkeypatch, name
):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-appear")
    assert "must-not-appear" not in json.dumps(capabilities())


@pytest.mark.native
def test_pi_native_resource_loader_consumes_uploaded_bytes(tmp_path):
    from tetrabench.harness_config import ResourceSource
    from tetrabench.resources import AGENT_RESOURCE_ROOT

    modules = native_modules(required=True)
    assert modules is not None
    (tmp_path / "system.md").write_text("Native system resource, unchanged.")
    (tmp_path / "SKILL.md").write_text(
        "---\nname: native-check\ndescription: Check native loading\n---\n"
        "Native skill resource\n"
    )
    instance = make(
        tmp_path,
        "pi",
        discovery="isolated",
        options={
            "system_prompt": "resource:system.md",
            "skill": "resource:skills/native-check/SKILL.md",
        },
        resources=[
            ResourceSource(source="system.md", destination="system.md"),
            ResourceSource(
                source="SKILL.md", destination="skills/native-check/SKILL.md"
            ),
        ],
    )
    capture = CaptureEnvironment("pi", "0.85.1")
    asyncio.run(instance.run("not executed", capture, AgentContext()))
    # Materialize exactly the uploaded bytes in a host sandbox. Only its root
    # differs from the container root; the native parser and loader are real.
    sandbox = tmp_path / "sandbox"
    for path, content in capture.uploads.items():
        if path.startswith(AGENT_RESOURCE_ROOT):
            target = sandbox / path.removeprefix(AGENT_RESOURCE_ROOT + "/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    argv = [
        arg.replace(AGENT_RESOURCE_ROOT, str(sandbox))
        for arg in shlex.split(instance.build_cli_flags())
    ]
    args_module = modules / "@earendil-works/pi-coding-agent/dist/cli/args.js"
    loader_module = (
        modules / "@earendil-works/pi-coding-agent/dist/core/resource-loader.js"
    )
    program = (
        f"import {{parseArgs}} from {json.dumps(args_module.as_uri())};"
        f"import {{DefaultResourceLoader}} from {json.dumps(loader_module.as_uri())};"
        "const a=parseArgs(JSON.parse(process.argv[1]));"
        "const loader=new DefaultResourceLoader({cwd:process.cwd(),"
        "agentDir:process.env.PI_CODING_AGENT_DIR,systemPrompt:a.systemPrompt,"
        "additionalSkillPaths:a.skills,noSkills:a.noSkills,noExtensions:a.noExtensions,"
        "noContextFiles:a.noContextFiles,noThemes:true,noPromptTemplates:true});"
        "await loader.reload(); console.log(JSON.stringify({"
        "system:loader.getSystemPrompt(),skills:loader.getSkills(),"
        "context:loader.getAgentsFiles()}));"
    )
    result = native_run(
        ["node", "--input-type=module", "-e", program, json.dumps(argv)], tmp_path
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["system"] == "Native system resource, unchanged."
    assert evidence["skills"]["skills"][0]["name"] == "native-check"
    assert evidence["context"]["agentsFiles"] == []
