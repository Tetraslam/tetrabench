"""Exercise the pinned Harbor hooks down to the subprocess and Docker boundary."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.environments.docker.docker import DockerEnvironment
from harbor.environments.factory import EnvironmentFactory
from harbor.models.agent.context import AgentContext
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import JobConfig, RetryConfig
from harbor.models.job.lock import JobLock
from harbor.models.job.result import JobResult, JobStats
from harbor.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from harbor.models.trial.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor.utils.env import resolve_env_vars

from tetrabench.auth_config import AuthSpec, EnvAuthReference
from tetrabench.controller_runtime import credential_free_harbor_environment
from tetrabench.docker_environment import (
    DOCKER_ENVIRONMENT_IMPORT_PATH,
    TetrabenchDockerEnvironment,
)
from tetrabench.harbor_api import Harbor022Api
from tetrabench.harness_agents import native_credential_hooks
from tetrabench.harness_config import HarnessConfig, NativeConfig
from tetrabench.harnesses import STABLE_VERSIONS, compile_agent_config, seal_harness


def environment(tmp_path, *, native=False, **kwargs) -> Any:
    root = tmp_path / uuid.uuid4().hex
    root.mkdir(parents=True)
    config = (
        EnvironmentConfig(type=EnvironmentType.DOCKER)
        if native
        else Harbor022Api.docker_environment()
    )
    return EnvironmentFactory.create_environment_from_config(
        config,
        environment_dir=root,
        environment_name="transport-fixture",
        session_id="transport-" + uuid.uuid4().hex,
        trial_paths=TrialPaths(trial_dir=root / "trial"),
        task_env_config=TaskEnvironmentConfig(docker_image="debian:bookworm-slim"),
        **kwargs,
    )


class Process:
    def __init__(self, stdout=b"", return_code=0):
        self.returncode = return_code
        self.output = stdout
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()

    async def communicate(self, input=None):
        return self.output, b""

    async def wait(self):
        return self.returncode


def capture_subprocess(monkeypatch, *, output=b"", return_code=0):
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        await asyncio.sleep(0)
        return Process(output, return_code)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return calls


def forwarded(argv):
    start = argv.index("exec")
    names = []
    index = start + 1
    while argv[index] in {"-e", "-w", "-u"}:
        if argv[index] == "-e":
            names.append(argv[index + 1])
        index += 2
    return names


def test_factory_pin_native_methods_and_compose_arguments(tmp_path, monkeypatch):
    env = environment(tmp_path)
    assert type(env) is TetrabenchDockerEnvironment
    for name in (
        "exec",
        "service_exec",
        "_compose_exec",
        "start",
        "stop",
        "upload_file",
        "_docker_compose_paths",
        "_collect_buffered_output",
        "_collect_streamed_output",
        "_terminate_process",
        "_merge_env",
        "scoped_exec_env",
    ):
        assert inspect.getattr_static(type(env), name) is inspect.getattr_static(
            DockerEnvironment, name
        )
    for name in ("_compose_env_vars", "_run_docker_compose_command"):
        assert tuple(inspect.signature(getattr(type(env), name)).parameters) == tuple(
            inspect.signature(getattr(DockerEnvironment, name)).parameters
        )
    calls = capture_subprocess(monkeypatch)
    asyncio.run(
        env.exec(
            "printf '%s' \"$MODEL_KEY\"",
            cwd="/workspace",
            user=0,
            env={"MODEL_KEY": "SYNTHETIC=value with $quotes'\n"},
        )
    )
    argv, kwargs = calls[0]
    assert argv[:6] == (
        "docker",
        "compose",
        "--project-name",
        env.session_id,
        "--project-directory",
        str(env.environment_dir.resolve()),
    )
    assert argv[6:8] == ("-f", str(env._DOCKER_COMPOSE_BUILD_PATH.resolve()))
    assert argv[8:] == (
        "exec",
        "-w",
        "/workspace",
        "-e",
        "MODEL_KEY",
        "-u",
        "0",
        "main",
        *env._platform.exec_shell_args("printf '%s' \"$MODEL_KEY\""),
    )
    assert kwargs["env"]["MODEL_KEY"] == "SYNTHETIC=value with $quotes'\n"
    assert "SYNTHETIC" not in repr(argv)
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT


def test_version_mismatch_refuses_before_native_construction(tmp_path, monkeypatch):
    monkeypatch.setattr("tetrabench.docker_environment.version", lambda _: "0.23.0")
    with pytest.raises(RuntimeError, match=r"0\.22\.0"):
        environment(tmp_path)


def test_original_backend_exposes_value_and_adapter_only_changes_transport(
    tmp_path, monkeypatch
):
    env = environment(tmp_path)
    calls = capture_subprocess(monkeypatch)
    # This reproduces the actual upstream bug rather than assuming a fake argv.
    command = ["exec", "-e", "MODEL_KEY=SYNTHETIC_ORIGINAL", "main", "true"]
    asyncio.run(DockerEnvironment._run_docker_compose_command(env, command))
    asyncio.run(env._run_docker_compose_command(command))
    original, fixed = calls
    assert "MODEL_KEY=SYNTHETIC_ORIGINAL" in original[0]
    assert "MODEL_KEY=SYNTHETIC_ORIGINAL" in command  # Caller inputs are unchanged.
    assert fixed[0] == tuple(
        "MODEL_KEY" if arg == "MODEL_KEY=SYNTHETIC_ORIGINAL" else arg
        for arg in original[0]
    )
    assert fixed[1] == original[1] | {
        "env": original[1]["env"] | {"MODEL_KEY": "SYNTHETIC_ORIGINAL"}
    }
    assert "MODEL_KEY" not in env._compose_env_vars()


@pytest.mark.parametrize(
    "command",
    [
        ["exec", "main", "printf", "-e", "opaque=not-an-environment-option"],
        ["exec", "--no-TTY", "helper", "network-policy", "deny-all"],
        ["cp", "/source", "main:/target"],
        ["down", "--rmi", "local", "--volumes", "--remove-orphans"],
    ],
)
def test_commands_without_env_are_identical_to_native(tmp_path, monkeypatch, command):
    env = environment(tmp_path)
    calls = capture_subprocess(monkeypatch)
    asyncio.run(DockerEnvironment._run_docker_compose_command(env, command))
    asyncio.run(env._run_docker_compose_command(command))
    assert calls[0] == calls[1]


def test_persistent_scoped_empty_sidecar_and_concurrent_env(tmp_path, monkeypatch):
    env = environment(tmp_path, persistent_env={"PERSIST": "base", "ORDER": "base"})
    other = environment(tmp_path)
    calls = capture_subprocess(monkeypatch)
    before = dict(os.environ)

    async def trial(value):
        with env.scoped_exec_env({"ORDER": value, "EMPTY": ""}):
            await env.exec(value, env={"ORDER": "per-call"})
            with env.scoped_exec_env({"ORDER": "nested"}):
                await env.exec("nested-" + value)
            await env.service_exec(
                "sidecar-" + value, service="helper", env={"SIDE": value}
            )
            await other.exec("other-" + value)

    async def run():
        await asyncio.gather(trial("one"), trial("two"))
        await env.exec("after")
        await env._run_docker_compose_command(["ps", "--quiet", "main"])

    asyncio.run(run())
    assert dict(os.environ) == before
    for argv, kwargs in calls:
        command, client = argv[-1], kwargs["env"]
        if command in {"one", "two"}:
            assert client["ORDER"] == command
            assert client["PERSIST"] == "base"
            assert client["EMPTY"] == ""
            assert set(forwarded(argv)) == {"ORDER", "PERSIST", "EMPTY"}
        elif command.startswith("nested-"):
            assert client["ORDER"] == "nested"
        elif command.startswith("sidecar-"):
            assert forwarded(argv) == ["SIDE"]
            assert argv[-3:-1] == ("sh", "-c")
        else:
            assert "EMPTY" not in client
            assert "SIDE" not in client
            if command == "after":
                assert client["ORDER"] == "base"


def test_nested_client_scope_and_s3_boundary(tmp_path, monkeypatch):
    env = environment(tmp_path)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SYNTHETIC_STORAGE")
    monkeypatch.setenv("DOCKER_HOST", "tcp://synthetic.invalid:2376")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/synthetic/certificates")
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "outer":
            await env.exec("inner", env={"MODEL_KEY": "inner-value"})
            assert env._compose_env_vars()["MODEL_KEY"] == "outer-value"
        assert "AWS_SECRET_ACCESS_KEY" not in kwargs["env"]
        assert kwargs["env"]["DOCKER_HOST"] == "tcp://synthetic.invalid:2376"
        assert kwargs["env"]["DOCKER_TLS_VERIFY"] == "1"
        assert kwargs["env"]["DOCKER_CERT_PATH"] == "/synthetic/certificates"
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with credential_free_harbor_environment():
        asyncio.run(env.exec("outer", env={"MODEL_KEY": "outer-value"}))
        assert "MODEL_KEY" not in env._compose_env_vars()
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "SYNTHETIC_STORAGE"
    assert [kwargs["env"]["MODEL_KEY"] for _, kwargs in calls] == [
        "outer-value",
        "inner-value",
    ]
    assert all(forwarded(argv) == ["MODEL_KEY"] for argv, _ in calls)


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_failed_spawn_resets_transport_context(tmp_path, monkeypatch, failure):
    env = environment(tmp_path)

    async def spawn(*args, **kwargs):
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise OSError("synthetic failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def run():
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else OSError):
            await env.exec("true", env={"MODEL_KEY": "synthetic-private"})
        assert "MODEL_KEY" not in env._compose_env_vars()

    asyncio.run(run())


def test_error_and_stream_logs_have_no_transport_values(tmp_path, monkeypatch, caplog):
    env = environment(tmp_path)
    calls = capture_subprocess(
        monkeypatch, output=b"synthetic failure\n", return_code=7
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as failure:
        asyncio.run(
            env._run_docker_compose_command(
                ["exec", "-e", "MODEL_KEY=synthetic-private", "main", "false"]
            )
        )
    logging.getLogger("transport-test").error("%s", failure.value)
    assert "synthetic-private" not in str(failure.value) + caplog.text
    assert "MODEL_KEY" in str(failure.value)
    assert calls[0][1]["env"]["MODEL_KEY"] == "synthetic-private"


@pytest.mark.parametrize("name", STABLE_VERSIONS)
@pytest.mark.parametrize("explicit_auth", [False, True])
def test_real_harness_to_actual_subprocess_boundary(
    tmp_path, monkeypatch, caplog, name, explicit_auth
):
    target = "ANTHROPIC_API_KEY" if name == "claude-code" else "OPENAI_API_KEY"
    secret = "SYNTHETIC_TRANSPORT_TOKEN_" + name
    monkeypatch.setenv("SELECTED_TOKEN", secret)
    spec = HarnessConfig(
        name=name,
        version=STABLE_VERSIONS[name],
        model=(
            "anthropic/claude-sonnet-4-6" if name == "claude-code" else "openai/gpt-5"
        ),
        auth=AuthSpec(mode="api_key", reference=EnvAuthReference(name="SELECTED_TOKEN"))
        if explicit_auth
        else None,
        env={} if explicit_auth else {target: "${SELECTED_TOKEN}"},
    )
    agent_config = compile_agent_config(seal_harness(spec, tmp_path))
    agent: Any = AgentFactory.create_agent_from_config(
        agent_config, logs_dir=tmp_path / "logs"
    )
    env = environment(tmp_path)
    calls = capture_subprocess(monkeypatch)

    class Capsule:
        async def prepare(self, agent, environment):
            pass

        def exec_environment(self, agent, env):
            return {**env, target: secret}

        async def capture(self, agent, environment):
            pass

    with native_credential_hooks(lambda _: Capsule()), caplog.at_level(logging.DEBUG):
        asyncio.run(
            agent.run("synthetic instruction; no provider", env, AgentContext())
        )
    execs = [(argv, kwargs["env"]) for argv, kwargs in calls if "exec" in argv]
    assert any(
        target in forwarded(argv) and client[target] == secret for argv, client in execs
    )
    assert secret not in repr([argv for argv, _ in calls]) + caplog.text
    assert secret not in agent_config.model_dump_json()
    assert secret not in (tmp_path / "logs/tetrabench-harness.json").read_text()
    for argv, _client in execs:
        assert "SELECTED_TOKEN" not in forwarded(argv)
        assert all("=" not in key for key in forwarded(argv))


@pytest.mark.parametrize("selector", ["env_key", "env_http_headers"])
def test_codex_native_environment_selectors_reach_subprocess(
    tmp_path, monkeypatch, selector
):
    secret = "SYNTHETIC_SELECTOR_TOKEN"
    monkeypatch.setenv("SELECTED_TOKEN", secret)
    provider = {
        "name": "fixture",
        "base_url": "https://synthetic.invalid/v1",
        "wire_api": "responses",
    }
    provider[selector] = (
        "CUSTOM_TOKEN" if selector == "env_key" else {"X-Api-Key": "CUSTOM_TOKEN"}
    )
    spec = HarnessConfig(
        name="codex",
        version=STABLE_VERSIONS["codex"],
        model="openai/gpt-5",
        env={"CUSTOM_TOKEN": "${SELECTED_TOKEN}"},
        native_config=NativeConfig(
            text=json.dumps(
                {"model_provider": "fixture", "model_providers": {"fixture": provider}}
            )
        ),
    )
    config = compile_agent_config(seal_harness(spec, tmp_path))
    agent: Any = AgentFactory.create_agent_from_config(
        config, logs_dir=tmp_path / "logs"
    )
    calls = capture_subprocess(monkeypatch)
    env = environment(tmp_path)
    # Harbor Trial applies AgentConfig.env through this native scoped overlay.
    with env.scoped_exec_env(resolve_env_vars(config.env)):
        asyncio.run(agent.run("synthetic instruction", env, AgentContext()))
    assert any(
        "exec" in argv
        and "CUSTOM_TOKEN" in forwarded(argv)
        and kwargs["env"]["CUSTOM_TOKEN"] == secret
        for argv, kwargs in calls
    )
    assert secret not in repr([argv for argv, _ in calls])


@pytest.mark.parametrize("legacy", [False, True])
def test_binding_and_owner_cancellation_still_wrap_native_job(
    tmp_path, monkeypatch, legacy
):
    config = JobConfig(
        jobs_dir=tmp_path,
        environment=EnvironmentConfig(type=EnvironmentType.DOCKER)
        if legacy
        else Harbor022Api.docker_environment(),
    )
    events = []
    monkeypatch.setattr(
        "tetrabench.docker_lifecycle.bind_docker",
        lambda root: events.append(("bind", root)),
    )
    monkeypatch.setattr(
        "tetrabench.local_control.read_owner_control", lambda root: "control"
    )

    class Owner:
        def __init__(self, control):
            assert control == "control"

        def run(self, operation):
            events.append("owner")
            return asyncio.run(operation())

    async def create(actual):
        assert actual is config
        events.append("create")
        return SimpleNamespace(run=run)

    async def run():
        events.append("run")
        return "result"

    monkeypatch.setattr("tetrabench.local_control.OwnerCancellation", Owner)
    monkeypatch.setattr("tetrabench.harbor_api.Job.create", create)
    assert Harbor022Api().execute(config) == "result"
    assert events == ["owner", ("bind", tmp_path), "create", "run"]


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_historical_native_config_validation(tmp_path, legacy):
    expected = JobConfig(
        environment=Harbor022Api.docker_environment(),
        retry=RetryConfig(exclude_exceptions=None),
    )
    persisted = (
        expected.model_copy(
            update={"environment": EnvironmentConfig(type=EnvironmentType.DOCKER)}
        )
        if legacy
        else expected
    )
    config_bytes = persisted.model_dump_json()
    (tmp_path / "config.json").write_text(config_bytes)
    (tmp_path / "lock.json").write_text(
        JobLock(
            n_concurrent_trials=persisted.n_concurrent_trials,
            retry=persisted.retry,
            trials=[],
        ).model_dump_json()
    )
    result = JobResult(
        id=uuid.uuid4(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        n_total_trials=0,
        stats=JobStats(),
    )
    (tmp_path / "result.json").write_text(result.model_dump_json())
    artifacts = Harbor022Api.validate_native_artifacts(tmp_path, None, expected)
    assert artifacts.config.model_dump_json() == config_bytes
    assert (tmp_path / "config.json").read_text() == config_bytes
    assert expected.environment.import_path == DOCKER_ENVIRONMENT_IMPORT_PATH
    if legacy:
        with pytest.raises(ValueError, match="config changed"):
            Harbor022Api.validate_native_artifacts(tmp_path, result, expected)
    for change in (
        {"delete": False},
        {"import_path": "untrusted:Environment"},
        {"kwargs": {"keep_containers": True}},
    ):
        modified = persisted.model_copy(
            update={"environment": persisted.environment.model_copy(update=change)}
        )
        (tmp_path / "config.json").write_text(modified.model_dump_json())
        with pytest.raises(ValueError, match="config changed"):
            Harbor022Api.validate_native_artifacts(tmp_path, None, expected)


@pytest.mark.docker
def test_real_docker_name_only_values_labels_streams_and_cleanup(
    tmp_path, monkeypatch, caplog
):
    env = environment(tmp_path)
    original = asyncio.create_subprocess_exec
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs.get("env", {})))
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    values = {
        "ANTHROPIC_API_KEY": "SYNTHETIC_CLAUDE=value with 'quotes' $dollar\nline",
        "OPENAI_API_KEY": "SYNTHETIC_CODEX",
        "OPENROUTER_API_KEY": "SYNTHETIC_PI_OPENCODE",
        "CUSTOM_TOKEN": "SYNTHETIC_SELECTOR",
        "EMPTY": "",
    }
    keys = " ".join(values)
    command = (
        "for key in "
        + keys
        + '; do printf "%s" "${!key}" | sha256sum; done; test "${UNSELECTED+x}" != x'
    )
    monkeypatch.setenv("UNSELECTED", "SYNTHETIC_AMBIENT")
    monkeypatch.setenv("EMPTY", "nonempty-host-value")
    streams = []

    async def output(text, stream):
        streams.append((text, stream))

    async def run():
        try:
            await env.start(force_build=False)
            with env.scoped_output_callback(output):
                result = await env.exec(command, env=values, timeout_sec=15)
            assert result.return_code == 0
            assert [line.split()[0] for line in result.stdout.splitlines()] == [
                hashlib.sha256(value.encode()).hexdigest() for value in values.values()
            ]
            assert streams
            assert (
                await env.exec(
                    'test "${EMPTY+x}" != x && test "${CUSTOM_TOKEN+x}" != x'
                )
            ).return_code == 0
            result = await env._run_docker_compose_command(["ps", "--quiet", "main"])
            container_id = result.stdout.strip()
            process = await original(
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                container_id,
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            labels = json.loads(stdout)
            assert labels["com.docker.compose.project.working_dir"] == str(
                env.environment_dir.resolve()
            )
            assert labels["com.docker.compose.project"] == env.session_id
            assert labels["com.docker.compose.service"] == "main"
        finally:
            await env.stop(delete=True)
        result = await env._run_docker_compose_command(["ps", "--all", "--quiet"])
        assert not result.stdout
        assert env._env_compose_temp_dir is None
        assert env._mounts_compose_temp_dir is None

    with credential_free_harbor_environment(), caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    for value in values.values():
        if value:
            assert value not in repr([argv for argv, _ in calls]) + caplog.text
    matching = [
        (argv, client)
        for argv, client in calls
        if "exec" in argv and "CUSTOM_TOKEN" in forwarded(argv)
    ]
    assert len(matching) == 1
    argv, client = matching[0]
    assert set(forwarded(argv)) == set(values)
    assert all(client[key] == value for key, value in values.items())
