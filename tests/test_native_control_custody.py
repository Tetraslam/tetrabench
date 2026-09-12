"""Native process responses are not evidence of successful credential teardown."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_auth_multistep import ShellEnvironment, capture_processes
from test_runtime_auth import new_scope

from tetrabench import native_control
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    AuthStateError,
    claim_session,
)
from tetrabench.native_control import (
    CUSTODY_ENV,
    ControlError,
    ControlProcess,
    CredentialCompletionError,
    auth_custody_report,
)


@pytest.mark.parametrize("finish", ["normal", "nonzero", "killed", "forced"])
def test_control_response_requires_graceful_native_completion(
    tmp_path, monkeypatch, finish
):
    monkeypatch.setattr(native_control, "_credential_processes", 0)
    monkeypatch.setattr(native_control, "_graceful_credential_processes", 0)
    suffix = {
        "normal": "sys.stdin.read()",
        "nonzero": "sys.exit(1)",
        "killed": "os.kill(os.getpid(), 9)",
        "forced": "time.sleep(30)",
    }[finish]
    script = "import os,sys,time; print('metadata response', flush=True); " + suffix
    process = ControlProcess(
        [sys.executable, "-I", "-c", script], tmp_path, {CUSTODY_ENV: "required"}
    )
    if finish == "normal":
        with process:
            assert process.line() == "metadata response"
        assert process.graceful and process.return_code == 0
    else:
        with pytest.raises(CredentialCompletionError, match="unproven"):
            with process:
                assert process.line() == "metadata response"
        assert not process.graceful
        with pytest.raises(CredentialCompletionError):
            process.close()  # An idempotent close cannot launder prior failure.
    assert process.process.poll() is not None
    assert process.process.stdout is not None and process.process.stdout.closed
    assert auth_custody_report() == {
        "schema_version": 1,
        "credential_processes": 1,
        "graceful_credential_processes": 1 if finish == "normal" else 0,
    }


def test_static_version_failure_does_not_register_a_credential_consumer(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(native_control, "_credential_processes", 0)
    monkeypatch.setattr(native_control, "_graceful_credential_processes", 0)
    with pytest.raises(ControlError):
        with ControlProcess(
            [sys.executable, "-I", "-c", "print('version'); raise SystemExit(1)"],
            tmp_path,
            {CUSTODY_ENV: "required"},
            credential_consumer=False,
        ) as process:
            assert process.line() == "version"
    assert auth_custody_report()["credential_processes"] == 0


def test_noncredential_server_shutdown_is_explicit_not_graceful(tmp_path):
    with ControlProcess(
        [
            sys.executable,
            "-I",
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        tmp_path,
        {},
    ) as process:
        assert process.line() == "ready"
    assert process.forced_termination
    assert not process.graceful
    assert process.return_code != 0


def test_graceful_eof_drains_bounded_output_and_closed_buffer_cannot_be_reused(
    tmp_path,
):
    script = (
        "import sys; print('reply', flush=True); sys.stdin.read(); "
        "print('x' * 200000, flush=True)"
    )
    with ControlProcess(
        [sys.executable, "-I", "-c", script], tmp_path, {CUSTODY_ENV: "required"}
    ) as process:
        assert process.line() == "reply"
    assert process.graceful
    assert process.read_bytes >= 200000
    with pytest.raises(ControlError, match="closed"):
        process.line()
    with pytest.raises(ControlError, match="closed"):
        process.send({"method": "model/list"})


def test_runtime_rejects_helper_zero_with_unsafe_native_completion_report(tmp_path):
    from test_auth_metadata_executor import MetadataEnvironment, command

    scope, store, ref = new_scope(tmp_path, "codex")

    class Environment(MetadataEnvironment):
        async def exec(self, command, env=None, **kwargs):
            result = await super().exec(command, env=env, **kwargs)
            if "runtime_metadata.py" in command and "auth-io.py" not in command:
                report = json.loads(result.stdout)
                report["auth_custody"]["graceful_credential_processes"] = 0
                result.stdout = json.dumps(report)
            return result

    async def run():
        environment = Environment()
        environment.resume.set()
        hook = scope.new_hook(scope.harness)
        with pytest.raises(AuthError):
            await hook.execute_metadata(environment, command())
        await environment.stop(delete=True)

    asyncio.run(run())
    with pytest.raises(AuthStateError):
        scope.finalize()
    assert store.read(ref.profile).state.phase == "claimed"


def test_pre_handoff_refusal_does_not_block_unused_profile(tmp_path):
    scope, store, ref = new_scope(tmp_path, "codex")
    hook = scope.new_hook(scope.harness)
    with pytest.raises(AuthError):
        asyncio.run(
            hook.execute_metadata(ShellEnvironment("codex"), "not a metadata command")
        )
    assert not hook.bound
    scope.finalize()
    assert store.read(ref.profile).state.phase == "ready"


NATIVE = """import json,os,sys
if "--version" in sys.argv:
    print("codex-cli 0.154.0")
    raise SystemExit(0)
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request: continue
    method = request["method"]
    result = {}
    if method == "config/read":
        result = {"config": {"model_provider":"openai", "model_reasoning_effort":"high",
          "model_providers":{"openai":{"wire_api":"responses","base_url":"https://example.invalid"}}}}
    if method == "model/list":
        result = {"data":[{"model":"model",
          "supportedReasoningEfforts":[{"reasoningEffort":"high"}]}], "nextCursor":None}
    print(json.dumps({"id":request["id"],"result":result}), flush=True)
    if method == "model/list" and os.environ.get("SYNTHETIC_END") == "killed":
        os.kill(os.getpid(), 9)
    if method == "model/list" and os.environ.get("SYNTHETIC_END") == "forced":
        import time
        time.sleep(30)
"""


@pytest.mark.parametrize("finish", ["normal", "killed", "forced"])
def test_native_reply_then_abnormal_exit_never_releases_oauth_lineage(tmp_path, finish):
    scope, store, ref = new_scope(tmp_path, "codex")
    scope.proof_factory = capture_processes
    environment: Any = ShellEnvironment("codex")
    guard = Path("/tmp") / ("tetrabench-capability-" + uuid.uuid4().hex)
    guard.mkdir(mode=0o700)
    fixture = tmp_path / "synthetic_codex.py"
    fixture.write_text(NATIVE)
    package = Path(native_control.__file__).parent
    for name in ("native_control.py", "runtime_metadata.py"):
        (guard / name).write_bytes((package / name).read_bytes())
        (guard / name).chmod(0o600)
    probe = {
        "identity": {
            "harness": "codex",
            "harness_version": "0.154.0",
            "requested_model": "openai/model",
            "resolved_model": "model",
            "provider_id": "openai",
            "protocol": "responses",
            "endpoints": ["https://example.invalid"],
        },
        "command": [sys.executable, "-I", str(fixture)],
        "files": [],
        "absent_files": [],
        "policy": "record",
        "selected": "high",
        "native_value": "high",
        "snapshot_sha256": "a" * 64,
        # Deliberately false: request JSON cannot downgrade the runtime owner's custody.
        "refreshable_auth": False,
    }
    (guard / "startup-probe.json").write_text(json.dumps(probe))
    command = (
        "if [ -f ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
        'export PATH="$HOME/.local/bin:$PATH"; '
        f"python3 {guard}/runtime_metadata.py {guard}/startup-probe.json"
    )
    outcomes = []
    original = environment.exec
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    async def execute(command, **kwargs):
        if "/runtime_metadata.py " in command and "auth-io.py" not in command:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                cwd=tmp_path,
                env={"PATH": os.defpath, "HOME": str(home), **kwargs.get("env", {})},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            environment.processes.append(process)
            out, err = await asyncio.wait_for(process.communicate(), 10)
            result = SimpleNamespace(
                return_code=process.returncode, stdout=out.decode(), stderr=err.decode()
            )
            outcomes.append(result)
            return result
        return await original(command, **kwargs)

    environment.exec = execute

    async def run():
        hook = scope.new_hook(scope.harness)
        if finish == "normal":
            result = await hook.execute_metadata(
                environment, command, env={"SYNTHETIC_END": finish}
            )
            assert result.return_code == 0
            assert (
                json.loads(result.stdout)["auth_custody"][
                    "graceful_credential_processes"
                ]
                == 1
            )
        else:
            with pytest.raises(AuthError):
                await hook.execute_metadata(
                    environment, command, env={"SYNTHETIC_END": finish}
                )
            assert outcomes[-1].return_code == 78
            report = json.loads(outcomes[-1].stdout)
            assert report["auth_custody"]["credential_processes"] == 1
            assert report["auth_custody"]["graceful_credential_processes"] == 0
        await environment.stop(delete=True)

    try:
        asyncio.run(run())
        if finish == "normal":
            scope.finalize()
            assert store.read(ref.profile).state.phase == "ready"
        else:
            with pytest.raises(AuthStateError):
                scope.finalize()
            assert store.read(ref.profile).state.phase == "claimed"
            with pytest.raises(AuthBusyError):
                claim_session(store, ref, "codex")
    finally:
        shutil.rmtree(guard)
        for root in environment.roots:
            shutil.rmtree(root, ignore_errors=True)
