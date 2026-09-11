"""Portable run inputs and native lifecycle wiring without task mutation."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.authoring import initialize_project
from tetrabench.canonical_json import sha256_hex
from tetrabench.config import load_harness_override
from tetrabench.harness_config import (
    HarnessConfig,
    HarnessSession,
    NativeConfig,
    ResolvedHarness,
    ResourceSource,
)
from tetrabench.harnesses import (
    STABLE_VERSIONS,
    compile_agent_config,
    parse_native,
    seal_harness,
)
from tetrabench.models import ConfigOverrides
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.records import RequestRecord
from tetrabench.resources import (
    AGENT_RESOURCE_ROOT,
    MAX_RESOURCE_FILE_BYTES,
    materialize_resources,
)
from tetrabench.submission import prepare_run


def spec(name="opencode", **kwargs):
    return HarnessConfig(
        name=name, version=STABLE_VERSIONS[name], model="openai/model", **kwargs
    )


def test_resource_sealing_portability_and_task_identity(tmp_path):
    root = initialize_project(tmp_path / "project")
    baseline = prepare_run(root, "example", run_id="same")
    (root / "rules.md").write_text("Keep native context management.\n")
    harness = spec(
        env={"OPENAI_API_KEY": "${MODEL_KEY}"},
        native_config=NativeConfig(
            format="jsonc", text='{// comment\n"instructions":["rules.md",],}'
        ),
    )
    prepared = prepare_run(
        root, "example", run_id="same", overrides=ConfigOverrides(harness=harness)
    )
    assert prepared.plan.harness is not None
    resource = prepared.plan.harness.resources[0]
    assert resource.text == "Keep native context management.\n"
    assert parse_native(prepared.plan.harness.native_config)["instructions"] == [
        f"{AGENT_RESOURCE_ROOT}/{resource.destination}"
    ]
    assert (
        baseline.request.context_manifest_sha256
        == prepared.request.context_manifest_sha256
    )
    assert baseline.plan.context == prepared.plan.context
    assert baseline.plan.trials == prepared.plan.trials
    data = canonical_model_bytes(prepared.request)
    assert str(root).encode() not in data
    assert canonical_model_bytes(parse_canonical_model(data, RequestRecord)) == data
    (tmp_path / "other").mkdir()
    (tmp_path / "other/rules.md").write_text(resource.text)
    portable = seal_harness(harness, tmp_path / "other")
    assert canonical_model_bytes(portable) == canonical_model_bytes(
        prepared.plan.harness
    )
    (root / "rules.md").write_text("Changed instructions\n")
    changed = prepare_run(
        root, "example", run_id="same", overrides=ConfigOverrides(harness=harness)
    )
    assert changed.request.plan_sha256 != prepared.request.plan_sha256
    assert (
        changed.request.context_manifest_sha256
        == baseline.request.context_manifest_sha256
    )


@pytest.mark.parametrize(
    "filename", ["auth.json", ".credentials.json", "secret.txt", ".env", "image.png"]
)
def test_resource_rejects_credential_and_binary_names(tmp_path, filename):
    (tmp_path / filename).write_text("{}")
    with pytest.raises(ValueError):
        seal_harness(
            spec(
                resources=[ResourceSource(source=filename, destination="innocent.json")]
            ),
            tmp_path,
        )


@pytest.mark.parametrize("directory", [False, True])
def test_resource_sealer_rejects_symlink_ancestors_and_files(tmp_path, directory):
    (tmp_path / "real").mkdir()
    (tmp_path / "real/rules.md").write_text("safe")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError):
        seal_harness(
            spec(
                resources=[
                    ResourceSource(
                        source="link" if directory else "link/rules.md",
                        destination="rules.md",
                        directory=directory,
                    )
                ]
            ),
            tmp_path,
        )


def test_resource_bounds_collisions_and_snapshot_mutation(tmp_path):
    (tmp_path / "rules.md").write_text("x" * (MAX_RESOURCE_FILE_BYTES + 1))
    resource = ResourceSource(source="rules.md", destination="rules.md")
    with pytest.raises(ValueError):
        seal_harness(spec(resources=[resource]), tmp_path)
    (tmp_path / "rules.md").write_text("small")
    with pytest.raises(ValueError):
        seal_harness(spec(resources=[resource, resource]), tmp_path)
    sealed = seal_harness(spec(resources=[resource]), tmp_path)
    value = sealed.model_dump()
    value["resources"][0]["text"] = "mutated"
    with pytest.raises(ValueError, match="digest"):
        ResolvedHarness.model_validate(value)


def test_configfile_resource_alias_and_native_file_interpolation(tmp_path):
    (tmp_path / "rules.md").write_text("instruction bytes")
    path = tmp_path / "run.toml"
    path.write_text(
        '[harness]\nname="opencode"\nversion="1.18.30"\nmodel="openai/model"\n[[harness.resources]]\nsource="rules.md"\ndestination="prompts/rules.md"\n[harness.native_config]\ntext=\'{"agent":{"build":{"prompt":"{file:resource:prompts/rules.md}"}}}\'\n'
    )
    loaded = load_harness_override(path)
    (tmp_path / "rules.md").unlink()
    resolved = seal_harness(loaded, Path("/does-not-exist"))
    assert (
        parse_native(resolved.native_config)["agent"]["build"]["prompt"]
        == f"{{file:{AGENT_RESOURCE_ROOT}/prompts/rules.md}}"
    )


def test_sealed_directory_binds_skills_through_harbor(tmp_path):
    (tmp_path / "pack").mkdir()
    (tmp_path / "pack/SKILL.md").write_text(
        "---\nname: example\ndescription: Test skill\n---\nNative skill body\n"
    )
    sealed = seal_harness(
        spec(
            "claude-code",
            resources=[
                ResourceSource(
                    source="pack", destination="skills/example", directory=True
                )
            ],
        ),
        tmp_path,
    )
    config = compile_agent_config(sealed, resource_directory=tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()
    materialize_resources(sealed.resources, tmp_path / "runtime")
    assert config.skills == [str(tmp_path / "runtime/skills/example")]
    assert (
        (Path(config.skills[0]) / "SKILL.md")
        .read_text()
        .endswith("Native skill body\n")
    )


@pytest.mark.parametrize("name", ["codex", "claude-code"])
def test_native_seed_and_second_step_use_harbor_lifecycle(tmp_path, name):
    filename = (
        "rollout-2026-09-10T12-00-00-11111111-1111-4111-8111-111111111111.jsonl"
        if name == "codex"
        else "11111111-1111-4111-8111-111111111111.jsonl"
    )
    (tmp_path / filename).write_text(
        '{"type":"user","message":{"role":"user","content":"remember"}}\n'
    )
    sealed = seal_harness(
        spec(
            name,
            resources=[
                ResourceSource(source=filename, destination="sessions/" + filename)
            ],
            session=HarnessSession(
                resume_trajectory=True, load_trajectory="sessions/" + filename
            ),
        ),
        tmp_path,
    )
    config = compile_agent_config(sealed, resource_directory=tmp_path / "runtime")
    materialize_resources(sealed.resources, tmp_path / "runtime")
    assert config.resume_trajectory is True
    instance: Any = AgentFactory.create_agent_from_config(
        config, logs_dir=tmp_path / "logs", load_trajectory=config.load_trajectory
    )
    capture = CaptureEnvironment(name, STABLE_VERSIONS[name])
    asyncio.run(instance.load("continue seeded", capture, AgentContext()))
    assert any(filename in path and "/sessions/" in path for path in capture.uploads)
    assert not any("auth.json" in path for path in capture.uploads)
    asyncio.run(instance.resume("second step", capture, AgentContext()))
    assert any(
        ("resume --last" if name == "codex" else "--continue") in item["command"]
        for item in capture.commands
    )


def test_historical_resolved_harness_bytes_unchanged():
    raw = (
        b'{"ancillary_models":"primary","env":{},"model":"openai/model",'
        b'"name":"opencode","native_config":null,"options":{"title":"tetrabench"},'
        b'"version":"1.18.29"}'
    )
    parsed = ResolvedHarness.model_validate_json(raw)
    assert canonical_model_bytes(parsed) == raw
    assert sha256_hex(canonical_model_bytes(parsed)) == sha256_hex(raw)


def test_nested_config_files_are_sealed_relative_to_their_own_source(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/prompt.md").write_text("Nested native instructions")
    (tmp_path / "config/role.toml").write_text('model_instructions_file="prompt.md"\n')
    (tmp_path / "config/main.toml").write_text(
        '[agents.worker]\nconfig_file="role.toml"\n'
    )
    sealed = seal_harness(
        spec(
            "codex",
            ancillary_models="native",
            native_config=NativeConfig(format="toml", path="config/main.toml"),
        ),
        tmp_path,
    )
    assert len(sealed.resources) == 2
    role = next(
        item for item in sealed.resources if item.destination.endswith("role.toml")
    )
    assert AGENT_RESOURCE_ROOT in role.text
    assert str(tmp_path) not in canonical_model_bytes(sealed).decode()
    assert sealed.native_config is not None
    portable = spec(
        "codex",
        ancillary_models="native",
        native_config=NativeConfig(
            format=sealed.native_config.format, text=sealed.native_config.text
        ),
        resources=list(sealed.resources),
    )
    assert canonical_model_bytes(
        seal_harness(portable, tmp_path / "absent")
    ) == canonical_model_bytes(sealed)


def test_mcp_bundle_snapshots_command_file_and_only_keeps_credential_alias(tmp_path):
    (tmp_path / "server.py").write_text("print('server fixture')\n")
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test": {
                        "command": "python",
                        "args": [str(tmp_path / "server.py")],
                        "env": {"TOKEN": "${MCP_TOKEN}"},
                    }
                }
            }
        )
    )
    sealed = seal_harness(
        spec(
            "claude-code",
            env={"MCP_TOKEN": "${HOST_TOKEN}"},
            options={"mcp_config": "resource:mcp.json"},
            resources=[ResourceSource(source="mcp.json", destination="mcp.json")],
        ),
        tmp_path,
    )
    mcp = next(item for item in sealed.resources if item.destination == "mcp.json")
    assert json.loads(mcp.text)["mcpServers"]["test"]["args"][0].startswith(
        AGENT_RESOURCE_ROOT
    )
    assert "${MCP_TOKEN}" in mcp.text
    assert str(tmp_path) not in canonical_model_bytes(sealed).decode()


def test_jsonl_session_cannot_import_an_auth_store_under_a_different_filename(tmp_path):
    (tmp_path / "session.jsonl").write_text('{"refresh_token":"must-not-import"}\n')
    with pytest.raises(ValueError, match="credentials"):
        seal_harness(
            spec(
                "pi",
                resources=[
                    ResourceSource(source="session.jsonl", destination="session.jsonl")
                ],
            ),
            tmp_path,
        )


def test_pi_load_uses_native_v3_session_and_harbor_resume(tmp_path):
    filename = "native-session.jsonl"
    (tmp_path / filename).write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-09-10T12:00:00Z",
                "cwd": "/app",
            }
        )
        + "\n"
    )
    sealed = seal_harness(
        spec(
            "pi",
            resources=[ResourceSource(source=filename, destination=filename)],
            session=HarnessSession(load_trajectory=filename, resume_trajectory=True),
        ),
        tmp_path,
    )
    config = compile_agent_config(sealed, resource_directory=tmp_path / "runtime")
    materialize_resources(sealed.resources, tmp_path / "runtime")
    instance: Any = AgentFactory.create_agent_from_config(
        config, logs_dir=tmp_path / "logs", load_trajectory=config.load_trajectory
    )
    capture = CaptureEnvironment("pi", "0.85.1")
    asyncio.run(instance.load("first continuation", capture, AgentContext()))
    assert (
        capture.uploads[f"/logs/agent/pi/sessions/{filename}"]
        == (tmp_path / filename).read_bytes()
    )
    assert any("--continue" in item["command"] for item in capture.commands)
    assert not instance._load and not instance._resume


@pytest.mark.parametrize("name", STABLE_VERSIONS)
def test_resource_upload_is_separate_from_task_and_provenance_is_not_resolved(
    tmp_path, name
):
    (tmp_path / "rules.md").write_text("resource bytes")
    sealed = seal_harness(
        spec(
            name, resources=[ResourceSource(source="rules.md", destination="rules.md")]
        ),
        tmp_path,
    )
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(sealed), logs_dir=tmp_path / "logs"
    )
    capture = CaptureEnvironment(name, STABLE_VERSIONS[name])
    asyncio.run(instance.run("unchanged task instruction", capture, AgentContext()))
    assert capture.uploads[f"{AGENT_RESOURCE_ROOT}/rules.md"] == b"resource bytes"
    instance._write_provenance("unverified")
    provenance = json.loads((instance.logs_dir / "tetrabench-harness.json").read_text())
    assert "injected" in provenance["native_config_evidence"]
    assert "unobserved" in provenance["native_config_evidence"]
