"""Native parser boundaries, independently of shell quoting and model execution."""

import json
import re
import shlex
import subprocess
import sys

import pytest
from native_consumer_support import VERSIONS, native_environment, native_modules
from test_harness_native_consumers import agent
from test_stable_native_consumers import executable, make_agent


def _argv(instance):
    consumer = shlex.join(
        [sys.executable, "-c", "import json,sys;print(json.dumps(sys.argv[1:]))"]
    )
    return json.loads(
        subprocess.check_output(
            ["/bin/sh", "-c", consumer + " " + instance.build_cli_flags()]
        )
    )


def _modules():
    root = native_modules(required=True)
    assert root is not None
    return root


def _environment(tmp_path):
    return native_environment(tmp_path)


@pytest.mark.native
@pytest.mark.parametrize(
    "value", ["--version", "--model=openai/other", "two words $(printf injected)"]
)
def test_opencode_values_cannot_become_native_options(tmp_path, value):
    modules = _modules()
    instance = make_agent(
        tmp_path, "opencode", options={"title": value, "variant": value}
    )
    arguments = ["--model=openai/model", "run", *_argv(instance), "--auto"]
    # A deliberately unknown final option forces the actual native parser to
    # stop before its handler. A separate --version value incorrectly exits 0.
    executable = modules / "opencode-linux-x64/bin/opencode"
    assert (
        subprocess.check_output(
            [str(executable), "--version"],
            text=True,
            env=_environment(tmp_path),
            timeout=20,
        ).strip()
        == VERSIONS["opencode"]
    )
    result = subprocess.run(
        [str(executable), *arguments, "--tetrabench-invalid-option"],
        env=_environment(tmp_path),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stderr.lstrip().startswith("opencode run [message..]")
    assert result.stdout.strip() != VERSIONS["opencode"]
    if value == "--version":
        broken = subprocess.run(
            [
                str(executable),
                "run",
                "--title",
                "--version",
                "--tetrabench-invalid-option",
            ],
            env=_environment(tmp_path),
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert broken.returncode == 0
        assert broken.stdout.strip() == VERSIONS["opencode"]


@pytest.mark.native
@pytest.mark.parametrize(
    "value",
    ["--version", "--model=openai/other", "words with spaces $(printf injected)"],
)
@pytest.mark.parametrize(
    "field", ["append_system_prompt", "allowed_tools", "disallowed_tools"]
)
def test_claude_native_parser_accepts_bound_text_and_tool_values(
    tmp_path, value, field
):
    modules = _modules()
    options = {
        "append_system_prompt": "two words",
        "allowed_tools": "Bash(git:*)",
        "disallowed_tools": "Bash(rm:*)",
        "max_turns": 3,
        "permission_mode": "acceptEdits",
    }
    options[field] = value
    instance = make_agent(tmp_path, "claude-code", options=options)
    arguments = _argv(instance)
    command = executable(modules, "claude-code")
    assert (
        subprocess.check_output(
            [*command, "--version"], text=True, env=_environment(tmp_path), timeout=20
        ).strip()
        == f"{VERSIONS['claude-code']} (Claude Code)"
    )
    result = subprocess.run(
        [*command, *arguments, "--tetrabench-invalid-option"],
        env=_environment(tmp_path),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "unknown option '--tetrabench-invalid-option'" in result.stderr
    assert "argument missing" not in result.stderr
    assert result.stdout.strip() != f"{VERSIONS['claude-code']} (Claude Code)"


@pytest.mark.native
def test_codex_native_parser_accepts_equals_config_assignments(tmp_path):
    modules = _modules()
    instance = make_agent(
        tmp_path,
        "codex",
        options={
            "reasoning_effort": "medium",
            "reasoning_summary": "detailed",
            "web_search": "disabled",
        },
    )
    arguments = _argv(instance)
    command = executable(modules, "codex")
    assert (
        subprocess.check_output(
            [*command, "--version"], text=True, env=_environment(tmp_path), timeout=20
        ).strip()
        == f"codex-cli {VERSIONS['codex']}"
    )
    result = subprocess.run(
        [*command, "exec", *arguments, "--tetrabench-invalid-option"],
        env=_environment(tmp_path),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "unexpected argument '--tetrabench-invalid-option'" in result.stderr
    assert "invalid value" not in result.stderr
    for value in ("--version", "--model=openai/other"):
        selected = make_agent(tmp_path, "codex", options={"reasoning_effort": value})
        result = subprocess.run(
            [*command, "exec", *_argv(selected), "--tetrabench-invalid-option"],
            env=_environment(tmp_path),
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode != 0
        assert "unexpected argument '--tetrabench-invalid-option'" in result.stderr
        assert result.stdout.strip() != f"codex-cli {VERSIONS['codex']}"


@pytest.mark.native
def test_pi_native_parser_requires_separate_enum_value(tmp_path):
    modules = _modules()
    instance = make_agent(tmp_path, "pi", options={"thinking": "high"})
    module = modules / "@earendil-works/pi-coding-agent/dist/cli/args.js"
    program = (
        f"import {{parseArgs}} from {json.dumps(module.as_uri())};"
        "const parsed = parseArgs(JSON.parse(process.argv[1]));"
        "console.log(JSON.stringify({...parsed, "
        "unknownFlags:[...parsed.unknownFlags]}));"
    )

    def parse(arguments):
        return json.loads(
            subprocess.check_output(
                ["node", "--input-type=module", "-e", program, json.dumps(arguments)],
                env=_environment(tmp_path),
            )
        )

    parsed = parse(
        ["--provider", "openai", "--model", "model", "--print", *_argv(instance)]
    )
    assert parsed["thinking"] == "high"
    assert parsed["model"] == "model" and parsed["provider"] == "openai"
    assert parsed["print"] is True and not parsed.get("version")
    assert parsed["unknownFlags"] == [] and parsed["diagnostics"] == []
    unsupported = parse(["--thinking=high"])
    assert "thinking" not in unsupported
    assert unsupported["unknownFlags"] == [["thinking", "high"]]
    with pytest.raises(ValueError):
        make_agent(tmp_path, "pi", options={"thinking": "--version"})


@pytest.mark.parametrize("section", ["model_providers", "mcp_servers"])
@pytest.mark.parametrize(
    "value",
    [
        "${OPENAI_API_KEY}",
        "$OPENAI_API_KEY",
        "Bearer ${OPENAI_API_KEY}",
        "literal-token",
    ],
)
def test_codex_literal_http_headers_cannot_carry_credentials(
    tmp_path, monkeypatch, section, value
):
    monkeypatch.setenv("HOST_TOKEN", "fixture-token")
    with pytest.raises(ValueError, match="env_http_headers"):
        agent(
            tmp_path,
            "codex",
            native={section: {"custom": {"http_headers": {"Authorization": value}}}},
            env={"OPENAI_API_KEY": "${HOST_TOKEN}"},
        )


@pytest.mark.parametrize("section", ["model_providers", "mcp_servers"])
def test_codex_static_headers_and_native_environment_headers_coexist(
    tmp_path, monkeypatch, section
):
    monkeypatch.setenv("HOST_TOKEN", "Bearer fixture-token")
    native = {
        section: {
            "custom": {
                "http_headers": {
                    "X-Experiment": "static-value",
                    "User-Agent": "tetrabench test",
                    "X-Template": "${LITERAL_LABEL}",
                },
                "env_http_headers": {"Authorization": "AUTH_HEADER"},
            }
        }
    }
    instance = agent(
        tmp_path, "codex", native=native, env={"AUTH_HEADER": "${HOST_TOKEN}"}
    )
    config = instance._build_effective_config()[section]["custom"]
    # Codex 0.114's build_header_map copies literal values, then reads each
    # env_http_headers selector from the environment. It never interpolates.
    headers = dict(config["http_headers"])
    headers.update(
        {
            name: instance.extra_env[variable]
            for name, variable in config["env_http_headers"].items()
        }
    )
    assert headers == {
        "X-Experiment": "static-value",
        "User-Agent": "tetrabench test",
        "X-Template": "${LITERAL_LABEL}",
        "Authorization": "Bearer fixture-token",
    }
    assert "fixture-token" not in json.dumps(config)


def test_other_harness_headers_use_their_own_reference_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_TOKEN", "fixture-token")
    opencode = agent(
        tmp_path,
        "opencode",
        native={
            "provider": {
                "openai": {
                    "options": {
                        "headers": {
                            "Authorization": "Bearer {env:TOKEN}",
                            "X-Static": "plain",
                        }
                    }
                }
            }
        },
        env={"TOKEN": "${HOST_TOKEN}"},
    )
    text = json.dumps(opencode._opencode_config)
    # ConfigVariable.substitute in OpenCode 1.18.29 expands {env:...} in JSON
    # text before parsing; dollar forms are not that native syntax.
    expanded = re.sub(
        r"\{env:([A-Z][A-Z0-9_]*)\}",
        lambda match: json.dumps(opencode.extra_env[match[1]])[1:-1],
        text,
    )
    assert (
        json.loads(expanded)["provider"]["openai"]["options"]["headers"][
            "Authorization"
        ]
        == "Bearer fixture-token"
    )
    with pytest.raises(ValueError, match="OpenCode header references"):
        agent(
            tmp_path,
            "opencode",
            native={
                "provider": {
                    "openai": {
                        "options": {"headers": {"Authorization": "${OPENAI_API_KEY}"}}
                    }
                }
            },
            env={"OPENAI_API_KEY": "${HOST_TOKEN}"},
        )
    with pytest.raises(ValueError, match="Claude settings headers"):
        agent(
            tmp_path,
            "claude-code",
            native={"headers": {"Authorization": "${ANTHROPIC_API_KEY}"}},
            env={"ANTHROPIC_API_KEY": "${HOST_TOKEN}"},
        )


@pytest.mark.native
def test_pi_native_header_resolver_preserves_static_values(tmp_path, monkeypatch):
    modules = _modules()
    monkeypatch.setenv("HOST_TOKEN", "Bearer fixture-token")
    instance = make_agent(
        tmp_path,
        "pi",
        native={
            "models": {
                "providers": {
                    "openai": {
                        "headers": {
                            "Authorization": "${AUTH_HEADER}",
                            "X-Static": "plain",
                        }
                    }
                }
            }
        },
        env={"AUTH_HEADER": "${HOST_TOKEN}"},
    )
    headers = instance._pi_native["models"]["providers"]["openai"]["headers"]
    module = (
        modules / "@earendil-works/pi-coding-agent/dist/core/resolve-config-value.js"
    )
    program = (
        f"import {{resolveHeadersOrThrow}} from {json.dumps(module.as_uri())}; "
        "console.log(JSON.stringify(resolveHeadersOrThrow("
        "JSON.parse(process.argv[1]), 'fixture')));"
    )
    result = json.loads(
        subprocess.check_output(
            ["node", "--input-type=module", "-e", program, json.dumps(headers)],
            env=dict(_environment(tmp_path), **instance.extra_env),
        )
    )
    assert result == {"Authorization": "Bearer fixture-token", "X-Static": "plain"}


def test_explicit_args_equal_boundary_accepts_literal_option_shape(tmp_path):
    from tetrabench.harness_config import HarnessConfig
    from tetrabench.harnesses import normalized_options

    spec = HarnessConfig(
        name="opencode",
        version="1.18.29",
        model="openai/model",
        args=["--title=--version"],
    )
    assert normalized_options(spec)["title"] == "--version"
