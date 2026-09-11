from __future__ import annotations

import asyncio
import os
import shlex
from types import SimpleNamespace

import pytest
from harbor.environments.dind_compose import DinDComposeOps
from harbor.environments.docker import RESOURCES_COMPOSE_NAME
from harbor.environments.modal import ModalEnvironment, _ModalDinD, _ModalDirect
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from tetrabench.harbor import TetrabenchModalEnvironment
from tetrabench.modal_environment import EnvNameModalDinD


def environment(tmp_path, *, compose=True, extra=False, **kwargs):
    definition = tmp_path / "environment"
    definition.mkdir(exist_ok=True)
    (definition / "Dockerfile").write_text("FROM debian:bookworm-slim\n")
    extra_paths = []
    if compose:
        path = definition / ("extra.yaml" if extra else "docker-compose.yaml")
        path.write_text("services:\n  main:\n    image: debian:bookworm-slim\n")
        if extra:
            extra_paths.append(path)
    return TetrabenchModalEnvironment(
        environment_dir=definition,
        environment_name="task",
        session_id="Harbor.Session-1",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(workdir="/workspace"),
        run_id="run-1",
        attempt_id="attempt-1",
        plan_sha256="f" * 64,
        event_sink_key="unused",
        observation_path=str(tmp_path / "children.jsonl"),
        extra_docker_compose=extra_paths,
        **kwargs,
    )


class RecordingSandbox:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.exec = SimpleNamespace(aio=self.execute)

    async def execute(self, *command_args, **kwargs):
        # This boundary is downstream of native _sdk_exec and _compose_cmd.
        await asyncio.sleep(0)
        self.calls.append((command_args, kwargs))
        if self.failure is not None:
            raise self.failure

        async def stdout():
            return "native stdout"

        async def stderr():
            return "native stderr"

        async def wait():
            return 7

        return SimpleNamespace(
            stdout=SimpleNamespace(read=SimpleNamespace(aio=stdout)),
            stderr=SimpleNamespace(read=SimpleNamespace(aio=stderr)),
            wait=SimpleNamespace(aio=wait),
        )


@pytest.fixture
def sandbox(monkeypatch):
    # Replace only the SDK payload materialization, never Harbor command building.
    monkeypatch.setattr("harbor.environments.modal._cached_env_secret", dict)
    return RecordingSandbox()


def client_env(call):
    return {k: v for secret in call[1]["secrets"] for k, v in secret.items()}


def exec_parts(call):
    parts = shlex.split(call[0][2])
    return parts[parts.index("exec") :]


def forwarded(call):
    parts = exec_parts(call)
    names = []
    index = 1
    while parts[index] in {"-e", "-w", "-u", "-T"}:
        if parts[index] == "-T":
            index += 1
            continue
        if parts[index] == "-e":
            names.append(parts[index + 1])
        index += 2
    return names


@pytest.mark.parametrize("extra", [False, True])
@pytest.mark.parametrize("vm", [False, True])
def test_native_dind_command_and_sdk_environment(tmp_path, sandbox, extra, vm):
    env = environment(tmp_path, extra=extra, modal_vm_runtime=vm)
    env._sandbox = sandbox
    values = {
        "ANTHROPIC_API_KEY": "SYNTHETIC_CLAUDE 'quotes' $dollar\nline",
        "OPENAI_API_KEY": "SYNTHETIC_CODEX=a=b",
        "OPENROUTER_API_KEY": "SYNTHETIC_OPENCODE_PI",
        "CUSTOM_TOKEN": 'SYNTHETIC_TOKEN; $(false) "quoted"',
        "EMPTY": "",
    }
    result = asyncio.run(
        env.exec("printf '%s' ok", env=values, user=123, timeout_sec=19)
    )
    assert (result.stdout, result.stderr, result.return_code) == (
        "native stdout",
        "native stderr",
        7,
    )
    call = sandbox.calls[0]
    assert call[0][:2] == ("sh", "-c")
    assert call[1]["timeout"] == 19
    assert call[1]["workdir"] is None  # -w belongs to the inner service.
    assert forwarded(call) == list(values)
    assert exec_parts(call)[-8:] == [
        "-w",
        "/workspace",
        "-u",
        "123",
        "main",
        "bash",
        "-lc",
        "printf '%s' ok",
    ]
    assert all(client_env(call)[key] == value for key, value in values.items())
    assert "SYNTHETIC_" not in str(call[0])
    parts = shlex.split(call[0][2])
    assert parts[:6] == [
        "docker",
        "compose",
        "-p",
        "harbor-session-1",
        "--project-directory",
        "/harbor/environment",
    ]
    assert ("/harbor/compose/docker-compose-host-network.yaml" in parts) is not vm
    assert "/harbor/compose/docker-compose-mounts.json" in parts
    assert f"/harbor/compose/{RESOURCES_COMPOSE_NAME}" in parts


def test_installed_harbor_renderer_reproduces_gap(tmp_path, sandbox):
    env = environment(tmp_path)
    env._sandbox = sandbox
    asyncio.run(_ModalDinD(env).exec("true", env={"TOKEN": "SYNTHETIC_UNSAFE"}))
    assert "TOKEN=SYNTHETIC_UNSAFE" in sandbox.calls[0][0][2]
    assert "TOKEN" not in client_env(sandbox.calls[0])


def test_native_lifecycle_and_operations_are_inherited(tmp_path):
    env = environment(tmp_path)
    assert isinstance(env._strategy, EnvNameModalDinD)
    assert env._compose_service_transport("sidecar") is env._strategy
    for name in (
        "start",
        "stop",
        "attach",
        "upload_file",
        "upload_dir",
        "download_file",
        "download_dir",
        "stop_service",
        "_compose_cmd",
        "_compose_env_vars",
        "_compose_file_flags",
        "_resolve_volumes",
        "_stage_env_compose_file",
    ):
        assert getattr(EnvNameModalDinD, name) is getattr(_ModalDinD, name)
    assert _ModalDinD.exec is DinDComposeOps.exec  # No global native monkeypatch.
    assert TetrabenchModalEnvironment.exec is ModalEnvironment.exec


def test_direct_modal_keeps_native_sdk_transport(tmp_path, sandbox):
    env = environment(tmp_path, compose=False)
    env._sandbox = sandbox
    assert type(env._strategy) is _ModalDirect
    asyncio.run(env.exec("true", env={"TOKEN": "SYNTHETIC_DIRECT"}, timeout_sec=9))
    call = sandbox.calls[0]
    assert call[0] == ("bash", "-c", "true")
    assert call[1]["workdir"] == "/workspace"
    assert call[1]["timeout"] == 9
    assert client_env(call) == {"TOKEN": "SYNTHETIC_DIRECT"}


def test_empty_env_command_matches_native_dind_exactly(tmp_path, sandbox):
    env = environment(tmp_path)
    env._sandbox = sandbox

    async def run():
        await _ModalDinD(env).exec("true", cwd="/work", user="agent", timeout_sec=5)
        await env._strategy.exec("true", cwd="/work", user="agent", timeout_sec=5)

    asyncio.run(run())
    assert sandbox.calls[0] == sandbox.calls[1]


def test_sidecar_env_wins_over_main_scope_only_for_that_call(tmp_path, sandbox):
    env = environment(tmp_path, persistent_env={"TOKEN": "SYNTHETIC_PERSISTENT"})
    env._sandbox = sandbox

    async def run():
        with env.scoped_exec_env({"TOKEN": "SYNTHETIC_MAIN"}):
            await env.service_exec(
                "sidecar", service="helper", env={"TOKEN": "SYNTHETIC_SIDECAR"}
            )
            await env.exec("main")
        await env.exec("after")

    asyncio.run(run())
    assert [client_env(call)["TOKEN"] for call in sandbox.calls] == [
        "SYNTHETIC_SIDECAR",
        "SYNTHETIC_MAIN",
        "SYNTHETIC_PERSISTENT",
    ]
    assert all("SYNTHETIC_" not in str(call[0]) for call in sandbox.calls)


def test_scoped_persistent_sidecar_and_concurrent_calls(tmp_path, sandbox):
    env = environment(tmp_path, persistent_env={"PERSIST": "base", "ORDER": "base"})
    env._sandbox = sandbox
    before = dict(os.environ)

    async def trial(value):
        with env.scoped_exec_env({"ORDER": value, "EMPTY": ""}):
            await env.exec(value, env={"ORDER": "per-call"})
            with env.scoped_exec_env({"ORDER": "nested"}):
                await env.exec("nested-" + value)
            await env.service_exec(
                "sidecar-" + value, service="helper", env={"SIDE": value}
            )

    async def run():
        await asyncio.gather(trial("one"), trial("two"))
        await env.exec("after")
        await env._strategy._compose_exec(["ps", "--quiet", "main"])

    asyncio.run(run())
    assert dict(os.environ) == before
    for call in sandbox.calls[:-1]:
        command, client = exec_parts(call)[-1], client_env(call)
        if command in {"one", "two"}:
            assert client["ORDER"] == command
            assert client["EMPTY"] == ""
            assert set(forwarded(call)) == {"PERSIST", "ORDER", "EMPTY"}
        elif command.startswith("nested-"):
            assert client["ORDER"] == "nested"
        elif command.startswith("sidecar-"):
            assert forwarded(call) == ["SIDE"]
            assert exec_parts(call)[-4:-1] == ["helper", "sh", "-c"]
            assert "-w" not in exec_parts(call)
            assert "-u" not in exec_parts(call)
        else:
            assert command == "after"
            assert client["ORDER"] == "base"
            assert "EMPTY" not in client
            assert "SIDE" not in client
    assert "SIDE" not in client_env(sandbox.calls[-1])
    assert "EMPTY" not in client_env(sandbox.calls[-1])


@pytest.mark.parametrize(
    "failure", [OSError("synthetic error"), asyncio.CancelledError()]
)
def test_failure_does_not_forward_credentials_later(tmp_path, sandbox, failure):
    env = environment(tmp_path)
    env._sandbox = sandbox
    sandbox.failure = failure

    async def run():
        with pytest.raises(type(failure)):
            await env.exec("true", env={"TOKEN": "SYNTHETIC_FAILURE"})
        sandbox.failure = None
        await env.exec("after")

    asyncio.run(run())
    assert all("SYNTHETIC_FAILURE" not in str(call[0]) for call in sandbox.calls)
    assert "TOKEN" not in client_env(sandbox.calls[-1])
    assert "TOKEN" not in env._strategy._compose_env_vars()


@pytest.mark.parametrize("name", ["", "A=B", "A\x00B", "-e", "A B", "A\nB"])
def test_invalid_names_fail_before_sdk_without_echoing_payload(tmp_path, sandbox, name):
    env = environment(tmp_path)
    env._sandbox = sandbox
    with pytest.raises(ValueError, match="invalid Modal Compose exec environment name"):
        asyncio.run(env.exec("true", env={name: "SYNTHETIC_INVALID"}))
    assert not sandbox.calls


def test_conflicting_infra_value_fails_before_sdk(tmp_path, sandbox):
    env = environment(tmp_path)
    env._sandbox = sandbox
    name = next(iter(env._strategy._infra_env_vars()))
    with pytest.raises(ValueError, match="conflicts with Harbor infra"):
        asyncio.run(env.exec("true", env={name: "SYNTHETIC_COLLISION"}))
    assert not sandbox.calls


def test_sidecar_outer_infra_override_fails_before_sdk_and_restores_scope(
    tmp_path, sandbox
):
    env = environment(tmp_path)
    env._sandbox = sandbox
    outer = {"CONTEXT_DIR": "/synthetic-wrong-context"}
    before = dict(os.environ)

    async def run():
        with env.scoped_exec_env(outer):
            with pytest.raises(ValueError, match="conflicts with Harbor infra"):
                await env.service_exec(
                    "true", service="helper", env={"TOKEN": "SYNTHETIC_ONLY"}
                )
            assert not sandbox.calls
            assert env._merge_env(None) == outer
        assert env._merge_env(None) is None
        await env.service_exec("after", service="helper")

    asyncio.run(run())
    assert dict(os.environ) == before
    assert len(sandbox.calls) == 1
    client = client_env(sandbox.calls[0])
    assert client["CONTEXT_DIR"] == env._strategy._infra_env_vars()["CONTEXT_DIR"]
    assert "TOKEN" not in client
    assert "SYNTHETIC_ONLY" not in str(sandbox.calls[0][0])


def test_version_guard_before_native_construction(tmp_path, monkeypatch):
    monkeypatch.setattr("tetrabench.harbor.version", lambda _: "0.23.0")
    with pytest.raises(RuntimeError, match=r"Harbor 0\.22\.0 is required"):
        environment(tmp_path)
