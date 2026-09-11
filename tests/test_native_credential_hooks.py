"""Private runtime capabilities stay outside native job/provenance records."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.auth_config import AuthSpec, EnvAuthReference, NativeAuthReference
from tetrabench.harness_agents import native_credential_hooks
from tetrabench.harness_config import HarnessConfig
from tetrabench.harnesses import STABLE_VERSIONS, compile_agent_config, seal_harness


def make_agent(tmp_path, name):
    auth = (
        AuthSpec(
            mode="claude_setup_token", reference=EnvAuthReference(name="SELECTED_TOKEN")
        )
        if name == "claude-code"
        else AuthSpec(
            mode="chatgpt_oauth",
            reference=NativeAuthReference(
                profile="test", binding="local", generation=1
            ),
        )
    )
    harness = HarnessConfig(
        name=name,
        version=STABLE_VERSIONS[name],
        model="openai-codex/model" if name == "pi" else "openai/model",
        auth=auth,
    )
    config = compile_agent_config(seal_harness(harness, tmp_path))
    return AgentFactory.create_agent_from_config(config, logs_dir=tmp_path), config


class Capsule:
    def __init__(self, token, events, fail_capture=False):
        self.token = token
        self.events = events
        self.fail_capture = fail_capture

    async def prepare(self, agent, environment):
        self.events.append("prepare")
        await asyncio.sleep(0)

    def exec_environment(self, agent, env):
        return dict(env, SELECTED_TOKEN=self.token)

    async def capture(self, agent, environment):
        self.events.append("capture")
        if self.fail_capture:
            raise ValueError("capture unavailable")


@pytest.mark.parametrize("name", STABLE_VERSIONS)
def test_capsule_prepares_before_native_execution_and_never_serializes(tmp_path, name):
    instance: Any
    instance, config = make_agent(tmp_path, name)
    before = config.model_dump_json()
    events = []
    capture: Any = CaptureEnvironment(name, STABLE_VERSIONS[name])
    original = capture.exec

    async def execute(**kwargs):
        events.append(kwargs["command"])
        return await original(**kwargs)

    capture.exec = execute
    with native_credential_hooks(lambda _: Capsule("runtime-only-credential", events)):
        asyncio.run(instance.run("no inference", capture, AgentContext()))
    assert events[0] == "prepare"
    assert events.count("capture") == 1
    if name == "codex":
        cleanup = next(
            i
            for i, event in enumerate(events)
            if 'rm -rf /tmp/codex-secrets "$CODEX_HOME"' in event
        )
        assert events.index("capture") < cleanup
    assert config.model_dump_json() == before
    instance._write_provenance("unverified")
    assert (
        "runtime-only-credential"
        not in (tmp_path / "tetrabench-harness.json").read_text()
    )
    assert "runtime-only-credential" not in before
    assert any(
        row.get("env", {}).get("SELECTED_TOKEN") == "runtime-only-credential"
        for row in capture.commands
    )


def test_explicit_auth_cannot_execute_without_runtime_capsule(tmp_path):
    from tetrabench.auth_sessions import AuthError

    instance: Any
    instance, _ = make_agent(tmp_path, "codex")
    capture = CaptureEnvironment("codex", "0.154.0")
    with pytest.raises(AuthError, match="runtime credential context"):
        asyncio.run(instance.run("no inference", capture, AgentContext()))
    assert capture.commands == []


def test_runtime_auth_hook_cannot_disappear_during_configuration(tmp_path, monkeypatch):
    instance: Any
    instance, _ = make_agent(tmp_path, "codex")
    hook = SimpleNamespace(
        configure_agent=lambda agent: setattr(agent, "_runtime_auth_hook", None)
    )
    monkeypatch.setattr("tetrabench.runtime_auth.current_runtime_auth", lambda _: hook)
    capture = CaptureEnvironment("codex", "0.154.0")
    with pytest.raises(RuntimeError, match="runtime auth hook lost"):
        asyncio.run(instance._prepare_credentials(capture))
    assert capture.commands == []


def test_capture_failure_blocks_codex_destructive_cleanup(tmp_path):
    instance: Any
    instance, _ = make_agent(tmp_path, "codex")
    capture = CaptureEnvironment("codex", "0.154.0")
    with native_credential_hooks(lambda _: Capsule("runtime-token", [], True)):
        with pytest.raises(ValueError, match="authority remains blocked"):
            asyncio.run(instance.run("no inference", capture, AgentContext()))
    assert not any(
        'rm -rf /tmp/codex-secrets "$CODEX_HOME"' in row["command"]
        for row in capture.commands
    )


def test_parallel_trial_contexts_do_not_share_credentials(tmp_path):
    async def trial(name):
        root = tmp_path / name
        root.mkdir()
        instance: Any
        instance, _ = make_agent(root, "opencode")
        capture = CaptureEnvironment("opencode", "1.18.30")
        with native_credential_hooks(lambda _: Capsule(name, [])):
            await instance.run("no inference", capture, AgentContext())
        return {row.get("env", {}).get("SELECTED_TOKEN") for row in capture.commands}

    async def run():
        return await asyncio.gather(trial("one"), trial("two"))

    assert asyncio.run(run()) == [{"one"}, {"two"}]


@pytest.mark.parametrize("name", ["codex", "opencode", "pi", "claude-code"])
def test_real_adapter_delegates_native_exec_to_auth_owner_before_cleanup(
    tmp_path, monkeypatch, name
):
    from test_runtime_auth import FakeHarborEnvironment, native, new_scope

    scope, store, ref = new_scope(
        tmp_path, name, mode="api_key" if name == "claude-code" else "chatgpt_oauth"
    )
    monkeypatch.setattr(
        "tetrabench.runtime_auth.current_runtime_auth",
        lambda harness: scope.new_hook(harness),
    )
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(scope.harness), logs_dir=tmp_path / "logs"
    )
    environment = FakeHarborEnvironment(name)

    async def run():
        await instance.run("SYNTHETIC_NATIVE_MODEL", environment, AgentContext())
        assert scope.hooks[0].captured
        assert scope.hooks[0].execution_returned
        await environment.stop(delete=True)

    asyncio.run(run())
    scope.finalize(require_consumer=True)
    if name != "claude-code":
        state = store.read(ref.profile)
        assert state is not None and state.state.native == native(
            name, "SYNTHETIC_ROTATED"
        )
        assert state.state.phase == "ready"
    assert not any(
        "auth.json" in path and path.startswith("/logs/") for path in environment.files
    )
    instance._write_provenance("unverified")
    assert (
        "SYNTHETIC_API_KEY"
        not in (instance.logs_dir / "tetrabench-harness.json").read_text()
    )


@pytest.mark.parametrize("name", ["codex", "opencode", "pi", "claude-code"])
def test_same_trial_resume_reuses_latest_auth_but_never_the_producer_witness(
    tmp_path, monkeypatch, name
):
    from test_runtime_auth import FakeHarborEnvironment, native, new_scope

    scope, store, ref = new_scope(
        tmp_path, name, mode="api_key" if name == "claude-code" else "chatgpt_oauth"
    )
    monkeypatch.setattr(
        "tetrabench.runtime_auth.current_runtime_auth",
        lambda harness: scope.new_hook(harness),
    )
    observed = []

    class Environment(FakeHarborEnvironment):
        async def exec(self, command, env=None, **kwargs):
            path = None
            if (
                "SYNTHETIC_NATIVE_MODEL" in command
                and env is not None
                and name != "claude-code"
            ):
                directory = env[
                    "CODEX_HOME"
                    if name == "codex"
                    else "XDG_DATA_HOME"
                    if name == "opencode"
                    else "PI_CODING_AGENT_DIR"
                ]
                path = directory + (
                    "/opencode/auth.json" if name == "opencode" else "/auth.json"
                )
                observed.append(self.files[path])
            result = await super().exec(command, env=env, **kwargs)
            if path is not None:
                self.files[path] = native(name, f"SYNTHETIC_ROTATED_{len(observed)}")
            return result

    environment = Environment(name)
    config = compile_agent_config(scope.harness)
    original = config.model_dump_json()
    instance: Any = AgentFactory.create_agent_from_config(
        config, logs_dir=tmp_path / "logs"
    )

    async def run():
        await instance.run("SYNTHETIC_NATIVE_MODEL", environment, AgentContext())
        first = (instance._native_producer_status_path, instance._native_producer_nonce)
        await instance.resume("SYNTHETIC_NATIVE_MODEL", environment, AgentContext())
        second = (
            instance._native_producer_status_path,
            instance._native_producer_nonce,
        )
        assert first[0] != second[0] and first[1] != second[1]
        await environment.stop(delete=True)

    asyncio.run(run())
    scope.finalize(require_consumer=True)
    assert config.model_dump_json() == original
    assert len(scope.hooks) == 1
    if name != "claude-code":
        assert observed == [native(name), native(name, "SYNTHETIC_ROTATED_1")]
        state = store.read(ref.profile)
        assert state is not None and state.state.native == native(
            name, "SYNTHETIC_ROTATED_2"
        )
