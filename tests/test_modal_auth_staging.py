"""Regression for image im-DHv9Pt5hw47vPSB5b8DzCx, wheel 954136ad...ab6cb.

The authorized stat-only invocation observed euid=0, /tmp uid=0 mode=0777
(no sticky), and a fresh temp child uid=0 mode=0700. The old default-temp
allocation failed private_directory before Harbor could create any child.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_controller_runtime import _invocation, _Observer, _request, _Store, _Volume

from tetrabench.auth_config import AuthSpec, EnvAuthReference
from tetrabench.auth_sessions import (
    AuthError,
    AuthStateError,
    auth_failure_diagnostic,
    private_directory,
)
from tetrabench.controller_runtime import ControllerRuntime
from tetrabench.harbor_runner import HarborRunner
from tetrabench.harness_config import ResolvedHarness
from tetrabench.plan import plan_digest
from tetrabench.runtime_auth import current_runtime_auth, make_credential_context


def _stat(mode: int, uid: int = 0) -> os.stat_result:
    return os.stat_result((stat.S_IFDIR | mode, 1, 1, 1, uid, uid, 0, 0, 0, 0))


def test_exact_deployed_filesystem_facts_remain_rejected(monkeypatch):
    facts = {
        Path("/tmp"): _stat(0o777),
        Path("/tmp/tetrabench-auth-stat-fixture"): _stat(0o700),
    }
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "lstat", lambda path: facts[path])
    with pytest.raises(AuthStateError) as error:
        private_directory(Path("/tmp/tetrabench-auth-stat-fixture"))
    assert str(error.value) == "auth directory has writable ancestors"
    assert auth_failure_diagnostic(error.value) == {
        "reason": "auth-parent-writable",
        "action": "select-private-auth-staging-outside-writable-ancestors",
    }
    # Trusted sticky /tmp remains accepted, without admitting plain 0777.
    facts[Path("/tmp")] = _stat(0o1777)
    assert private_directory(Path("/tmp/tetrabench-auth-stat-fixture"))


def _keyed_request():
    request = _request()
    harness = ResolvedHarness(
        name="claude-code",
        version="2.1.267",
        model="anthropic/claude-opus-5[1m]",
        auth=AuthSpec(
            mode="api_key", reference=EnvAuthReference(name="ANTHROPIC_API_KEY")
        ),
    )
    plan = request.plan.model_copy(update={"harness": harness})
    return request.model_copy(update={"plan": plan, "plan_sha256": plan_digest(plan)})


class NativeBoundaryReached(RuntimeError):
    pass


@pytest.mark.parametrize("unsafe_home", [False, True])
def test_real_controller_auth_entry_with_modal_temp_shape(
    tmp_path, monkeypatch, capsys, unsafe_home
):
    request = _keyed_request()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    bad_temp = tmp_path / "modal-tmp"
    bad_temp.mkdir()
    bad_temp.chmod(0o777)
    monkeypatch.setattr(tempfile, "tempdir", str(bad_temp))
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: bad_temp if unsafe_home else home)
    )
    model_key = "SYNTHETIC_SELECTED_CLAUDE_KEY"
    storage_key = "SYNTHETIC_ARTIFACT_STORAGE_KEY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", model_key)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "SYNTHETIC_ARTIFACT_ACCESS")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", storage_key)
    # This reconstructs the old failure with an actual private child in an
    # unsafe temp parent; no remote execution or credential value is involved.
    with tempfile.TemporaryDirectory(prefix="old-default-") as old:
        with pytest.raises(AuthStateError, match="writable ancestors"):
            private_directory(Path(old))
    calls = []
    stages = []

    def native_on_controller(*args, **kwargs):
        pytest.fail("controller must not invoke a native agent binary")

    monkeypatch.setattr("tetrabench.auth.run_native", native_on_controller)

    class Api:
        def task_config(self, **kwargs):
            return SimpleNamespace(**kwargs)

        def import_path_environment(self, **kwargs):
            return SimpleNamespace(**kwargs)

        def job_config(self, **kwargs):
            return SimpleNamespace(**kwargs)

        def execute(self, config):
            calls.append("native-launch-boundary")
            hook = current_runtime_auth(request.plan.harness)
            assert hook is not None
            stages.append(hook.scope.directory)
            assert hook.scope.directory.is_relative_to(home)
            assert not hook.scope.directory.is_relative_to(bad_temp)
            assert hook.scope.directory.stat().st_mode & 0o777 == 0o700
            assert hook.agent_environment == {"ANTHROPIC_API_KEY": model_key}
            assert "ANTHROPIC_API_KEY" not in os.environ
            assert "AWS_ACCESS_KEY_ID" not in os.environ
            assert "AWS_SECRET_ACCESS_KEY" not in os.environ
            assert model_key not in repr(config)
            raise NativeBoundaryReached("fixture stops before child creation")

    class Runner(HarborRunner):
        def run(self, request, paths, **kwargs):
            (paths.context / "task.module").mkdir(parents=True)
            return super().run(request, paths, **kwargs)

    operations = []
    store = _Store(request, operations)
    volume_root = tmp_path / "controller-volume"
    context = make_credential_context(
        engine="modal",
        consumer_id="fc-SYNTHETIC",
        run_id=request.run_id,
        environment={
            "ANTHROPIC_API_KEY": model_key,
            "AWS_ACCESS_KEY_ID": "SYNTHETIC_ARTIFACT_ACCESS",
            "AWS_SECRET_ACCESS_KEY": storage_key,
        },
        forbidden_runtime_roots=[volume_root],
    )
    api: Any = Api()
    runtime = ControllerRuntime(
        store,
        _Volume(operations),
        Runner(api=api, credential_context=context),
        _Observer(operations),
        controller_root=volume_root,
        attempt_id=lambda: "attempt-one",
    )
    published = []
    store.after_stream_read = published.append
    result = runtime.run(_invocation(request), function_call_id="fc-SYNTHETIC")
    assert result.state == "failed"  # Deliberate test boundary, never an inference.
    assert "s3:cas:running" in operations
    output = capsys.readouterr().out
    assert model_key not in output and storage_key not in output
    assert not any(
        model_key.encode() in data or storage_key.encode() in data for data in published
    )
    if unsafe_home:
        assert not calls
        assert "reason=auth-parent-writable" in output
        assert any(
            b'"auth_diagnostic"' in data and b'"auth-parent-writable"' in data
            for data in published
        )
    else:
        assert calls == ["native-launch-boundary"]
        assert result.detail == "NativeBoundaryReached"
        assert all(not path.exists() for path in stages)


def test_modal_private_home_must_not_be_in_controller_volume(tmp_path, monkeypatch):
    from tetrabench.local_execution import local_paths

    volume = tmp_path / "controller-volume"
    volume.mkdir(mode=0o700)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: volume))
    context = make_credential_context(
        engine="modal",
        consumer_id="fc-SYNTHETIC",
        environment={"ANTHROPIC_API_KEY": "SYNTHETIC_KEY"},
        forbidden_runtime_roots=[volume],
    )
    with pytest.raises(AuthStateError) as error:
        with context(_keyed_request().plan.harness, local_paths(volume / "attempt")):
            pytest.fail("overlapping auth root was admitted")
    diagnostic = auth_failure_diagnostic(error.value)
    assert diagnostic is not None and diagnostic["reason"] == "auth-output-overlap"
    assert not (volume / ".tetrabench-native-auth").exists()


def test_auth_diagnostics_never_serialize_messages_or_unknown_reasons():
    error = AuthError("SYNTHETIC_PRIVATE_MESSAGE", reason="SYNTHETIC_PRIVATE_REASON")
    assert auth_failure_diagnostic(error) == {
        "reason": "auth-runtime",
        "action": "check-auth-profile-and-native-runtime",
    }
