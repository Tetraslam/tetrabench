"""Real subprocess/file-transfer witnesses; no accounts or provider calls."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime_auth import FakeHarborEnvironment, new_scope

from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    AuthStateError,
    claim_session,
)
from tetrabench.harness_agents import native_status_command, pi_pipeline_status

PRODUCER = """import json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
step = int(sys.argv[2])
document = json.loads(path.read_bytes())
token = document.get("tokens", document.get("openai-codex"))
key = "access_token" if "tokens" in document else "access"
expected = "SYNTHETIC_INITIAL" if step == 1 else f"SYNTHETIC_ACCESS_STEP_{step-1}"
assert token[key] == expected
token[key] = f"SYNTHETIC_ACCESS_STEP_{step}"
path.write_text(json.dumps(document))
if sys.argv[3] == "kill":
    os.kill(os.getpid(), 9)
print("filtered event")
"""


class ShellEnvironment:
    default_user = str(os.geteuid())

    def __init__(self, harness: str, *, replay: bool = False):
        self.harness = harness
        self.closed = False
        self.processes: list[asyncio.subprocess.Process] = []
        self.roots: set[Path] = set()
        self.witnesses: list[bytes] = []
        self.replay = replay

    async def exec(self, command: str, env=None, **kwargs):
        if "npm root -g" in command:
            return SimpleNamespace(
                return_code=0, stdout="/synthetic/node_modules", stderr=""
            )
        if "login status" in command:
            return SimpleNamespace(
                return_code=0, stdout="", stderr="Logged in using ChatGPT"
            )
        if "--input-type=module" in command:
            return SimpleNamespace(return_code=0, stdout='{"type":"oauth"}', stderr="")
        found = re.search(r"/tmp/tetrabench-auth-[0-9a-f]{32}", command)
        if found:
            self.roots.add(Path(found[0]))
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            env={"PATH": os.defpath, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.processes.append(process)
        out, err = await asyncio.wait_for(process.communicate(), 10)
        if "step_producer.py" in command:
            match = re.search(
                r"/tmp/tetrabench-auth-[0-9a-f]{32}/producer-status-[0-9a-f]{32}\.json",
                command,
            )
            assert match is not None
            path = Path(match[0])
            self.witnesses.append(path.read_bytes())
            if self.replay and len(self.witnesses) == 2:
                path.write_bytes(self.witnesses[0])
        return SimpleNamespace(
            return_code=process.returncode, stdout=out.decode(), stderr=err.decode()
        )

    async def upload_file(self, source, target):
        shutil.copyfile(source, target)

    async def download_file(self, source, target):
        shutil.copyfile(source, target)

    async def stop(self, delete):
        assert all(process.returncode is not None for process in self.processes)
        self.closed = True
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)


class ProcessProof:
    def __init__(self, environment: ShellEnvironment):
        self.environment = environment

    async def stopped(self):
        return self.environment.closed and all(
            process.returncode is not None for process in self.environment.processes
        )


async def capture_processes(environment, engine):
    return ProcessProof(environment)


def command_for(hook, dispatch, script: Path, step: int, *, kill: bool):
    pipeline = shlex.join(
        [
            sys.executable,
            "-I",
            str(script),
            hook.native_path,
            str(step),
            "kill" if kill else "normal",
        ]
    )
    pipeline += " | grep -v . | tee " + shlex.quote(
        str(script.parent / f"step-{step}.log")
    )
    if hook.scope.harness.name == "pi":
        pipeline += "; " + pi_pipeline_status()
    return native_status_command(
        pipeline,
        hook.scope.harness.name,
        dispatch.producer_status_path,
        nonce=dispatch.nonce,
    )


@pytest.mark.parametrize("harness", ["codex", "pi"])
def test_two_real_processes_restore_latest_refresh_after_cleanup(tmp_path, harness):
    scope, store, ref = new_scope(tmp_path, harness)
    scope.proof_factory = capture_processes
    environment = ShellEnvironment(harness)
    script = tmp_path / "step_producer.py"
    script.write_text(PRODUCER)

    async def run():
        hook = scope.new_hook(scope.harness)
        await hook.bind(environment)
        first = hook.new_dispatch()
        result = await hook.execute_model(
            environment, command_for(hook, first, script, 1, kill=False), dispatch=first
        )
        assert result.return_code == 0  # Native zero, grep one is not a failure.
        assert store.read(ref.profile).state.phase == "claimed"
        if harness == "codex":
            shutil.rmtree(Path(hook.root) / "codex")
            shutil.rmtree(Path(hook.root) / "secrets")
        second = hook.new_dispatch()
        assert first.nonce != second.nonce
        assert first.producer_status_path != second.producer_status_path
        with pytest.raises(AuthError):
            await hook.execute_model(environment, "never execute", dispatch=first)
        result = await hook.execute_model(
            environment,
            command_for(hook, second, script, 2, kill=False),
            dispatch=second,
        )
        assert result.return_code == 0
        assert hook.processes_settled
        assert not hook.physically_stopped
        with pytest.raises(AuthBusyError):
            claim_session(store, ref, harness)
        await environment.stop(delete=True)

    try:
        asyncio.run(run())
        scope.finalize(require_consumer=True)
        successor = claim_session(store, ref, harness)
        assert b"SYNTHETIC_ACCESS_STEP_2" in successor.snapshot.state.native
    finally:
        for root in environment.roots:
            shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("replay", [False, True])
def test_second_real_process_kill_or_replayed_first_witness_blocks_lineage(
    tmp_path, replay
):
    scope, store, ref = new_scope(tmp_path, "pi")
    scope.proof_factory = capture_processes
    environment = ShellEnvironment("pi", replay=replay)
    script = tmp_path / "step_producer.py"
    script.write_text(PRODUCER)

    async def run():
        hook = scope.new_hook(scope.harness)
        await hook.bind(environment)
        first = hook.new_dispatch()
        await hook.execute_model(
            environment, command_for(hook, first, script, 1, kill=False), dispatch=first
        )
        second = hook.new_dispatch()
        command = command_for(hook, second, script, 2, kill=True)
        if replay:
            with pytest.raises(AuthError):
                await hook.execute_model(environment, command, dispatch=second)
        else:
            assert (
                await hook.execute_model(environment, command, dispatch=second)
            ).return_code == 137
        assert not hook.processes_settled
        await environment.stop(delete=True)
        assert json.loads(environment.witnesses[0])["nonce"] == first.nonce
        assert json.loads(environment.witnesses[1])["nonce"] == second.nonce

    try:
        asyncio.run(run())
        with pytest.raises(AuthStateError):
            scope.finalize()
        with pytest.raises(AuthBusyError):
            claim_session(store, ref, "pi")
        assert b"SYNTHETIC_ACCESS_STEP_2" in store.read(ref.profile).state.native
    finally:
        for root in environment.roots:
            shutil.rmtree(root, ignore_errors=True)


def test_dispatch_remains_exclusive_during_writeback(tmp_path):
    scope, _, _ = new_scope(tmp_path, "pi")

    class PausedCapture(FakeHarborEnvironment):
        def __init__(self):
            super().__init__("pi")
            self.capture = asyncio.Event()
            self.resume = asyncio.Event()

        async def download_file(self, source, target):
            if "/snapshot-" in source:
                self.capture.set()
                await self.resume.wait()
            await super().download_file(source, target)

    async def run():
        environment = PausedCapture()
        hook = scope.new_hook(scope.harness)
        await hook.bind(environment)
        dispatch = hook.new_dispatch()
        task = asyncio.create_task(
            hook.execute_model(
                environment,
                "SYNTHETIC_NATIVE_MODEL " + dispatch.producer_status_path,
                dispatch=dispatch,
            )
        )
        await asyncio.wait_for(environment.capture.wait(), 5)
        with pytest.raises(AuthBusyError):
            hook.new_dispatch()
        environment.resume.set()
        await task
        await environment.stop(delete=True)

    asyncio.run(run())
    scope.finalize(require_consumer=True)


def test_reasoning_preload_applies_only_to_model_not_auth_helpers(tmp_path):
    scope, _, _ = new_scope(tmp_path, "pi", mode="api_key")

    class Environment(FakeHarborEnvironment):
        async def exec(self, command, env: dict[str, str] | None = None, **kwargs):
            if "npm root -g" in command or "--input-type=module" in command:
                assert "NODE_OPTIONS" not in (env or {})
                assert "TETRABENCH_PI_CAPABILITY_PROBE" not in (env or {})
            if "SYNTHETIC_NATIVE_MODEL" in command:
                assert env is not None
                assert env["NODE_OPTIONS"] == "--import=/synthetic/guard.mjs"
            return await super().exec(command, env=env, **kwargs)

    async def run():
        environment = Environment("pi")
        environment.mode = "api_key"
        hook = scope.new_hook(scope.harness)
        await hook.bind(environment)
        dispatch = hook.new_dispatch()
        await hook.execute_model(
            environment,
            "SYNTHETIC_NATIVE_MODEL " + dispatch.producer_status_path,
            dispatch=dispatch,
            env={
                "NODE_OPTIONS": "--import=/synthetic/guard.mjs",
                "TETRABENCH_PI_CAPABILITY_PROBE": "/synthetic/probe.json",
            },
        )
        await environment.stop(delete=True)

    asyncio.run(run())
    scope.finalize(require_consumer=True)
