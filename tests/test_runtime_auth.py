from __future__ import annotations

import asyncio
import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tetrabench.auth_config import AuthSpec, EnvAuthReference, NativeAuthReference
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    AuthStateError,
    LocalSessionStore,
    claim_session,
    private_directory,
    seed_session,
)
from tetrabench.harness_config import ResolvedHarness
from tetrabench.local_execution import local_paths
from tetrabench.runtime_auth import (
    DockerConsumerProof,
    ModalConsumerProof,
    RuntimeAuthScope,
    current_runtime_auth,
    make_credential_context,
    validate_runtime_auth_request,
)


def native(harness: str, marker: str = "SYNTHETIC_INITIAL") -> bytes:
    if harness == "codex":
        value = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": marker,
                "refresh_token": "SYNTHETIC_REFRESH",
                "id_token": "SYNTHETIC_ID",
            },
        }
    else:
        value = {
            "openai" if harness == "opencode" else "openai-codex": {
                "type": "oauth",
                "access": marker,
                "refresh": "SYNTHETIC_REFRESH",
                "expires": 4000000000000,
            }
        }
    return json.dumps(value).encode()


class FakeContainer:
    def __init__(self):
        self.running = True
        self.removed = False


class FakeProof:
    def __init__(self, container: FakeContainer):
        self.container = container

    async def stopped(self) -> bool:
        return not self.container.running or self.container.removed


class FakeHarborEnvironment:
    default_user = "root"

    def __init__(
        self, harness: str, *, outcome: str = "success", stop_works: bool = True
    ):
        self.harness, self.outcome, self.stop_works = harness, outcome, stop_works
        self.container = FakeContainer()
        self.files: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.delivered = False
        self.exec_env: dict[str, str] = {}
        self.fail_download = False
        self.mode = "chatgpt_oauth"

    async def upload_file(self, source: Path, target: str) -> None:
        self.files[target] = source.read_bytes()

    async def download_file(self, source: str, target: Path) -> None:
        if self.fail_download:
            raise OSError("SYNTHETIC_TRANSPORT_ERROR")
        target.write_bytes(self.files[source])

    async def stop(self, delete: bool) -> None:
        if self.stop_works:
            self.container.running = False
            self.container.removed = delete

    async def exec(self, command: str, env: dict[str, str] | None = None, **kwargs):
        env = env or {}
        self.commands.append(command)
        tokens = shlex.split(command)
        result = SimpleNamespace(return_code=0, stdout="", stderr="")
        if "auth-io.py" in command:
            action, root = tokens[2:4]
            if action == "snapshot":
                self.files[tokens[5]] = self.files[tokens[4]]
            elif action == "remove":
                self.files.pop(tokens[4])
            elif action in {"codex-regular", "codex-api"}:
                if root + "/secrets/auth.json" in self.files:
                    self.files[root + "/codex/auth.json"] = self.files[
                        root + "/secrets/auth.json"
                    ]
            elif action == "opencode-evidence":
                for suffix in ("opencode.db", "opencode.db-wal"):
                    source = root + "/data/opencode/" + suffix
                    if source in self.files:
                        self.files[
                            "/logs/agent/opencode/xdg-data/opencode/" + suffix
                        ] = self.files[source]
        elif "npm root -g" in command:
            result.stdout = "/SYNTHETIC/node_modules\n"
        elif "SYNTHETIC_NATIVE_MODEL" in command or "tee /logs/agent/" in command:
            self.delivered = True
            self.exec_env = dict(env)
            root = (
                env["XDG_DATA_HOME"].removesuffix("/data")
                if self.harness == "opencode"
                else ""
            )
            path = {"codex": "CODEX_HOME", "pi": "PI_CODING_AGENT_DIR"}.get(
                self.harness
            )
            auth_path = (
                (
                    env[path] + "/auth.json"
                    if path
                    else root + "/data/opencode/auth.json"
                )
                if self.harness != "claude-code"
                else None
            )
            if auth_path and auth_path in self.files and self.mode == "chatgpt_oauth":
                self.files[auth_path] = native(self.harness, "SYNTHETIC_ROTATED")
            if self.harness == "opencode":
                self.files[root + "/data/opencode/opencode.db"] = (
                    b"SYNTHETIC_SESSION_EVIDENCE"
                )
            if self.outcome == "cancel":
                raise asyncio.CancelledError
            result.return_code = 5 if self.outcome == "error" else 0
            selector = {
                "codex": "CODEX_HOME",
                "pi": "PI_CODING_AGENT_DIR",
                "opencode": "XDG_DATA_HOME",
                "claude-code": "CLAUDE_CONFIG_DIR",
            }[self.harness]
            producer_root = env[selector].rsplit("/", 1)[0]
            match = re.search(r"producer-status-([0-9a-f]{32})\.json", command)
            assert match is not None
            nonce = match[1]
            self.files[producer_root + f"/producer-status-{nonce}.json"] = json.dumps(
                {
                    "schema_version": 1,
                    "exit_code": result.return_code,
                    "nonce": nonce,
                }
            ).encode()
        elif "login status" in command:
            result.stderr = (
                "Logged in using an API key - SYNTHETI***I_KEY"
                if self.mode == "api_key"
                else "Logged in using ChatGPT"
            )
        elif "auth list" in command:
            result.stdout = (
                "Environment OpenRouter OPENROUTER_API_KEY"
                if self.mode == "api_key"
                else "OpenAI oauth"
            )
        elif "auth status" in command:
            method = "oauth_token" if self.mode == "claude_setup_token" else "api_key"
            result.stdout = json.dumps({"loggedIn": True, "authMethod": method})
        elif tokens and tokens[-1] == "status":
            result.stdout = json.dumps(
                {"type": "api_key" if self.mode == "api_key" else "oauth"}
            )
        elif "cat >" in command and '"OPENAI_API_KEY"' in command:
            root = env["CODEX_HOME"].removesuffix("/codex")
            self.files[root + "/secrets/auth.json"] = json.dumps(
                {"OPENAI_API_KEY": env["OPENAI_API_KEY"]}
            ).encode()
        return result


async def proof_factory(environment: FakeHarborEnvironment, engine: str) -> FakeProof:
    assert engine in {"docker", "modal"}
    await asyncio.sleep(0)
    return FakeProof(environment.container)


def new_scope(
    tmp_path: Path, harness: str = "codex", *, engine="docker", mode="chatgpt_oauth"
):
    store = LocalSessionStore(
        tmp_path / "authority", binding="test-runtime", local_filesystem=True
    )
    ref = NativeAuthReference(profile="profile", generation=1, binding="test-runtime")
    if mode == "chatgpt_oauth" and store.read("profile") is None:
        seed_session(store, ref, harness, native(harness))
    spec = AuthSpec(
        mode=mode,
        reference=ref if mode == "chatgpt_oauth" else EnvAuthReference(name="EVAL_KEY"),
    )
    resolved = ResolvedHarness(
        name=harness,
        version={
            "codex": "0.154.0",
            "pi": "0.85.1",
            "opencode": "1.18.30",
            "claude-code": "2.1.267",
        }[harness],
        model="openrouter/openai/gpt-5"
        if mode == "api_key" and harness in {"opencode", "pi"}
        else "openai-codex/gpt-5"
        if harness == "pi"
        else "anthropic/claude-sonnet-4-6"
        if harness == "claude-code"
        else "openai/gpt-5",
        auth=spec,
    )
    directory = private_directory(
        tmp_path / ("private-" + __import__("uuid").uuid4().hex), create=True
    )
    scope = RuntimeAuthScope(
        resolved,
        local_paths(tmp_path / "artifacts"),
        engine=engine,
        consumer_id="fc-SYNTHETIC-OWNER",
        directory=directory,
        environment={"EVAL_KEY": "SYNTHETIC_API_KEY"}
        if mode != "chatgpt_oauth"
        else {},
        store=store,
        proof_factory=proof_factory,
    )
    return scope, store, ref


async def native_trial(scope: RuntimeAuthScope, environment: FakeHarborEnvironment):
    hook = scope.new_hook(scope.harness)
    agent = SimpleNamespace(_extra_env={}, _base_config={})
    hook.configure_agent(agent)
    await hook.bind(environment)
    if scope.harness.name == "codex":
        assert hook.codex_source_path is not None
        await environment.upload_file(
            hook.codex_source_path, hook.root + "/secrets/auth.json"
        )
    try:
        dispatch = hook.new_dispatch()
        return await hook.execute_model(
            environment,
            "SYNTHETIC_NATIVE_MODEL " + dispatch.producer_status_path,
            dispatch=dispatch,
        )
    finally:
        # Faithful Codex 0.22 cleanup: native auth disappears BEFORE environment.stop.
        if scope.harness.name == "codex":
            environment.files.pop(hook.native_path, None)
        await environment.stop(delete=True)


@pytest.mark.parametrize("engine", ["docker", "modal"])
@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("outcome", ["success", "error"])
def test_native_writeback_precedes_cleanup_and_physical_stop(
    tmp_path, engine, harness, outcome
):
    scope, store, ref = new_scope(tmp_path, harness, engine=engine)
    environment = FakeHarborEnvironment(harness, outcome=outcome)
    result = asyncio.run(native_trial(scope, environment))
    assert result.return_code == (5 if outcome == "error" else 0)
    assert store.read("profile").state.phase == "claimed"
    if outcome == "error":
        with pytest.raises(AuthStateError):
            scope.finalize(require_consumer=True)
        with pytest.raises(AuthBusyError):
            claim_session(store, ref, harness)
    else:
        scope.finalize(require_consumer=True)
        claim = claim_session(store, ref, harness)
        assert b"SYNTHETIC_ROTATED" in claim.snapshot.state.native
    assert "SYNTHETIC_REFRESH" not in "\n".join(environment.commands)
    public = b"\n".join(
        data for path, data in environment.files.items() if path.startswith("/logs/")
    )
    assert b"SYNTHETIC_REFRESH" not in public
    assert b"SYNTHETIC_ROTATED" not in public
    if harness == "opencode":
        assert b"SYNTHETIC_SESSION_EVIDENCE" in public
        assert not environment.exec_env["XDG_DATA_HOME"].startswith("/logs")
    elif harness == "pi":
        assert not environment.exec_env["PI_CODING_AGENT_DIR"].startswith("/logs")


def test_two_simultaneous_runs_cannot_clone_subscription(tmp_path):
    scope, store, ref = new_scope(tmp_path)
    with pytest.raises(AuthBusyError):
        new_scope(tmp_path)
    scope.finalize()
    claim = claim_session(store, ref, "codex")
    assert claim.snapshot.state.native == native("codex")


def test_two_concurrent_agent_bindings_reserve_before_provider_await(tmp_path):
    scope, _, _ = new_scope(tmp_path, "opencode")
    environments = [FakeHarborEnvironment("opencode") for _ in range(2)]
    hooks = [scope.new_hook(scope.harness) for _ in environments]

    async def bind():
        return await asyncio.gather(
            *(hook.bind(env) for hook, env in zip(hooks, environments, strict=True)),
            return_exceptions=True,
        )

    results = asyncio.run(bind())
    assert sum(isinstance(result, AuthBusyError) for result in results) == 1
    assert (
        sum(
            any(path.endswith("/auth.json") for path in env.files)
            for env in environments
        )
        == 1
    )
    with pytest.raises(AuthStateError):
        scope.finalize()


def test_cancel_preserves_observed_refresh_without_releasing_lineage(tmp_path):
    scope, store, ref = new_scope(tmp_path)
    environment = FakeHarborEnvironment("codex", outcome="cancel")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(native_trial(scope, environment))
    with pytest.raises(AuthStateError, match="unproven"):
        scope.finalize()
    assert b"SYNTHETIC_ROTATED" in store.read("profile").state.native
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, "codex")


def test_native_return_without_physical_stop_is_insufficient(tmp_path):
    scope, store, ref = new_scope(tmp_path)
    environment = FakeHarborEnvironment("codex", stop_works=False)
    asyncio.run(native_trial(scope, environment))
    with pytest.raises(AuthStateError, match="physical stop"):
        scope.finalize()
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, "codex")


def test_lost_writeback_never_reuses_bootstrap_copy(tmp_path):
    scope, store, ref = new_scope(tmp_path)
    environment = FakeHarborEnvironment("codex")
    environment.fail_download = True
    with pytest.raises(AuthError, match="capture"):
        asyncio.run(native_trial(scope, environment))
    with pytest.raises(AuthStateError):
        scope.finalize()
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, "codex")


def test_two_trials_in_one_run_read_latest_native_generation(tmp_path):
    scope, store, _ = new_scope(tmp_path)
    first = FakeHarborEnvironment("codex")
    asyncio.run(native_trial(scope, first))
    second = FakeHarborEnvironment("codex")
    asyncio.run(native_trial(scope, second))
    assert b"SYNTHETIC_ROTATED" in next(
        data
        for name, data in second.files.items()
        if name.endswith("secrets/auth.json")
    )
    scope.finalize(require_consumer=True)
    assert store.read("profile").state.phase == "ready"


def test_api_key_consumers_stay_parallel_and_out_of_records(tmp_path):
    scope, _, _ = new_scope(tmp_path, "claude-code", mode="api_key")

    async def simultaneous():
        environments = [FakeHarborEnvironment("claude-code") for _ in range(2)]
        await asyncio.gather(*(native_trial(scope, env) for env in environments))
        return environments

    environments = asyncio.run(simultaneous())
    scope.finalize(require_consumer=True)
    assert scope.claim is None
    for env in environments:
        assert env.exec_env["ANTHROPIC_API_KEY"] == "SYNTHETIC_API_KEY"
        assert "SYNTHETIC_API_KEY" not in "\n".join(env.commands)
    assert "SYNTHETIC_API_KEY" not in scope.harness.model_dump_json()


def test_docker_stop_proof_rejects_other_daemon_or_running_container(monkeypatch):
    state = {"daemon": "SYNTHETIC_DAEMON", "process": "a" * 64 + " running"}

    def docker(*args):
        return state["daemon"] if args[0] == "info" else state["process"]

    monkeypatch.setattr("tetrabench.docker_lifecycle._docker", docker)
    proof = DockerConsumerProof("SYNTHETIC_DAEMON", "a" * 64)
    assert not asyncio.run(proof.stopped())
    state["process"] = "a" * 64 + " exited"
    assert asyncio.run(proof.stopped())
    state["daemon"] = "OTHER_DAEMON"
    assert not asyncio.run(proof.stopped())


def test_modal_stop_proof_polls_retained_native_handle():
    state = {"result": None}

    async def poll():
        return state["result"]

    proof = ModalConsumerProof(SimpleNamespace(poll=SimpleNamespace(aio=poll)))
    assert not asyncio.run(proof.stopped())
    state["result"] = 137
    assert asyncio.run(proof.stopped())


def test_subscription_concurrency_rejected_before_native_execution():
    request = SimpleNamespace(
        plan=SimpleNamespace(
            harness=SimpleNamespace(auth=SimpleNamespace(mode="chatgpt_oauth")),
            harbor=SimpleNamespace(concurrency=2),
        )
    )
    with pytest.raises(AuthError, match="concurrency=1"):
        validate_runtime_auth_request(request)


def test_context_without_agent_hook_fails_closed_and_restores_environment(
    tmp_path, monkeypatch
):
    scope, _, _ = new_scope(tmp_path, "claude-code", mode="api_key")
    monkeypatch.setenv("EVAL_KEY", "SYNTHETIC_API_KEY")
    context = make_credential_context(
        engine="docker",
        consumer_id="local-SYNTHETIC",
        environment={"EVAL_KEY": "SYNTHETIC_API_KEY"},
    )
    with pytest.raises(AuthError, match="hook did not run"):
        with context(scope.harness, local_paths(tmp_path / "artifacts")):
            import os

            assert "EVAL_KEY" not in os.environ
            hook = current_runtime_auth(scope.harness)
            assert hook is not None
    import os

    assert os.environ["EVAL_KEY"] == "SYNTHETIC_API_KEY"


@pytest.mark.parametrize(
    "harness,mode",
    [
        ("codex", "chatgpt_oauth"),
        ("codex", "api_key"),
        ("claude-code", "api_key"),
        ("claude-code", "claude_setup_token"),
        ("opencode", "chatgpt_oauth"),
        ("pi", "chatgpt_oauth"),
        ("opencode", "api_key"),
        ("pi", "api_key"),
    ],
)
def test_real_harbor_agent_factory_runs_through_runtime_hook(tmp_path, harness, mode):
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.context import AgentContext

    from tetrabench.harnesses import compile_agent_config
    from tetrabench.runtime_auth import _current

    scope, store, _ = new_scope(tmp_path, harness, mode=mode)
    environment = FakeHarborEnvironment(harness)
    environment.mode = mode
    config = compile_agent_config(scope.harness)
    before = config.model_dump_json()
    agent: Any = AgentFactory.create_agent_from_config(
        config, logs_dir=tmp_path / "artifacts/agent"
    )

    async def run():
        token = _current.set(scope)
        try:
            await agent.run("Write an answer", environment, AgentContext())
            await agent.run("Continue with a second step", environment, AgentContext())
        finally:
            await environment.stop(delete=True)
            _current.reset(token)

    asyncio.run(run())
    scope.finalize(require_consumer=True)
    assert environment.delivered
    observed = json.loads(
        (tmp_path / "artifacts/agent/tetrabench-auth.json").read_bytes()
    )
    assert observed["requested_mode"] == mode
    assert observed["observed"]["mode"] == mode
    assert observed["observed"]["source"] == "native_status"
    assert observed["account_verified"] is False
    assert observed["model_activity"] == "not_assessed"
    hook = agent._runtime_auth_hook
    assert len(hook._dispatches) == 2
    assert len({item.dispatch.nonce for item in hook._dispatches}) == 2
    if mode == "api_key":
        from tetrabench.auth_config import credential_env_name

        assert (
            environment.exec_env[
                credential_env_name(harness, mode, model=scope.harness.model)
            ]
            == "SYNTHETIC_API_KEY"
        )
        assert scope.claim is None
    assert config.model_dump_json() == before
    for path in (tmp_path / "artifacts").rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC_API_KEY" not in path.read_bytes()
            assert b"SYNTHETIC_REFRESH" not in path.read_bytes()
    if mode == "chatgpt_oauth":
        assert b"SYNTHETIC_ROTATED" in store.read("profile").state.native
