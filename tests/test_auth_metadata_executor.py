from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from test_runtime_auth import FakeHarborEnvironment, native, new_scope

from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    AuthStateError,
    claim_session,
)


def command():
    root = "/tmp/tetrabench-capability-" + uuid.uuid4().hex
    return (
        "if [ -f ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
        'export PATH="$HOME/.local/bin:$PATH"; '
        f"python3 {root}/runtime_metadata.py {root}/startup-probe.json"
    )


class MetadataEnvironment(FakeHarborEnvironment):
    def __init__(self, *, failed=False, leak=False):
        super().__init__("codex")
        self.failed, self.leak = failed, leak
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.seen_env = {}

    async def exec(self, command, env: dict[str, str] | None = None, **kwargs):
        if "runtime_metadata.py" in command and "auth-io.py" not in command:
            assert env is not None
            self.started.set()
            self.seen_env = env
            await self.resume.wait()
            self.files[env["CODEX_HOME"] + "/auth.json"] = native(
                "codex", "SYNTHETIC_METADATA_REFRESH"
            )
            return SimpleNamespace(
                return_code=1 if self.failed else 0,
                stdout=json.dumps(
                    {
                        "inference_validated": False,
                        "auth_custody": {
                            "schema_version": 1,
                            "credential_processes": 1,
                            "graceful_credential_processes": 1,
                        },
                        "value": "SYNTHETIC_METADATA_REFRESH" if self.leak else "safe",
                    }
                ),
                stderr="",
            )
        return await super().exec(command, env=env, **kwargs)


def test_metadata_runs_serially_and_checkpoints_same_pending_model_lineage(tmp_path):
    scope, store, ref = new_scope(tmp_path)

    async def run():
        environment: Any = MetadataEnvironment()
        hook = scope.new_hook(scope.harness)
        await hook.bind(environment)
        dispatch = hook.new_dispatch()
        task = asyncio.create_task(
            hook.execute_metadata(
                environment, command(), env={"NODE_OPTIONS": "model-only"}
            )
        )
        await asyncio.wait_for(environment.started.wait(), 5)
        with pytest.raises(AuthBusyError):
            await hook.execute_metadata(environment, command())
        with pytest.raises(AuthError):
            await hook.execute_model(environment, "not run", dispatch=dispatch)
        environment.resume.set()
        report = await task
        assert json.loads(report.stdout)["inference_validated"] is False
        assert "NODE_OPTIONS" not in environment.seen_env
        assert hook.producer_status_path == dispatch.producer_status_path
        assert b"SYNTHETIC_METADATA_REFRESH" in store.read(ref.profile).state.native
        assert store.read(ref.profile).state.phase == "claimed"
        assert b"SYNTHETIC_METADATA_REFRESH" in hook.codex_source_path.read_bytes()
        provenance = hook.observed_auth_provenance()
        assert provenance["observed"]["mode"] == "chatgpt_oauth"
        assert provenance["observed"]["source"] == "native_status"
        assert provenance["account_verified"] is False
        assert "SYNTHETIC" not in json.dumps(provenance)
        await hook.execute_model(
            environment,
            "SYNTHETIC_NATIVE_MODEL " + dispatch.producer_status_path,
            dispatch=dispatch,
        )
        await environment.stop(delete=True)

    asyncio.run(run())
    scope.finalize(require_consumer=True)
    assert claim_session(store, ref, "codex").snapshot.state.phase == "claimed"


@pytest.mark.parametrize("failed,leak", [(True, False), (False, True)])
def test_metadata_failure_or_secret_output_keeps_lineage_blocked(
    tmp_path, failed, leak
):
    scope, store, ref = new_scope(tmp_path)

    async def run():
        environment = MetadataEnvironment(failed=failed, leak=leak)
        environment.resume.set()
        hook = scope.new_hook(scope.harness)
        with pytest.raises(AuthError) as error:
            await hook.execute_metadata(environment, command())
        assert "SYNTHETIC_METADATA_REFRESH" not in str(error.value)
        await environment.stop(delete=True)

    asyncio.run(run())
    with pytest.raises(AuthStateError):
        scope.finalize()
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, "codex")
    assert b"SYNTHETIC_METADATA_REFRESH" in store.read(ref.profile).state.native


def test_metadata_rejects_prompt_commands_and_observed_mode_mismatch(tmp_path):
    scope, _, _ = new_scope(tmp_path)

    async def run():
        environment: Any = MetadataEnvironment()
        hook = scope.new_hook(scope.harness)
        with pytest.raises(AuthError, match="reviewed"):
            await hook.execute_metadata(environment, "codex exec 'a model prompt'")
        assert not environment.commands
        original = environment.exec

        async def wrong_status(command, **kwargs):
            if "login status" in command:
                return SimpleNamespace(
                    return_code=0,
                    stdout="",
                    stderr="Logged in using an API key - SYNTHETI***I_KEY",
                )
            return await original(command, **kwargs)

        environment.exec = wrong_status
        environment.resume.set()
        with pytest.raises(AuthError):
            await hook.execute_metadata(environment, command())
        evidence = hook.observed_auth_provenance()
        assert evidence["requested_mode"] == "chatgpt_oauth"
        assert evidence["observed"]["mode"] == "api_key"
        assert evidence["account_verified"] is False
        await environment.stop(delete=True)

    asyncio.run(run())
