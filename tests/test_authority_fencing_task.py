from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
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
    sys.modules[name] = module
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


def test_clean_source_snapshot_archives_one_tracked_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_clean_snapshot",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "candidate").mkdir()
    (repository / "candidate/task.py").write_text("VALUE = 1\n")
    (repository / "tool.py").write_text("TOOL = 1\n")
    _git(repository, "init")
    _git(repository, "add", ".")
    _git(
        repository,
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
    monkeypatch.setattr(
        admission,
        "SOURCE_RELATIVE_PATHS",
        (Path("candidate"), Path("tool.py")),
    )
    private = tmp_path / "private"
    private.mkdir()
    snapshot = admission.create_clean_source_snapshot(private, repository=repository)
    assert snapshot.revision == _git(repository, "rev-parse", "HEAD").stdout.strip()
    assert snapshot.source_state == "clean"
    assert snapshot.archive_sha256 is not None
    assert (snapshot.root / "candidate/task.py").read_text() == "VALUE = 1\n"

    (repository / "tool.py").write_text("TOOL = 2\n")
    second_private = tmp_path / "second-private"
    second_private.mkdir()
    with pytest.raises(ValueError, match="clean Git worktree and index"):
        admission.create_clean_source_snapshot(second_private, repository=repository)


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
        env={**os.environ, "TETRABENCH_EXPECT_DOCKER_TESTS": "4"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 4
    assert "expected 4 Docker tests, collected 3" in result.stderr


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


@pytest.mark.parametrize(
    ("count", "accepted"),
    [(0, False), (1, True), (2, True), (3, True), (4, False)],
)
def test_proof_repetition_parser_exact_counts(count: int, accepted: bool) -> None:
    admission = _load_source_module(
        f"authority_admission_parser_{count}",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    arguments = ["--proof-runs", str(count)]
    if accepted:
        assert admission.parse_arguments(arguments).proof_runs == count
    else:
        with pytest.raises(SystemExit) as error:
            admission.parse_arguments(arguments)
        assert error.value.code == 2


def test_debug_image_cannot_write_proof_output(tmp_path: Path) -> None:
    output = tmp_path / "proof.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_authority_fencing_admission.py"),
            "--debug",
            "--skip-build",
            "--proof-runs",
            "1",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "--output requires exactly 3 proof runs" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("count", [1, 2])
def test_diagnostic_run_count_cannot_write_proof_output(
    tmp_path: Path, count: int
) -> None:
    output = tmp_path / "proof.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_authority_fencing_admission.py"),
            "--proof-runs",
            str(count),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "--output requires exactly 3 proof runs" in result.stderr
    assert not output.exists()


def test_proof_output_is_exclusive_private_and_refuses_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_proof_output",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    output = Path("proof.json")
    authority = admission.open_proof_output_authority(output)
    admission.write_exclusive_proof(authority, b'{"ok":true}\n')
    authority.close()
    assert output.read_bytes() == b'{"ok":true}\n'
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    second = admission.open_proof_output_authority(Path("second.json"))
    admission.write_exclusive_proof(second, b"first")
    with pytest.raises(FileExistsError):
        admission.write_exclusive_proof(second, b"replacement")
    second.close()

    target = Path("target")
    target.write_text("untouched")
    link = Path("link")
    link.symlink_to(target)
    with pytest.raises(ValueError, match="refuses symlink"):
        admission.open_proof_output_authority(link)
    assert target.read_text() == "untouched"


def test_proof_output_accepts_absolute_home_path() -> None:
    admission = _load_source_module(
        "authority_admission_absolute_home_output",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    with tempfile.TemporaryDirectory(
        prefix=".tetrabench-proof-", dir=Path.home()
    ) as root:
        parent = Path(root)
        parent.chmod(0o700)
        output = parent / "proof.json"
        authority = admission.open_proof_output_authority(output)
        try:
            admission.write_exclusive_proof(authority, b'{"ok":true}\n')
        finally:
            authority.close()
        assert output.read_bytes() == b'{"ok":true}\n'


def test_proof_output_accepts_private_child_beneath_tmp() -> None:
    admission = _load_source_module(
        "authority_admission_tmp_private_output",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    with tempfile.TemporaryDirectory(prefix="tetrabench-proof-", dir="/tmp") as root:
        parent = Path(root)
        parent.chmod(0o700)
        output = parent / "proof.json"
        authority = admission.open_proof_output_authority(output)
        try:
            admission.write_exclusive_proof(authority, b'{"ok":true}\n')
        finally:
            authority.close()
        assert output.read_bytes() == b'{"ok":true}\n'


def test_proof_output_accepts_cwd_relative_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_relative_private_output",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    parent = Path("private")
    parent.mkdir(mode=0o700)
    output = parent / "proof.json"
    authority = admission.open_proof_output_authority(output)
    try:
        admission.write_exclusive_proof(authority, b'{"ok":true}\n')
    finally:
        authority.close()
    assert output.read_bytes() == b'{"ok":true}\n'


def test_proof_output_rejects_tmp_as_final_parent() -> None:
    admission = _load_source_module(
        "authority_admission_direct_tmp_output",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    output = Path("/tmp") / f"tetrabench-proof-{os.getpid()}.json"
    with pytest.raises(PermissionError, match="current euid and private"):
        admission.open_proof_output_authority(output)
    assert not output.exists()


def test_proof_evidence_normalizes_output_path() -> None:
    admission = _load_source_module(
        "authority_admission_proof_command",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    assert admission.evidence_argv(
        ["--proof-runs", "1", "--output", "/private/proof.json"]
    ) == ["--proof-runs", "1", "--output", "<exclusive-proof-output>"]
    assert admission.evidence_argv(
        ["--proof-runs=1", "--output=/private/proof.json"]
    ) == ["--proof-runs=1", "--output=<exclusive-proof-output>"]


def test_proof_output_rejects_intermediate_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_output_race",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    parent = Path("parent")
    parent.mkdir()
    output = parent / "proof.json"
    authority = admission.open_proof_output_authority(output)
    moved = Path("moved")
    parent.rename(moved)
    parent.mkdir()
    redirected = parent / "proof.json"
    try:
        with pytest.raises(OSError, match="parent identity changed"):
            admission.write_exclusive_proof(authority, b'{"ok":true}\n')
    finally:
        authority.close()
    assert not redirected.exists()
    assert not (moved / "proof.json").exists()


def test_proof_output_write_failure_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_output_failure",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    output = Path("proof.json")
    authority = admission.open_proof_output_authority(output)
    real_fsync = admission.os.fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(admission.os, "fsync", fail_first_fsync)
    try:
        with pytest.raises(OSError, match="injected fsync failure"):
            admission.write_exclusive_proof(authority, b'{"ok":true}\n')
    finally:
        authority.close()
    assert not output.exists()


def test_proof_output_rejects_shared_final_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_untrusted_final_mode",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    parent = Path("parent")
    parent.mkdir()
    parent.chmod(0o770)
    with pytest.raises(PermissionError, match="current euid and private"):
        admission.open_proof_output_authority(parent / "proof.json")


def test_proof_output_accepts_private_sticky_final_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_private_sticky_final_parent",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    parent = Path("parent")
    parent.mkdir()
    parent.chmod(0o1700)
    authority = admission.open_proof_output_authority(parent / "proof.json")
    authority.close()


def test_proof_output_rejects_parent_beneath_nonsticky_writable_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_nonsticky_writable_ancestor",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    ancestor = Path("ancestor")
    parent = ancestor / "private"
    parent.mkdir(parents=True, mode=0o700)
    ancestor.chmod(0o777)
    with pytest.raises(PermissionError, match="proof output ancestor"):
        admission.open_proof_output_authority(parent / "proof.json")


def test_proof_output_rejects_ancestor_owned_by_unrelated_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_unrelated_ancestor_owner",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    ancestor = Path("ancestor")
    parent = ancestor / "private"
    parent.mkdir(parents=True, mode=0o700)
    real_fstat = admission.os.fstat
    unrelated_uid = os.geteuid() + 1

    def unrelated_ancestor_stat(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        if Path(os.readlink(f"/proc/self/fd/{descriptor}")).name != ancestor.name:
            return metadata
        values = list(metadata)
        values[4] = unrelated_uid
        return os.stat_result(values)

    monkeypatch.setattr(admission.os, "fstat", unrelated_ancestor_stat)
    with pytest.raises(PermissionError, match="proof output ancestor"):
        admission.open_proof_output_authority(parent / "proof.json")


def test_proof_output_detects_replacement_during_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_replace_during_fsync",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    output = Path("proof.json")
    moved = Path("moved.json")
    authority = admission.open_proof_output_authority(output)
    real_fsync = admission.os.fsync
    replaced = False

    def replace_on_parent_fsync(descriptor: int) -> None:
        nonlocal replaced
        if descriptor == authority.parent_fd and not replaced:
            replaced = True
            output.rename(moved)
            output.write_bytes(b"replacement")
        real_fsync(descriptor)

    monkeypatch.setattr(admission.os, "fsync", replace_on_parent_fsync)
    try:
        with pytest.raises(OSError, match="identity or bytes changed"):
            admission.write_exclusive_proof(authority, b'{"ok":true}\n')
    finally:
        authority.close()
    assert moved.read_bytes() == b'{"ok":true}\n'
    assert output.read_bytes() == b"replacement"


def test_proof_output_detects_rename_during_file_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _load_source_module(
        "authority_admission_rename_during_close",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    monkeypatch.chdir(tmp_path)
    output = Path("proof.json")
    moved = Path("moved.json")
    authority = admission.open_proof_output_authority(output)
    real_close = admission.os.close
    renamed = False

    def rename_on_file_close(descriptor: int) -> None:
        nonlocal renamed
        if descriptor not in authority.descriptors and not renamed:
            renamed = True
            output.rename(moved)
        real_close(descriptor)

    monkeypatch.setattr(admission.os, "close", rename_on_file_close)
    try:
        with pytest.raises(OSError, match="identity changed during close"):
            admission.write_exclusive_proof(authority, b'{"ok":true}\n')
    finally:
        authority.close()
    assert moved.read_bytes() == b'{"ok":true}\n'
    assert not output.exists()


def test_clean_proof_refuses_dirty_source_without_creating_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "proof.json"
    source = ROOT / "tools/run_authority_fencing_admission.py"
    original = source.read_bytes()
    source.write_bytes(original + b"\n")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(source),
                "--proof-runs",
                "3",
                "--output",
                output.name,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        source.write_bytes(original)
    evidence = json.loads(result.stdout)
    assert result.returncode == 1
    assert evidence["admissible"] is False
    assert evidence["ok"] is False
    assert "clean Git worktree and index" in evidence["error"]
    assert not output.exists()


def test_three_ordered_runs_abort_without_retry_on_second_failure() -> None:
    admission = _load_source_module(
        "authority_admission_three_calls",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    calls: list[int] = []

    def invoke(ordinal: int) -> dict[str, Any]:
        calls.append(ordinal)
        if ordinal == 2:
            raise ValueError("second failed")
        return {"ordinal": ordinal}

    outcome = admission.execute_ordered_calls(3, invoke)
    assert calls == [1, 2]
    assert outcome.records == [{"ordinal": 1}]
    assert outcome.error == "ValueError: second failed"
    diagnostic_runs_ok, full_runs_ok, admissible = admission.proof_status(
        debug=False,
        source_state="clean",
        matrix_ok=True,
        requested_runs=3,
        outcome=outcome,
    )
    assert diagnostic_runs_ok is False
    assert full_runs_ok is False
    assert admissible is False


def test_three_ordered_runs_make_three_independent_calls() -> None:
    admission = _load_source_module(
        "authority_admission_three_successes",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    calls: list[int] = []
    outcome = admission.execute_ordered_calls(
        3,
        lambda ordinal: calls.append(ordinal) or {"ordinal": ordinal},
    )
    assert calls == [1, 2, 3]
    assert [record["ordinal"] for record in outcome.records] == [1, 2, 3]
    assert outcome.error is None
    diagnostic_runs_ok, full_runs_ok, admissible = admission.proof_status(
        debug=True,
        source_state="dirty-debug",
        matrix_ok=True,
        requested_runs=3,
        outcome=outcome,
    )
    assert diagnostic_runs_ok is True
    assert full_runs_ok is True
    assert admissible is False


@pytest.mark.parametrize(
    ("requested", "record_count", "diagnostic_ok", "full_ok", "admissible"),
    [
        (0, 0, False, False, False),
        (1, 1, True, False, False),
        (2, 2, True, False, False),
        (3, 3, True, True, True),
        (4, 4, False, False, False),
        (3, 2, False, False, False),
    ],
)
def test_proof_status_admits_only_exactly_three_complete_records(
    requested: int,
    record_count: int,
    diagnostic_ok: bool,
    full_ok: bool,
    admissible: bool,
) -> None:
    admission = _load_source_module(
        f"authority_admission_status_{requested}_{record_count}",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    outcome = admission.FullRunOutcome(
        records=[{"ordinal": ordinal} for ordinal in range(1, record_count + 1)],
        error=None,
    )
    assert admission.proof_status(
        debug=False,
        source_state="clean",
        matrix_ok=True,
        requested_runs=requested,
        outcome=outcome,
    ) == (diagnostic_ok, full_ok, admissible)


def test_failure_evidence_does_not_serialize_private_paths() -> None:
    admission = _load_source_module(
        "authority_admission_safe_failure",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    assert admission.safe_error(ValueError("failed at /tmp/private/run")) == (
        "ValueError: operation failed"
    )


def test_production_command_bounds_aggregate_output() -> None:
    admission = _load_source_module(
        "authority_admission_bounded_output",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    source = "import os\nos.write(1,b'a'*600000)\nos.write(2,b'b'*600000)\n"
    with pytest.raises(ValueError, match="output exceeded limit"):
        admission._bounded_command(
            [sys.executable, "-c", source],
            cwd=ROOT,
            env=dict(os.environ),
            timeout=10,
        )


def test_production_command_kills_descendant_after_parent_exit(tmp_path: Path) -> None:
    admission = _load_source_module(
        "authority_admission_descendant_cleanup",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    pid_path = tmp_path / "descendant.pid"
    source = (
        "import os,time\n"
        "pid=os.fork()\n"
        "if pid: raise SystemExit(0)\n"
        f"open({str(pid_path)!r},'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(
        (TimeoutError, RuntimeError),
        match=r"production CLI (timed out|descendants retained output pipes)",
    ):
        admission._bounded_command(
            [sys.executable, "-c", source],
            cwd=ROOT,
            env=dict(os.environ),
            timeout=1,
        )
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_path.exists()
    descendant = int(pid_path.read_text())
    while Path(f"/proc/{descendant}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{descendant}").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaper")
@pytest.mark.parametrize("double_fork", [False, True])
def test_production_command_kills_setsid_closed_pipe_daemons(
    tmp_path: Path, double_fork: bool
) -> None:
    admission = _load_source_module(
        f"authority_admission_daemon_{double_fork}",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    pid_path = tmp_path / "daemon.pid"
    marker = tmp_path / "mutated"
    fork_again = "second=os.fork()\nif second: os._exit(0)\n" if double_fork else ""
    source = (
        "import os,time\n"
        "first=os.fork()\n"
        "if first:\n"
        f" while not os.path.exists({str(pid_path)!r}): time.sleep(0.01)\n"
        " raise SystemExit(0)\n"
        f"{fork_again}"
        "os.setsid()\n"
        "os.close(1);os.close(2)\n"
        f"open({str(pid_path)!r},'w').write(str(os.getpid()))\n"
        "time.sleep(0.5)\n"
        f"open({str(marker)!r},'w').write('escaped')\n"
        "time.sleep(30)\n"
    )
    result = admission._bounded_command(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=dict(os.environ),
        timeout=5,
    )
    assert result.containment["descendants_observed_after_exit"] >= 1
    assert result.containment["survivors"] == 0
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_path.exists()
    daemon = int(pid_path.read_text())
    time.sleep(0.6)
    assert not marker.exists()
    assert not Path(f"/proc/{daemon}").exists()


def test_proof_project_selects_only_binary_oracle_candidate(tmp_path: Path) -> None:
    admission = _load_source_module(
        "authority_admission_proof_project",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    project, config_root = admission._write_proof_project(tmp_path)
    catalog = (project / "benchmarks/catalog.toml").read_text()
    profile = (config_root / "tetrabench/config.toml").read_text()
    assert catalog.count('id = "authority-fencing"') == 1
    assert 'reward_policy = "binary"' in catalog
    assert 'include = ["authority-fencing"]' in profile
    assert 'agent_name = "oracle"' in profile
    assert "model_name" not in profile
    assert 'kind = "local"' in profile
    assert 'kind = "docker"' in profile


def test_native_output_snapshot_is_bounded_no_follow_and_complete(
    tmp_path: Path,
) -> None:
    admission = _load_source_module(
        "authority_admission_native_snapshot",
        ROOT / "tools/run_authority_fencing_admission.py",
    )
    output = tmp_path / "run"
    output.mkdir(mode=0o700)
    nested = output / "job"
    nested.mkdir()
    (nested / "result.json").write_bytes(b'{"ok":true}\n')
    snapshot = admission.snapshot_native_output(output)
    assert snapshot.read("job/result.json") == b'{"ok":true}\n'
    assert snapshot.manifest == [
        {"mode": 0o700, "path": ".", "type": "directory"},
        {"mode": 0o755, "path": "job", "type": "directory"},
        {
            "mode": 0o644,
            "path": "job/result.json",
            "sha256": hashlib.sha256(b'{"ok":true}\n').hexdigest(),
            "size": 12,
            "type": "file",
        },
    ]
    (output / "escape").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="unsafe entry"):
        admission.snapshot_native_output(output)


def test_proof_output_requires_proof_mode(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_authority_fencing_admission.py"),
            "--output",
            str(tmp_path / "proof.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "--output requires --proof-runs" in result.stderr


@pytest.mark.docker
def test_authority_fencing_admission_and_mutant_attribution(tmp_path: Path) -> None:
    assert _docker_available(), "Docker daemon is required for the test suite"
    result = subprocess.run(
        [
            "python",
            str(ROOT / "tools/run_authority_fencing_admission.py"),
            "--debug",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    evidence = json.loads(result.stdout)
    assert result.returncode == 1
    assert evidence["ok"] is False
    assert evidence["admissible"] is False
    assert evidence["debug"] is True
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
        evidence["subject"]["task_tests_manifest_sha256"]
        == evidence["subject"]["verifier_context_sha256"]
    )
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

    assert evidence["subject"]["source_state"] == "dirty-debug"
    assert evidence["source_snapshot"]["mode"] == "tracked-worktree-debug-copy"
    assert evidence["source_snapshot"]["archive_sha256"] is None
    assert evidence["full_runs_ok"] is False


@pytest.mark.docker
def test_authority_fencing_non_admission_uses_isolated_production_cli_once(
    tmp_path: Path,
) -> None:
    assert _docker_available(), "Docker daemon is required for the test suite"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_authority_fencing_admission.py"),
            "--debug",
            "--proof-runs",
            "1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    evidence = json.loads(result.stdout)
    assert result.returncode == 1
    assert evidence["schema_version"] == 3
    assert evidence["ok"] is False
    assert evidence["admissible"] is False
    assert evidence["matrix_ok"] is True
    assert evidence["proof_repetitions"] == 1
    assert evidence["full_run_count"] == 1
    assert evidence["diagnostic_runs_ok"] is True
    assert evidence["full_runs_ok"] is False
    run = evidence["full_runs"][0]
    assert run["ordinal"] == 1
    assert run["cli"]["outcome"] == "succeeded"
    assert run["cli"]["reward"] == "1"
    assert run["cli"]["summary"]["policy"] == "binary"
    assert run["cli"]["summary"]["pass_count"] == 1
    assert run["cli"]["summary"]["sample_count"] == 1
    assert len(run["native"]["trial"]["task_checksum"]) == 64
    assert run["native"]["trial"]["task_digest"].startswith("sha256:")
    assert len(run["native"]["trial"]["task_digest"]) == 71
    assert len(run["artifact_manifest"]["sha256"]) == 64
    assert run["containment"]["subreaper"] is True
    assert run["containment"]["survivors"] == 0
    assert run["output_snapshot"]["manifest"][0] == {
        "mode": 0o700,
        "path": ".",
        "type": "directory",
    }
    distribution = evidence["cli_distribution"]
    assert distribution["wheel"]["filename"].endswith(".whl")
    assert len(distribution["wheel"]["sha256"]) == 64
    assert distribution["executable"] == "<private-venv>/bin/tetrabench"
    assert distribution["python"]["executable"] == "<private-venv>/bin/python"
    assert distribution["distribution"]["metadata"]["name"] == "tetrabench"
    installed = {
        item["name"].lower(): item["version"]
        for item in distribution["distribution"]["installed_distributions"]
    }
    assert installed["tetrabench"] == "0.1.0"
    assert installed["harbor"] == "0.22.0"
    serialized = result.stdout
    assert "/tmp/" not in serialized
    assert str(tmp_path) not in serialized
    assert str(Path.home()) not in serialized


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
