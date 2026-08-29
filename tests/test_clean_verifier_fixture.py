from __future__ import annotations

import http.client
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock, TrialLock
from harbor.models.job.result import JobResult
from harbor.models.task.config import NetworkMode, VerifierEnvironmentMode
from harbor.models.task.task import Task
from harbor.models.trial.artifact_manifest import ArtifactManifestEntry
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult

from tetrabench.integration import run_local_composition

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/harbor_authority_task"
FORGE_PATH = FIXTURE / "environment/forge.py"
VERIFY_PATH = FIXTURE / "tests/verify.py"
EXPECTED_INITIAL = FIXTURE / "tests/expected_initial.json"
CAPABILITY = "tetrabench-fixture-capability-7e9f9eeb2d74f043"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FORGE = _load_module("authority_forge", FORGE_PATH)
VERIFY = _load_module("authority_verify", VERIFY_PATH)


def _git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.stdout.strip()


def _valid_artifacts(tmp_path: Path) -> tuple[Path, Path, Path, Any]:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    shutil.copy2(FIXTURE / "environment/repo/app.py", workspace / "app.py")
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "config", "user.name", "Fixture")
    _git(workspace, "config", "user.email", "fixture@example.invalid")
    _git(workspace, "add", "app.py")
    commit_env = {
        "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00",
        "HOME": str(tmp_path),
        "PATH": __import__("os").environ["PATH"],
    }
    _git(workspace, "commit", "--message", "initial", env=commit_env)
    shutil.rmtree(workspace / ".git/hooks")
    assert _git(workspace, "rev-parse", "HEAD") == (
        "40cf7d08fd09619514bab16351e1e926fde8698c"
    )
    _git(workspace, "switch", "-c", "feature")
    (workspace / "app.py").write_text('print("hello, tetrabench")\n')
    _git(workspace, "add", "app.py")
    _git(
        workspace,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Oracle",
        "-c",
        "user.email=oracle@example.invalid",
        "commit",
        "--message",
        "update greeting",
    )
    shutil.rmtree(workspace / ".git/rr-cache", ignore_errors=True)
    (workspace / ".git/MERGE_RR").unlink(missing_ok=True)
    head_oid = _git(workspace, "rev-parse", "HEAD")
    database = tmp_path / "forge/state.sqlite3"
    store = FORGE.ForgeStore(
        database, FIXTURE / "environment/initial_state.json", CAPABILITY
    )
    common = {
        "base": "main",
        "head": "feature",
        "head_oid": head_oid,
        "schema_version": 1,
    }
    store.apply_transition(
        {
            **common,
            "request_id": "opened-0000000000000001",
            "type": "pull_request.opened",
        },
        CAPABILITY,
    )
    store.apply_transition(
        {
            **common,
            "request_id": "submitted-00000000000001",
            "type": "pull_request.submitted",
        },
        CAPABILITY,
    )
    export = tmp_path / "forge/export"
    store.export_sealed(export)
    marker = tmp_path / "baked-marker"
    marker.write_text("baked-verifier-source\n")
    return workspace, export, marker, store


def _artifact_contract(tmp_path: Path, workspace: Path, export: Path) -> Path:
    contract = tmp_path / "artifact-contract.json"
    contract.write_bytes(
        VERIFY.canonical(
            {
                "artifacts": [
                    {"kind": "git-worktree", "source": str(workspace)},
                    {"kind": "sealed-forge-export", "source": str(export)},
                ],
                "schema_version": 1,
            }
        )
        + b"\n"
    )
    return contract


def test_fixture_uses_pinned_native_separate_verifier_sidecar_contract() -> None:
    task = Task(FIXTURE)
    config = task.config
    assert config.verifier.environment_mode == VerifierEnvironmentMode.SEPARATE
    assert config.environment.network_mode == NetworkMode.PUBLIC
    assert config.verifier.environment is not None
    assert config.verifier.environment.network_mode == NetworkMode.NO_NETWORK
    assert config.environment.gpus is None
    assert config.verifier.environment.gpus is None
    assert [(hook.service, hook.timeout_sec) for hook in config.verifier.collect] == [
        ("forge", 20.0)
    ]
    assert [
        (artifact.source, artifact.destination, artifact.service)
        for artifact in config.artifacts
        if not isinstance(artifact, str)
    ] == [
        ("/workspace/repo", "main-workspace", "main"),
        ("/forge/export", "forge-export", "forge"),
    ]
    assert config.environment.cpus == 1
    assert config.environment.memory_mb == 512
    assert config.environment.storage_mb == 1024
    assert config.verifier.environment.cpus == 1
    assert config.verifier.environment.memory_mb == 384
    assert config.verifier.environment.storage_mb == 512


def test_clean_verifier_accepts_only_canonical_git_and_forge_artifacts(
    tmp_path: Path,
) -> None:
    workspace, export, marker, _store = _valid_artifacts(tmp_path)
    diagnostics = VERIFY.verify_submission(
        workspace,
        export,
        _artifact_contract(tmp_path, workspace, export),
        EXPECTED_INITIAL,
        marker,
    )
    assert diagnostics["checks"] == [
        "artifact",
        "forge",
        "git",
        "clean-clone",
        "product",
        "runtime",
    ]


def test_forge_rejects_invalid_current_state_and_every_post_seal_write(
    tmp_path: Path,
) -> None:
    workspace, _export, _marker, store = _valid_artifacts(tmp_path)
    transition = {
        "base": "main",
        "head": "feature",
        "head_oid": _git(workspace, "rev-parse", "HEAD"),
        "request_id": "opened-0000000000000002",
        "schema_version": 1,
        "type": "pull_request.opened",
    }
    with pytest.raises(FORGE.ForgeError, match="sealed"):
        store.apply_transition(transition, CAPABILITY)

    fresh = FORGE.ForgeStore(
        tmp_path / "fresh.sqlite3",
        FIXTURE / "environment/initial_state.json",
        CAPABILITY,
    )
    with pytest.raises(FORGE.ForgeError, match="head_oid"):
        fresh.apply_transition(
            {**transition, "head_oid": "agent-owned-result"}, CAPABILITY
        )
    with pytest.raises(FORGE.ForgeError, match="base"):
        fresh.apply_transition({**transition, "base": "other"}, CAPABILITY)

    FORGE.Handler.store = store
    server = FORGE.http.server.ThreadingHTTPServer(("127.0.0.1", 0), FORGE.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/transitions",
        data=FORGE.canonical(transition),
        headers={
            "Content-Type": "application/json",
            "X-Forge-Capability": CAPABILITY,
        },
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=2)
        assert captured.value.code == 409
        assert json.loads(captured.value.read()) == {
            "error": "forge is terminal and sealed"
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_collect_refuses_to_create_terminal_state(tmp_path: Path) -> None:
    store = FORGE.ForgeStore(
        tmp_path / "forge.sqlite3",
        FIXTURE / "environment/initial_state.json",
        CAPABILITY,
    )
    with pytest.raises(FORGE.ForgeError, match="not finalized"):
        store.export_sealed(tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_replayed_request_id_is_rejected_before_finalization(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    workspace, _export, _marker, _store = _valid_artifacts(seed)
    store = FORGE.ForgeStore(
        tmp_path / "replay.sqlite3",
        FIXTURE / "environment/initial_state.json",
        CAPABILITY,
    )
    common = {
        "base": "main",
        "head": "feature",
        "head_oid": _git(workspace, "rev-parse", "HEAD"),
        "request_id": "same-request-000000000001",
        "schema_version": 1,
    }
    store.apply_transition({**common, "type": "pull_request.opened"}, CAPABILITY)
    with pytest.raises(FORGE.ForgeError, match="replayed"):
        store.apply_transition({**common, "type": "pull_request.submitted"}, CAPABILITY)
    with pytest.raises(FORGE.ForgeError, match="not finalized"):
        store.export_sealed(tmp_path / "export")


def test_final_transition_atomically_seals_revokes_and_serializes_racers(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    workspace, _export, _marker, _sealed = _valid_artifacts(seed)
    store = FORGE.ForgeStore(
        tmp_path / "race.sqlite3",
        FIXTURE / "environment/initial_state.json",
        CAPABILITY,
    )
    common = {
        "base": "main",
        "head": "feature",
        "head_oid": _git(workspace, "rev-parse", "HEAD"),
        "schema_version": 1,
    }
    store.apply_transition(
        {
            **common,
            "request_id": "opened-race-000000000001",
            "type": "pull_request.opened",
        },
        CAPABILITY,
    )
    outcomes: list[str] = []

    def finalize(index: int) -> None:
        try:
            store.apply_transition(
                {
                    **common,
                    "request_id": f"submitted-race-{index:016d}",
                    "type": "pull_request.submitted",
                },
                CAPABILITY,
            )
            outcomes.append("sealed")
        except FORGE.ForgeError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=finalize, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("sealed") == 1
    assert outcomes.count("forge is terminal and sealed") == 7
    with sqlite3.connect(tmp_path / "race.sqlite3") as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        assert metadata["terminal"] == b"sealed"
        assert "capability" not in metadata
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (2,)


def test_forge_http_surface_rejects_ambiguous_or_noncanonical_requests(
    tmp_path: Path,
) -> None:
    store = FORGE.ForgeStore(
        tmp_path / "http.sqlite3",
        FIXTURE / "environment/initial_state.json",
        CAPABILITY,
    )
    FORGE.Handler.store = store
    server = FORGE.http.server.ThreadingHTTPServer(("127.0.0.1", 0), FORGE.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cases = [
        ("PUT", "/transitions", "application/json", b"{}", 405),
        ("POST", "/other", "application/json", b"{}", 404),
        ("POST", "/transitions", "text/plain", b"{}", 400),
        ("POST", "/transitions", "application/json", b'{"x":1, "x":2}', 400),
        ("POST", "/transitions", "application/json", b'{"x":NaN}', 400),
        ("POST", "/transitions", "application/json", b"{}\n", 400),
        ("POST", "/transitions", "application/json", b"x" * 4097, 400),
    ]
    try:
        for method, path, content_type, body, expected in cases:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            connection.request(
                method,
                path,
                body=body,
                headers={
                    "Content-Type": content_type,
                    "X-Forge-Capability": CAPABILITY,
                },
            )
            response = connection.getresponse()
            response.read()
            assert response.status == expected
            connection.close()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=2
        )
        connection.putrequest("POST", "/transitions", skip_accept_encoding=True)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "2")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.putheader("X-Forge-Capability", CAPABILITY)
        connection.endheaders(b"2\r\n{}\r\n0\r\n\r\n")
        response = connection.getresponse()
        response.read()
        assert response.status == 400
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_agent_owned_forge_event_file_cannot_pass(tmp_path: Path) -> None:
    workspace, export, marker, _store = _valid_artifacts(tmp_path)
    (workspace / "events.jsonl").write_text('{"event_hash":"forged"}\n')
    with pytest.raises(ValueError, match="forbidden agent file"):
        VERIFY.verify_submission(
            workspace,
            export,
            _artifact_contract(tmp_path, workspace, export),
            EXPECTED_INITIAL,
            marker,
        )


def test_modified_forge_snapshot_and_recomputed_manifest_cannot_pass(
    tmp_path: Path,
) -> None:
    workspace, export, marker, _store = _valid_artifacts(tmp_path)
    snapshot_path = export / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["head_hash"] = "0" * 64
    snapshot_bytes = VERIFY.canonical(snapshot) + b"\n"
    snapshot_path.write_bytes(snapshot_bytes)
    manifest_path = export / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["snapshot.json"] = VERIFY.digest(snapshot_bytes)
    manifest_path.write_bytes(VERIFY.canonical(manifest) + b"\n")
    with pytest.raises(ValueError, match="snapshot head hash"):
        VERIFY.verify_submission(
            workspace,
            export,
            _artifact_contract(tmp_path, workspace, export),
            EXPECTED_INITIAL,
            marker,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        b'{"schema_version":1,"schema_version":1}\n',
        b'{"schema_version":NaN}\n',
        b'{"schema_version":true}\n',
        b'{ "schema_version":1}\n',
        b'{"extra":1,"schema_version":1}\n',
    ],
)
def test_strict_forge_json_rejects_adversarial_manifest_bytes(
    tmp_path: Path, mutation: bytes
) -> None:
    workspace, export, marker, _store = _valid_artifacts(tmp_path)
    (export / "manifest.json").write_bytes(mutation)
    with pytest.raises(ValueError):
        VERIFY.verify_submission(
            workspace,
            export,
            _artifact_contract(tmp_path, workspace, export),
            EXPECTED_INITIAL,
            marker,
        )


def test_recomputed_event_hash_cannot_hide_boolean_sequence(tmp_path: Path) -> None:
    workspace, export, marker, _store = _valid_artifacts(tmp_path)
    events_path = export / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_bytes().splitlines()]
    events[0]["sequence"] = True
    previous = VERIFY.digest(VERIFY.canonical(json.loads(EXPECTED_INITIAL.read_text())))
    for event in events:
        event["prev_hash"] = previous
        core = {
            "prev_hash": event["prev_hash"],
            "sequence": event["sequence"],
            "transition": event["transition"],
        }
        event["event_hash"] = VERIFY.digest(VERIFY.canonical(core))
        previous = event["event_hash"]
    event_bytes = b"".join(VERIFY.canonical(event) + b"\n" for event in events)
    events_path.write_bytes(event_bytes)
    snapshot_path = export / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["head_hash"] = previous
    snapshot_bytes = VERIFY.canonical(snapshot) + b"\n"
    snapshot_path.write_bytes(snapshot_bytes)
    manifest = {
        "files": {
            "events.jsonl": VERIFY.digest(event_bytes),
            "snapshot.json": VERIFY.digest(snapshot_bytes),
        },
        "schema_version": 1,
    }
    (export / "manifest.json").write_bytes(VERIFY.canonical(manifest) + b"\n")
    with pytest.raises(ValueError, match="event chain"):
        VERIFY.verify_submission(
            workspace,
            export,
            _artifact_contract(tmp_path, workspace, export),
            EXPECTED_INITIAL,
            marker,
        )


@pytest.mark.parametrize(
    "mechanism",
    ["hook", "fsmonitor", "alias", "filter", "alternates", "replace", "shallow"],
)
def test_git_control_plane_exploits_are_rejected_without_execution(
    tmp_path: Path, mechanism: str
) -> None:
    workspace, export, marker, _store = _valid_artifacts(tmp_path)
    marker_file = tmp_path / f"executed-{mechanism}"
    payload = tmp_path / "payload.sh"
    payload.write_text(f"#!/bin/sh\ntouch {marker_file}\n")
    payload.chmod(0o755)
    git_dir = workspace / ".git"
    if mechanism == "hook":
        hooks = git_dir / "hooks"
        hooks.mkdir()
        shutil.copy2(payload, hooks / "post-checkout")
    elif mechanism in {"fsmonitor", "alias", "filter"}:
        section, key = {
            "fsmonitor": ("core", "fsmonitor"),
            "alias": ("alias", "fsck"),
            "filter": ("filter exploit", "clean"),
        }[mechanism]
        with (git_dir / "config").open("a") as stream:
            stream.write(f"\n[{section}]\n\t{key} = {payload}\n")
    elif mechanism == "alternates":
        path = git_dir / "objects/info/alternates"
        path.write_text(str(tmp_path / "objects") + "\n")
    elif mechanism == "replace":
        replace = git_dir / "refs/replace"
        replace.mkdir()
        (replace / ("0" * 40)).write_text("0" * 40 + "\n")
    else:
        (git_dir / "shallow").write_text("0" * 40 + "\n")
    with pytest.raises(ValueError):
        VERIFY.verify_submission(
            workspace,
            export,
            _artifact_contract(tmp_path, workspace, export),
            EXPECTED_INITIAL,
            marker,
        )
    assert not marker_file.exists()


@pytest.mark.parametrize("missing", ["workspace", "forge"])
def test_missing_declared_artifact_cannot_pass(tmp_path: Path, missing: str) -> None:
    workspace, export, marker, _store = _valid_artifacts(tmp_path)
    shutil.rmtree(workspace if missing == "workspace" else export)
    with pytest.raises((FileNotFoundError, ValueError), match=r"missing|unsafe"):
        VERIFY.verify_submission(
            workspace,
            export,
            _artifact_contract(tmp_path, workspace, export),
            EXPECTED_INITIAL,
            marker,
        )


def test_same_environment_tests_assumption_cannot_pass(tmp_path: Path) -> None:
    workspace, export, _marker, _store = _valid_artifacts(tmp_path)
    with pytest.raises(FileNotFoundError):
        VERIFY.verify_submission(
            workspace,
            export,
            _artifact_contract(tmp_path, workspace, export),
            EXPECTED_INITIAL,
            tmp_path / "shared-environment-has-no-baked-marker",
        )


def _docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
def test_real_harbor_clean_verifier_forge_sidecar_end_to_end(
    tmp_path: Path,
) -> None:
    assert _docker_available(), "Docker daemon is required for the test suite"
    fixture_result = run_local_composition(FIXTURE, tmp_path / "run")
    assert fixture_result.terminal.outcome == "succeeded"
    run = fixture_result.invocation_root / "jobs/harbor-job"
    JobConfig.model_validate_json((run / "config.json").read_text())
    JobLock.model_validate_json((run / "lock.json").read_text())
    JobResult.model_validate_json((run / "result.json").read_text())
    trial_directories = [path for path in run.iterdir() if path.is_dir()]
    assert len(trial_directories) == 1
    trial = trial_directories[0]
    TrialConfig.model_validate_json((trial / "config.json").read_text())
    TrialLock.model_validate_json((trial / "lock.json").read_text())
    TrialResult.model_validate_json((trial / "result.json").read_text())
    result = json.loads((trial / "result.json").read_text())
    assert result["verifier_environment_mode"] == "separate"
    assert result["verifier_result"]["rewards"] == {"reward": 1.0}
    assert (trial / "verifier/reward.txt").read_bytes() == b"1\n"
    diagnostics = json.loads((trial / "verifier/diagnostics.json").read_text())
    assert diagnostics["ok"] is True
    runtime = diagnostics["runtime"]
    assert runtime["orchestrator"] == {"gid": 0, "uid": 0}
    assert runtime["runner"] == {"gid": 65532, "uid": 65532}
    assert runtime["cgroup"] == {
        "cpu_max": "100000 100000",
        "memory_max": str(384 * 1024 * 1024),
        "pid_guarantee": "none",
    }
    assert all(probe["blocked"] is True for probe in runtime["network_probes"])
    assert runtime["mounts"]["docker_socket"] is False
    assert runtime["mounts"]["task_side_volumes"] is False
    manifest_data = json.loads((trial / "artifacts/manifest.json").read_text())
    entries = [ArtifactManifestEntry.model_validate(item) for item in manifest_data]
    by_source = {(entry.source, entry.service): entry for entry in entries}
    assert by_source[("/workspace/repo", "main")].status == "ok"
    assert by_source[("/forge/export", "forge")].status == "ok"
    assert (trial / "artifacts/main-workspace/.git").is_dir()
    assert (trial / "artifacts/forge-export/manifest.json").is_file()
    assert "agent-tests-absent" in (trial / "agent/oracle.txt").read_text()
