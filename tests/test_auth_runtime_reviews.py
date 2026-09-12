from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_runtime_auth import FakeHarborEnvironment, native, native_trial, new_scope

from tetrabench.artifact_policy import ArtifactLimits
from tetrabench.auth_config import (
    AuthSpec,
    EnvAuthReference,
    credential_env_name,
    validate_auth_spec,
)
from tetrabench.auth_retention import (
    AuthRetentionError,
    KnownAuthRetention,
    outputs_blocked,
)
from tetrabench.auth_sessions import AuthBusyError, AuthStateError, claim_session
from tetrabench.controller_runtime import ControllerRuntime
from tetrabench.harness_agents import _producer_status_write, pi_pipeline_status
from tetrabench.local_execution import local_paths
from tetrabench.records import ContentObject


class PipelineEnvironment(FakeHarborEnvironment):
    def __init__(self, work: Path, *, killed: bool):
        super().__init__("pi")
        self.work, self.killed = work, killed

    async def exec(self, command: str, env: dict[str, str] | None = None, **kwargs):
        if "native-pipeline-test" not in command:
            return await super().exec(command, env=env, **kwargs)
        assert env is not None
        root = env["PI_CODING_AGENT_DIR"].rsplit("/", 1)[0]
        match = re.search(r"producer-status-([0-9a-f]{32})\.json", command)
        assert match is not None
        nonce = match[1]
        status = self.work / "producer-status.json"
        script = (
            "import os,sys; print('{\"exit_code\":0}', file=sys.stderr, flush=True); "
            "os.kill(os.getpid(), 9)"
            if self.killed
            else "print('filtered event')"
        )
        pipeline = (
            shlex.join([sys.executable, "-I", "-c", script])
            + " | grep -v . | tee "
            + shlex.quote(str(self.work / "native.log"))
        )
        # Reproduce the bug: expose grep's 1 even when the producer was killed.
        # The private witness comes from the real kernel PIPESTATUS vector.
        wrapper = (
            "set +e; " + pipeline + '; tetrabench_pi_status=("${PIPESTATUS[@]}"); '
        )
        if self.killed:
            wrapper += _producer_status_write(str(status), 0, nonce)
            wrapper += 'exit "${tetrabench_pi_status[1]}"'
        else:
            wrapper = (
                "set +e; "
                + pipeline
                + "; "
                + pi_pipeline_status(str(status), nonce=nonce)
            )
        result = subprocess.run(
            ["bash", "-c", wrapper],
            cwd=self.work,
            env={"PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == (1 if self.killed else 0)
        self.files[root + f"/producer-status-{nonce}.json"] = status.read_bytes()
        return SimpleNamespace(
            return_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )


@pytest.mark.parametrize("killed", [False, True])
def test_real_pipeline_uses_native_producer_not_filter_status(tmp_path, killed):
    scope, store, ref = new_scope(tmp_path, "pi")
    environment = PipelineEnvironment(tmp_path, killed=killed)

    async def execute():
        hook = scope.new_hook(scope.harness)
        await hook.bind(environment)
        dispatch = hook.new_dispatch()
        result = await hook.execute_model(
            environment,
            "native-pipeline-test " + dispatch.producer_status_path,
            dispatch=dispatch,
        )
        await environment.stop(delete=True)
        return hook, result

    hook, result = asyncio.run(execute())
    assert hook.producer_exit_code == (137 if killed else 0)
    assert result.return_code == hook.producer_exit_code
    if killed:
        with pytest.raises(AuthStateError, match="unproven"):
            scope.finalize()
        with pytest.raises(AuthBusyError):
            claim_session(store, ref, "pi")
    else:
        scope.finalize(require_consumer=True)
        assert claim_session(store, ref, "pi").snapshot.state.native == native("pi")


def registry() -> KnownAuthRetention:
    guard = KnownAuthRetention()
    guard.native("pi", native("pi", "SYNTHETIC_INITIAL_ACCESS"))
    guard.native("pi", native("pi", "SYNTHETIC_ROTATED_ACCESS"))
    return guard


def test_original_and_rotated_literals_feed_real_harbor_text_scrubber(tmp_path):
    attempt = tmp_path / "attempt"
    job = attempt / "harbor-job"
    job.mkdir(parents=True)
    log = job / "native.log"
    log.write_text(
        "SYNTHETIC_INITIAL_ACCESS then SYNTHETIC_ROTATED_ACCESS and SYNTHETIC_REFRESH"
    )
    guard = registry()
    guard.enforce(
        job, attempt_root=attempt, private_parent=tmp_path, refresh_unknown=False
    )
    assert "SYNTHETIC" not in log.read_text()
    assert "[REDACTED]" in log.read_text()
    assert not outputs_blocked(attempt)


def test_sqlite_literal_is_quarantined_byte_exact_and_never_published(tmp_path):
    attempt = tmp_path / "attempt"
    job = attempt / "harbor-job"
    job.mkdir(parents=True)
    database = job / "opencode.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table tokens (value text)")
        connection.execute(
            "insert into tokens values (?)", ("SYNTHETIC_ROTATED_ACCESS",)
        )
    original = database.read_bytes()
    guard = registry()
    with pytest.raises(AuthRetentionError):
        guard.enforce(
            job, attempt_root=attempt, private_parent=tmp_path, refresh_unknown=False
        )
    assert guard.quarantine_path is not None
    assert (guard.quarantine_path / "opencode.db").read_bytes() == original
    marker = (attempt / "native-auth-retention.json").read_bytes()
    assert (
        b"SYNTHETIC" not in marker
        and b"sha256" not in marker
        and b"opencode.db" not in marker
    )
    runtime: Any = object.__new__(ControllerRuntime)
    runtime._artifact_limits = ArtifactLimits()
    with pytest.raises(AuthRetentionError):
        runtime._publish_artifacts(
            local_paths(attempt), individual_files=(), directory=attempt
        )


@pytest.mark.parametrize("kind", ["proof", "binary", "ambiguous", "bounded"])
def test_unsafe_or_unprovable_native_outputs_never_silently_rewritten(tmp_path, kind):
    attempt = tmp_path / "attempt"
    job = attempt / "harbor-job"
    job.mkdir(parents=True)
    path = job / ("manifest.json" if kind == "proof" else "data.bin")
    data = (
        b"SYNTHETIC_ROTATED_ACCESS"
        if kind != "ambiguous"
        else b"previously unknown token"
    )
    if kind == "binary":
        data = b"\0" + data
    path.write_bytes(data)
    guard = registry()
    if kind == "bounded":
        guard.limits = ArtifactLimits(max_file_bytes=1)
    with pytest.raises(AuthRetentionError):
        guard.enforce(
            job,
            attempt_root=attempt,
            private_parent=tmp_path,
            refresh_unknown=kind == "ambiguous",
        )
    assert guard.quarantine_path is not None
    assert (guard.quarantine_path / path.name).read_bytes() == data
    assert outputs_blocked(attempt)


def test_literal_crossing_scan_chunk_boundary_is_detected(tmp_path):
    attempt = tmp_path / "attempt"
    job = attempt / "harbor-job"
    job.mkdir(parents=True)
    (job / "native.db").write_bytes(
        b"\0" + b"x" * (64 * 1024 - 8) + b"SYNTHETIC_ROTATED_ACCESS"
    )
    with pytest.raises(AuthRetentionError):
        registry().enforce(
            job, attempt_root=attempt, private_parent=tmp_path, refresh_unknown=False
        )


def test_api_key_modes_use_provider_metadata_not_a_second_provider_map(monkeypatch):
    from harbor.agents.model_connection import PROVIDERS, ProviderAccess

    monkeypatch.setitem(
        PROVIDERS, "synthetic-provider", ProviderAccess(("DECLARED_API_KEY",))
    )
    spec = AuthSpec(mode="api_key", reference=EnvAuthReference(name="USER_KEY"))
    for harness in ("opencode", "pi"):
        assert (
            credential_env_name(harness, "api_key", model="synthetic-provider/model")
            == "DECLARED_API_KEY"
        )
        validate_auth_spec(harness, spec, model="openrouter/openai/gpt-5")
    with pytest.raises(ValueError, match="provider"):
        validate_auth_spec("codex", spec, model="openrouter/openai/gpt-5")
    with pytest.raises(ValueError, match="single API-key"):
        credential_env_name("pi", "api_key", model="openai-codex/gpt-5")


def test_runtime_captures_both_generations_before_native_cleanup(tmp_path):
    scope, _, _ = new_scope(tmp_path)
    asyncio.run(native_trial(scope, FakeHarborEnvironment("codex")))
    job = scope.paths.root / "harbor-job"
    job.mkdir(parents=True)
    log = job / "native.log"
    log.write_text("SYNTHETIC_INITIAL SYNTHETIC_ROTATED SYNTHETIC_REFRESH")
    scope.finalize(require_consumer=True)
    assert "SYNTHETIC" not in log.read_text()


def test_shared_effective_validator_is_rechecked_before_handoff(tmp_path, monkeypatch):
    scope, _, _ = new_scope(tmp_path)
    environment = FakeHarborEnvironment("codex")

    def rejected_resource(harness):
        assert harness is scope.harness
        raise ValueError("sealed resource contains an auth override")

    monkeypatch.setattr(
        "tetrabench.harnesses.validate_explicit_auth_configuration", rejected_resource
    )
    with pytest.raises(ValueError, match="sealed resource"):
        asyncio.run(scope.new_hook(scope.harness).bind(environment))
    assert environment.files == {}
    scope.finalize()


def test_quarantine_move_failure_still_publishes_only_safe_failure(
    tmp_path, monkeypatch
):
    attempt = tmp_path / "attempt"
    job = attempt / "harbor-job"
    job.mkdir(parents=True)
    raw = b"\0SYNTHETIC_ROTATED_ACCESS"
    (job / "native.db").write_bytes(raw)

    def cannot_move(*args):
        raise OSError("synthetic cross-device failure")

    monkeypatch.setattr("tetrabench.auth_retention.shutil.move", cannot_move)
    with pytest.raises(AuthRetentionError):
        registry().enforce(
            job, attempt_root=attempt, private_parent=tmp_path, refresh_unknown=False
        )
    assert (job / "native.db").read_bytes() == raw
    published = []
    events = []

    class Store:
        def publish_content_stream(self, stream, *, media_type):
            data = stream.read()
            published.append(data)
            digest = hashlib.sha256(data).hexdigest()
            return ContentObject(
                sha256=digest,
                key="objects/sha256/" + digest,
                size=len(data),
                media_type=media_type,
            )

        def publish_event(self, event):
            events.append(event)

    runtime: Any = object.__new__(ControllerRuntime)
    runtime._store = Store()
    runtime._artifact_limits = ArtifactLimits()
    runtime._volume = SimpleNamespace(commit=lambda: None, reload=lambda: None)
    runtime._publish_failure_evidence(
        SimpleNamespace(run_id="run"),
        "attempt",
        local_paths(attempt),
        AuthRetentionError("blocked"),
        phase="harbor-execution",
    )
    assert len(published) == 1
    assert json.loads(published[0])["error_type"] == "AuthRetentionError"
    assert b"SYNTHETIC_ROTATED_ACCESS" not in published[0]
    assert len(events) == 1
