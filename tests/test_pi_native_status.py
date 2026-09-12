"""The native producer, not grep/tee output, determines credential release."""

import asyncio
import json
import shlex
import subprocess
import sys
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.agent.context import AgentContext
from native_consumer_support import native_modules, native_run
from test_runtime_auth import FakeHarborEnvironment, new_scope

from tetrabench.auth_sessions import AuthStateError
from tetrabench.harness_agents import native_status_command, pi_pipeline_status
from tetrabench.harness_config import HarnessConfig, HarnessSession
from tetrabench.harnesses import compile_agent_config


def shell_status(
    producer, filter_command="grep -v message_update", sink="cat >/dev/null"
):
    return subprocess.run(
        [
            "bash",
            "-c",
            f"set -o pipefail; set +e; {producer} | {filter_command} | {sink}; "
            + pi_pipeline_status(),
        ],
        capture_output=True,
        env={"PATH": "/usr/bin:/bin"},
    ).returncode


@pytest.mark.parametrize("code", [1, 42, 130, 136, 137, 139, 143])
def test_pipeline_preserves_native_exit_even_when_grep_has_no_lines(code):
    assert shell_status(f"(exit {code})") == code


def test_real_killed_producer_is_not_masked_by_failed_filters():
    producer = shlex.join(
        [sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGKILL)"]
    )
    assert shell_status(producer) == 137
    assert shell_status(producer, "(exit 2)", "(exit 1)") == 137
    assert shell_status("(exit 0)") == 0
    assert shell_status("(exit 0)", "(exit 2)") == 1


@pytest.mark.parametrize("name", ["pi", "opencode", "codex", "claude-code"])
def test_private_witness_records_native_signal_not_pipeline_status(tmp_path, name):
    path = tmp_path / "producer-status.json"
    producer = shlex.join(
        [sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGKILL)"]
    )
    pipeline = f"{producer} | grep -v message_update | cat >/dev/null"
    if name == "claude-code":
        pipeline = f"printf '' | {producer} | cat >/dev/null"
    if name == "pi":
        pipeline += "; " + pi_pipeline_status()
    command = native_status_command(pipeline, name, str(path))
    result = subprocess.run(["bash", "-c", command], capture_output=True)
    assert result.returncode == 137
    assert json.loads(path.read_text()) == {"schema_version": 1, "exit_code": 137}
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "session",
    [
        HarnessSession(load_trajectory="seed.jsonl"),
        HarnessSession(resume_trajectory=True),
    ],
)
def test_no_session_rejected_with_loaded_or_resumed_history(session):
    with pytest.raises(ValueError, match="persistence"):
        HarnessConfig(
            name="pi",
            version="0.85.1",
            model="openai/model",
            options={"no_session": True},
            session=session,
        )


@pytest.mark.native
def test_current_pi_session_manager_preserves_seed_except_no_session(tmp_path):
    modules = native_modules(required=True)
    assert modules is not None
    path = tmp_path / "seed.jsonl"
    records = [
        {
            "type": "session",
            "version": 3,
            "id": "11111111-1111-4111-8111-111111111111",
            "timestamp": "2026-09-10T00:00:00Z",
            "cwd": str(tmp_path),
        },
        {
            "type": "message",
            "id": "abc12345",
            "parentId": None,
            "timestamp": "2026-09-10T00:00:01Z",
            "message": {
                "role": "user",
                "content": "retain-this-native-fact",
                "timestamp": 1,
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    module = modules / "@earendil-works/pi-coding-agent/dist/main.js"
    program = (
        f"import {{createSessionManager}} from {json.dumps(module.as_uri())};"
        "const cwd=process.cwd();"
        "const yes=await createSessionManager({continue:true},cwd,cwd);"
        "const no=await createSessionManager({continue:true,noSession:true},cwd,cwd);"
        "console.log(JSON.stringify({yes:yes.buildSessionContext(),no:no.buildSessionContext()}));"
    )
    result = native_run(["node", "--input-type=module", "-e", program], tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "retain-this-native-fact" in json.dumps(data["yes"])
    assert "retain-this-native-fact" not in json.dumps(data["no"])


def test_killed_real_producer_blocks_runtime_credential_release(tmp_path, monkeypatch):
    scope, store, ref = new_scope(tmp_path, "pi")
    monkeypatch.setattr(
        "tetrabench.runtime_auth.current_runtime_auth",
        lambda harness: scope.new_hook(harness),
    )

    class Environment(FakeHarborEnvironment):
        async def exec(self, command, env=None, **kwargs):
            result = await super().exec(command, env=env, **kwargs)
            if "pi --print --mode json" in command:
                assert "PIPESTATUS" in command
                path = tmp_path / "actual-producer-status.json"
                producer = shlex.join(
                    [
                        sys.executable,
                        "-c",
                        "import os,signal; os.kill(os.getpid(),signal.SIGKILL)",
                    ]
                )
                pipeline = (
                    producer
                    + " | grep -v message_update | cat >/dev/null; "
                    + pi_pipeline_status()
                )
                result.return_code = subprocess.run(
                    [
                        "bash",
                        "-c",
                        native_status_command(
                            pipeline,
                            "pi",
                            str(path),
                            nonce=instance._native_producer_nonce,
                        ),
                    ],
                    capture_output=True,
                ).returncode
                self.files[scope.hooks[0].producer_status_path] = path.read_bytes()
            return result

    environment = Environment("pi")
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(scope.harness), logs_dir=tmp_path / "logs"
    )

    async def run():
        with pytest.raises(NonZeroAgentExitCodeError):
            await instance.run("SYNTHETIC_NATIVE_MODEL", environment, AgentContext())
        await environment.stop(delete=True)

    asyncio.run(run())
    assert scope.poisoned
    assert scope.hooks[0].producer_exit_code == 137
    with pytest.raises(AuthStateError):
        scope.finalize()
    state = store.read(ref.profile)
    assert state is not None and state.state.phase == "claimed"
