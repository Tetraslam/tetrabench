from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
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
from tetrabench.plan import parse_canonical_model
from tetrabench.rewards import ControllerResultV2

ROOT = Path(__file__).parents[1]
TASK = ROOT / "benchmarks/tasks/systems-design/authority-fencing"


def _load_source_module(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def test_authority_fencing_contract_and_hidden_cases_are_frozen() -> None:
    contract = tomllib.loads((TASK / "contract.toml").read_text())
    hidden_contract = (TASK / "tests/contract.toml").read_bytes()
    assert hidden_contract == (TASK / "contract.toml").read_bytes()
    for name in ("README.md", "authority.py", "contract.toml", "test_public.py"):
        assert (TASK / f"environment/seed/{name}").read_bytes() == (
            TASK / name
        ).read_bytes()
    assert contract["task_id"] == "systems-design/authority-fencing"
    assert contract["schema_version"] == 1
    manifest = [
        {
            "path": name,
            "sha256": hashlib.sha256((TASK / name).read_bytes()).hexdigest(),
        }
        for name in ("README.md", "authority.py", "test_public.py")
    ]
    assert (
        hashlib.sha256(canonical(manifest)).hexdigest()
        == contract["initial_workspace_sha256"]
    )
    assert contract["initial_workspace_files"] == [
        "README.md",
        "authority.py",
        "test_public.py",
    ]
    assert contract["initial_workspace_manifest"] == manifest
    cases = tomllib.loads((TASK / "tests/cases.toml").read_text())
    assert len(cases["cases"]) <= 16
    assert len(cases["fault_schedules"]) <= 8
    assert len({item["id"] for item in cases["cases"]}) == len(cases["cases"])
    assert all(
        "PLACEHOLDER" not in value
        for value in (TASK / "tests/cases.toml").read_text().splitlines()
    )
    case_inputs = []
    for item in cases["cases"]:
        value = {
            "gate_ids": item["gate_ids"],
            "id": item["id"],
            "scenario": item["scenario"],
            "seed": item["seed"],
        }
        if "fault_schedule_id" in item:
            value["fault_schedule_id"] = item["fault_schedule_id"]
        case_inputs.append(value)
    hidden_input = {
        "cases": case_inputs,
        "fault_schedules": cases["fault_schedules"],
        "schema_version": cases["schema_version"],
        "task_id": cases["task_id"],
    }
    hidden_digest = hashlib.sha256(canonical(hidden_input)).hexdigest()
    assert hidden_digest == cases["input_manifest_sha256"]
    assert hidden_digest == contract["hidden_case_input_sha256"]
    assert "authority-fencing" not in (ROOT / "benchmarks/catalog.toml").read_text()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'id = "claim-precommit-exit"',
            "id = 7",
            "fault schedule schema mismatch",
        ),
        ('checkpoint_id = "claim.precommit"\n', "", "fault schedule schema mismatch"),
        (
            "exit_code = 86",
            'exit_code = "86"',
            "fault schedule schema mismatch",
        ),
        (
            "exit_code = 86",
            "exit_code = 86\nsettle_ticks = 1",
            "fault schedule schema mismatch",
        ),
        (
            "exit_code = 86",
            "exit_code = 87",
            "fault schedule differs from public fault contract",
        ),
        (
            'checkpoint_id = "claim.precommit"',
            'checkpoint_id = "transition.precommit"',
            "claim fault schedule checkpoint mismatch",
        ),
    ],
)
def test_fault_schedules_are_closed_and_public_contract_authoritative(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    verifier = _load_source_module(
        "authority_verify_fault_schema", TASK / "tests/verify.py"
    )
    original_digest = tomllib.loads((TASK / "tests/cases.toml").read_text())[
        "input_manifest_sha256"
    ]
    cases_text = (TASK / "tests/cases.toml").read_text().replace(old, new, 1)
    cases = tomllib.loads(cases_text)
    changed_digest = verifier.case_input_digest(cases)
    cases_text = cases_text.replace(original_digest, changed_digest, 1)
    contract_text = (
        (TASK / "contract.toml").read_text().replace(original_digest, changed_digest, 1)
    )
    contract_path = tmp_path / "contract.toml"
    cases_path = tmp_path / "cases.toml"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract_path.write_text(contract_text)
    cases_path.write_text(cases_text)
    (workspace / "contract.toml").write_text(contract_text)
    with pytest.raises(ValueError, match=message):
        verifier.validate_manifests(contract_path, cases_path, workspace)


def test_seed_211693_has_one_ordered_argument_schedule() -> None:
    model = _load_source_module("authority_model_seed_211693", TASK / "tests/model.py")
    schedule_module = sys.modules["schedule"]
    manifest = tomllib.loads((TASK / "tests/cases.toml").read_text())
    schedules = {
        item["id"]: schedule_module.FaultSchedule(**item)
        for item in manifest["fault_schedules"]
    }
    case = next(item for item in manifest["cases"] if item["seed"] == 211693)
    schedule = schedule_module.derive_schedule(case, 4611686018427387903, schedules)
    assert schedule.invalid_ttls == (-6, 0)
    assert schedule.action_order == ("fail", "renew", "complete")
    _state, effects = model.execute_scenario(case, 0, 4611686018427387903, schedules)
    renew_ttls = [
        item["arguments"]["ttl"]
        for item in effects
        if item["operation"] == "renew" and item["exit_code"] == 2
    ]
    assert renew_ttls == [-6, 0]
    assert model.digest(effects) != model.digest(list(reversed(effects)))


def test_concurrent_claim_requires_durable_state_to_name_actual_winner() -> None:
    verifier = _load_source_module(
        "authority_verify_concurrent_identity", TASK / "tests/verify.py"
    )
    state = {"fence_token": 2, "worker_id": "worker-b"}
    with pytest.raises(ValueError, match="durable winner mismatch"):
        verifier.verify_concurrent_claim_outcome(
            [("worker-a", 0), ("worker-b", 2)],
            state,
            ("worker-a", "worker-b"),
        )


def test_concurrent_claim_evidence_rejects_unseeded_and_nonexclusive_results() -> None:
    verifier = _load_source_module(
        "authority_verify_concurrent_schedule", TASK / "tests/verify.py"
    )
    state = {"fence_token": 2, "worker_id": "worker-a"}
    with pytest.raises(ValueError, match="seeded schedule"):
        verifier.verify_concurrent_claim_outcome(
            [("worker-a", 0), ("worker-c", 2)],
            state,
            ("worker-a", "worker-b"),
        )
    with pytest.raises(ValueError, match="one winner"):
        verifier.verify_concurrent_claim_outcome(
            [("worker-a", 0), ("worker-b", 0)],
            state,
            ("worker-a", "worker-b"),
        )


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"winner_worker_id": "worker-a"}, "evidence mismatch"),
        (
            {
                "durable_owner_token_match": False,
                "loser_worker_id": "worker-b",
                "winner_worker_id": "worker-a",
                "winning_fence_token": 2,
            },
            "evidence mismatch",
        ),
        (None, "omit concurrent claim evidence"),
    ],
)
def test_admission_rejects_tampered_concurrent_claim_evidence(
    replacement: dict[str, Any] | None, message: str
) -> None:
    admission = _load_source_module(
        "authority_admission_concurrent_evidence",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    diagnostics = {} if replacement is None else {"concurrent_claim": replacement}
    with pytest.raises(ValueError, match=message):
        admission.concurrent_claim_evidence(diagnostics, required=True)


def test_build_context_manifest_binds_empty_directories_and_modes(
    tmp_path: Path,
) -> None:
    admission = _load_source_module(
        "authority_admission_tree_manifest",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    context = tmp_path / "context"
    context.mkdir(mode=0o755)
    source = context / "verify.py"
    source.write_text("pass\n")
    source.chmod(0o644)
    initial = admission.tree_manifest(context)
    assert initial == [
        {"mode": 0o755, "path": ".", "type": "directory"},
        {
            "mode": 0o644,
            "path": "verify.py",
            "sha256": hashlib.sha256(b"pass\n").hexdigest(),
            "size": 5,
            "type": "file",
        },
    ]
    initial_digest = admission.manifest_digest(initial)

    empty = context / "empty"
    empty.mkdir(mode=0o755)
    with_empty = admission.tree_digest(context)
    assert with_empty != initial_digest
    empty.chmod(0o700)
    with_directory_mode = admission.tree_digest(context)
    assert with_directory_mode != with_empty
    source.chmod(0o755)
    assert admission.tree_digest(context) != with_directory_mode


def _git(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_admission_subject_binds_sources_and_reports_untracked_candidate_dirty(
    tmp_path: Path,
) -> None:
    admission = _load_source_module(
        "authority_admission_source_manifest",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    task = tmp_path / "candidate"
    task.mkdir()
    task_file = task / "task.toml"
    task_file.write_text("value = 1\n")
    tool = tmp_path / "admission.py"
    tool.write_text("TOOL = 1\n")
    helper = tmp_path / "evidence.py"
    helper.write_text("HELPER = 1\n")
    paths = (task, tool, helper)
    _git(tmp_path, "init")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=tetrabench test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "fixture",
    )
    manifest = admission.source_manifest(paths, root=tmp_path)
    revision, state = admission.source_git_state(manifest, root=tmp_path)
    assert revision == _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert state == "clean"

    mutants = tomllib.loads((TASK / "tests/mutants.toml").read_text())["mutants"]

    def subject() -> dict[str, Any]:
        return admission.admission_subject(
            hidden_case_input_sha256="0" * 64,
            mutants=mutants,
            tool_version_values={"test": "1"},
            verifier_context_sha256=admission.tests_context_digest(),
            source_paths=paths,
            source_root=tmp_path,
        )

    clean_subject = subject()
    assert clean_subject["source_revision"] == revision
    assert clean_subject["source_state"] == "clean"
    task.chmod(0o700)
    assert subject()["source_state"] == "dirty"
    task.chmod(0o755)
    tool.write_text("TOOL = 2\n")
    tool_subject = subject()
    assert (
        tool_subject["source_manifest_sha256"]
        != clean_subject["source_manifest_sha256"]
    )
    assert tool_subject["source_revision"] is None
    assert tool_subject["source_state"] == "dirty"
    tool.write_text("TOOL = 1\n")
    task_file.write_text("value = 2\n")
    task_subject = subject()
    assert (
        task_subject["source_manifest_sha256"]
        != clean_subject["source_manifest_sha256"]
    )

    task_file.write_text("value = 1\n")
    untracked = task / "new.py"
    untracked.write_text("DIRTY = True\n")
    dirty_subject = subject()
    assert dirty_subject["source_revision"] is None
    assert dirty_subject["source_state"] == "dirty"
    assert any(
        item["path"] == "candidate/new.py" for item in dirty_subject["source_manifest"]
    )


def test_docker_marker_accounting_fails_when_a_required_marker_is_missing() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            str(Path(__file__)),
        ],
        cwd=ROOT,
        env={**os.environ, "TETRABENCH_EXPECT_DOCKER_TESTS": "3"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 4
    assert "expected 3 Docker tests, collected 2" in result.stderr


def test_authority_fencing_native_task_boundary() -> None:
    task = Task(TASK)
    assert task.config.environment.network_mode == NetworkMode.PUBLIC
    assert task.config.environment.gpus is None
    assert task.config.verifier.environment_mode == VerifierEnvironmentMode.SEPARATE
    assert task.config.verifier.environment is not None
    assert task.config.verifier.environment.network_mode == NetworkMode.NO_NETWORK
    artifacts = [item for item in task.config.artifacts if not isinstance(item, str)]
    assert [(item.source, item.destination, item.service) for item in artifacts] == [
        ("/workspace", "workspace", "main")
    ]


def test_public_skeleton_passes_only_its_happy_path() -> None:
    subprocess.run(
        ["python", str(TASK / "test_public.py")],
        cwd=TASK,
        check=True,
        timeout=15,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import os\nwhile True: os.write(1, b'x' * 16384)\n",
        "import os,time\n"
        "os.write(1,b'a'*40000);os.write(2,b'b'*40000);time.sleep(30)\n",
    ],
)
def test_verifier_rejects_unbounded_output_promptly(source: str) -> None:
    verifier = _load_source_module("authority_verify_output", TASK / "tests/verify.py")
    process = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    with pytest.raises(ValueError, match="output exceeded limit"):
        verifier.bounded_process_output(process, timeout=5)
    assert time.monotonic() - started < 2
    assert process.poll() is not None


def test_verifier_kills_descendant_pipe_writer(tmp_path: Path) -> None:
    verifier = _load_source_module(
        "authority_verify_descendant", TASK / "tests/verify.py"
    )
    pid_path = tmp_path / "descendant.pid"
    source = (
        "import os,time\n"
        "pid=os.fork()\n"
        "if pid:\n"
        " raise SystemExit(0)\n"
        f"open({str(pid_path)!r},'w').write(str(os.getpid()))\n"
        "while True: os.write(1,b'x'*16384)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    with pytest.raises(ValueError, match="output exceeded limit"):
        verifier.bounded_process_output(process, timeout=5)
    assert time.monotonic() - started < 2
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_path.exists()
    descendant = int(pid_path.read_text())
    while Path(f"/proc/{descendant}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{descendant}").exists()


def test_submission_root_rejects_many_entries_in_constant_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_source_module("authority_verify_tree", TASK / "tests/verify.py")
    for index in range(5000):
        (tmp_path / f"unknown-{index:05d}").mkdir()
    real_scandir = verifier.os.scandir
    consumed = 0

    class CountingIterator:
        def __init__(self, path: Path):
            self.iterator = real_scandir(path)

        def __iter__(self):  # type: ignore[no-untyped-def]
            return self

        def __next__(self):  # type: ignore[no-untyped-def]
            nonlocal consumed
            consumed += 1
            return next(self.iterator)

    monkeypatch.setattr(verifier.os, "scandir", CountingIterator)
    started = time.monotonic()
    with pytest.raises(ValueError, match="unknown root entry"):
        verifier.validate_tree(tmp_path)
    assert consumed == 1
    assert time.monotonic() - started < 1


def test_debug_image_cannot_emit_admissible_proof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admission = _load_source_module(
        "authority_admission_debug", ROOT / "tools/run_authority_fencing_admission.py"
    )
    mutants = tomllib.loads((TASK / "tests/mutants.toml").read_text())["mutants"]

    def fake_verifier(_image: str, _workspace: Path, output: Path):
        index = int(output.parent.name.removeprefix("case-"))
        gates = {gate: 0 for gate in admission.GATES}
        reward = int(index == 0)
        if index == 0:
            gates = dict.fromkeys(admission.GATES, 1)
        elif 2 <= index < 2 + len(mutants):
            intended = mutants[index - 2]["gate_id"]
            gates = {gate: int(gate != intended) for gate in admission.GATES}
        return reward, {
            "concurrent_claim": {
                "durable_owner_token_match": True,
                "loser_worker_id": "worker-b",
                "winner_worker_id": "worker-a",
                "winning_fence_token": 2,
            },
            "gates": gates,
        }

    monkeypatch.setattr(admission, "run_verifier", fake_verifier)
    monkeypatch.setattr(admission, "tool_versions", lambda: {"fake": "1"})
    result = admission.main(
        ["--debug", "--skip-build", "--image", "attacker-tailored:latest"]
    )
    evidence = json.loads(capsys.readouterr().out)
    assert result == 1
    assert evidence["matrix_ok"] is True
    assert evidence["admissible"] is False
    assert evidence["ok"] is False
    assert evidence["run_attestation"]["image_id"] is None


def test_caller_image_requires_debug_mode() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_authority_fencing_admission.py"),
            "--image",
            "attacker-tailored:latest",
            "--skip-build",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "require explicit --debug mode" in result.stderr


@pytest.mark.docker
def test_authority_fencing_admission_and_mutant_attribution(tmp_path: Path) -> None:
    assert _docker_available(), "Docker daemon is required for the test suite"
    result = subprocess.run(
        ["python", str(ROOT / "tools/run_authority_fencing_admission.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    evidence = json.loads(result.stdout)
    assert evidence["ok"] is True
    assert evidence["admissible"] is True
    assert evidence["debug"] is False
    assert evidence["matrix_ok"] is True
    first_attestation = evidence["run_attestation"]
    assert first_attestation["image_id"].startswith("sha256:")
    assert first_attestation["mode"] == "fresh-no-cache-build"
    assert "<temporary-iidfile>" in first_attestation["build_command"]
    assert "<verifier-context>" in first_attestation["build_command"]
    assert not any(str(tmp_path) in item for item in first_attestation["build_command"])
    assert evidence["subject"]["tool_versions"]["harbor"] == "0.22.0"
    assert len(evidence["subject"]["verifier_context_sha256"]) == 64
    assert (
        evidence["subject_sha256"]
        == hashlib.sha256(canonical(evidence["subject"])).hexdigest()
    )
    assert evidence["candidate_count"] == 17
    gold = next(entry for entry in evidence["entries"] if entry["name"] == "gold")
    assert gold["concurrent_claim"]["winner_worker_id"].startswith("worker-")
    assert gold["concurrent_claim"]["loser_worker_id"].startswith("worker-")
    assert (
        gold["concurrent_claim"]["winner_worker_id"]
        != gold["concurrent_claim"]["loser_worker_id"]
    )
    assert gold["concurrent_claim"]["winning_fence_token"] == 2
    assert gold["concurrent_claim"]["durable_owner_token_match"] is True
    assert evidence["concurrent_claim"] == gold["concurrent_claim"]
    for entry in evidence["entries"]:
        intended = entry["intended_gate"]
        if intended is None:
            continue
        expected = {
            gate: int(gate != intended)
            for gate in (
                "single-authority",
                "monotonic-fence",
                "stale-rejection",
                "restart-durability",
                "transaction-rollback",
                "terminal-idempotence",
            )
        }
        if entry["name"] == "attribution-probe-broad-mutant":
            assert entry["attribution_admitted"] is False
            assert entry["gate_vector"] != expected
        else:
            assert entry["attribution_admitted"] is True
            assert entry["gate_vector"] == expected

    admission = _load_source_module(
        "authority_admission_second_fresh_build",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    context = tmp_path / "verifier-context"
    shutil.copytree(
        TASK / "tests",
        context,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    context_sha256 = admission.tests_context_digest(context)
    _image, second_attestation = admission.build_admission_image(
        context, context_sha256
    )
    mutants = tomllib.loads((TASK / "tests/mutants.toml").read_text())["mutants"]
    second_subject = admission.admission_subject(
        hidden_case_input_sha256=evidence["hidden_case_input_sha256"],
        mutants=mutants,
        tool_version_values=evidence["subject"]["tool_versions"],
        verifier_context_sha256=context_sha256,
    )
    assert (
        hashlib.sha256(canonical(second_subject)).hexdigest()
        == evidence["subject_sha256"]
    )
    assert second_attestation != first_attestation
    assert second_attestation["build_nonce"] != first_attestation["build_nonce"]
    assert second_attestation["image_id"] != first_attestation["image_id"]


def _docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
def test_real_harbor_authority_fencing_oracle(tmp_path: Path) -> None:
    assert _docker_available(), "Docker daemon is required for the test suite"
    result = run_local_composition(TASK, tmp_path / "run", reward_policy="binary")
    assert result.terminal.outcome == "succeeded"
    run = result.invocation_root / "jobs/harbor-job"
    JobConfig.model_validate_json((run / "config.json").read_text())
    JobLock.model_validate_json((run / "lock.json").read_text())
    JobResult.model_validate_json((run / "result.json").read_text())
    trial = next(path for path in run.iterdir() if path.is_dir())
    TrialConfig.model_validate_json((trial / "config.json").read_text())
    TrialLock.model_validate_json((trial / "lock.json").read_text())
    native = TrialResult.model_validate_json((trial / "result.json").read_text())
    assert native.verifier_result is not None
    assert native.verifier_result.rewards == {"reward": 1}
    assert (trial / "verifier/reward.json").read_bytes() == b'{"reward":1}\n'
    diagnostics = json.loads((trial / "verifier/diagnostics.json").read_text())
    assert diagnostics["ok"] is True
    assert diagnostics["mandatory_gate_pass_count"] == 6
    concurrent_claim = diagnostics["concurrent_claim"]
    assert concurrent_claim["winner_worker_id"].startswith("worker-")
    assert concurrent_claim["loser_worker_id"].startswith("worker-")
    assert concurrent_claim["winner_worker_id"] != concurrent_claim["loser_worker_id"]
    assert concurrent_claim["winning_fence_token"] == 2
    assert concurrent_claim["durable_owner_token_match"] is True
    assert diagnostics["runtime"]["orchestrator"] == {"gid": 0, "uid": 0}
    assert diagnostics["runtime"]["runner"] == {"gid": 65532, "uid": 65532}
    probes = {item["kind"]: item for item in diagnostics["runtime"]["network_probes"]}
    assert probes["direct-ip-tcp"]["blocked"] is True
    assert probes["hostname-tcp"]["blocked"] is True
    manifest = json.loads((trial / "artifacts/manifest.json").read_text())
    entries = [ArtifactManifestEntry.model_validate(item) for item in manifest]
    workspace = next(item for item in entries if item.source == "/workspace")
    assert workspace.destination == "artifacts/workspace"
    assert workspace.service == "main"
    assert workspace.status == "ok"
    controller_entry = next(
        item
        for item in result.terminal.artifacts
        if item.logical_path.endswith("controller-result.json")
    )
    controller = parse_canonical_model(
        result.published_content[controller_entry.content.sha256], ControllerResultV2
    )
    assert controller.summary.policy == "binary"
    assert controller.summary.aggregate_kind == "binary_pass_rate"
    assert controller.summary.aggregate == "1"
    assert controller.summary.pass_count == 1
    assert controller.summary.sample_count == 1
