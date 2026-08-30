#!/usr/bin/env python3
"""Run the bounded authority-fencing gold, no-op, mutant, and exploit matrix."""

from __future__ import annotations

import argparse
import atexit
import ctypes
import dataclasses
import hashlib
import json
import os
import platform
import selectors
import shutil
import signal
import stat
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
import tomllib
import uuid
import warnings
from collections.abc import Callable
from contextlib import suppress
from importlib import metadata
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock, TrialLock
from harbor.models.job.result import JobResult
from harbor.models.task.task import Task
from harbor.models.trajectories import Trajectory
from harbor.models.trial.artifact_manifest import ArtifactManifestEntry
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult
from harbor.publisher.packager import Packager

from tetrabench.rewards import SectionRewardSummary

ROOT = Path(__file__).parents[1]
TASK = ROOT / "benchmarks/tasks/systems-design/authority-fencing"
TESTS = TASK / "tests"
ADMISSION_TOOL = ROOT / "tools/run_authority_fencing_admission.py"
ADMISSION_TEST = ROOT / "tests/test_authority_fencing_task.py"
ADMISSION_TEST_HELPER = ROOT / "tests/conftest.py"
SOURCE_PATHS = (TASK, ADMISSION_TOOL, ADMISSION_TEST, ADMISSION_TEST_HELPER)
SOURCE_RELATIVE_PATHS = (
    Path("src/tetrabench"),
    Path("pyproject.toml"),
    Path("uv.lock"),
    TASK.relative_to(ROOT),
    ADMISSION_TOOL.relative_to(ROOT),
    ADMISSION_TEST.relative_to(ROOT),
    ADMISSION_TEST_HELPER.relative_to(ROOT),
)
DEFAULT_IMAGE = "tetrabench-authority-fencing-verifier:local"
DOCKER = "/usr/bin/docker"
PROOF_PROFILE = "authority-proof"
MAX_PROOF_RUNS = 3
MAX_CLI_OUTPUT_BYTES = 1 << 20
MAX_NATIVE_FILES = 10_000
MAX_NATIVE_FILE_BYTES = 64 << 20
MAX_NATIVE_TOTAL_BYTES = 1 << 30
PRODUCTION_RUN_TIMEOUT_SECONDS = 2_700
PROCESS_CLEANUP_SECONDS = 10
PIPE_DRAIN_SECONDS = 2
PR_SET_CHILD_SUBREAPER = 36
GATES = (
    "single-authority",
    "monotonic-fence",
    "stale-rejection",
    "restart-durability",
    "transaction-rollback",
    "terminal-idempotence",
)


@dataclasses.dataclass(frozen=True, slots=True)
class SourceSnapshot:
    root: Path
    revision: str
    source_state: str
    archive_sha256: str | None
    mode: str

    @property
    def task(self) -> Path:
        return self.root / TASK.relative_to(ROOT)

    @property
    def tests(self) -> Path:
        return self.task / "tests"


@dataclasses.dataclass(frozen=True, slots=True)
class InstalledCLI:
    executable: Path
    python: Path
    attestation: dict[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    containment: dict[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class NativeSnapshot:
    files: dict[str, bytes]
    manifest: list[dict[str, Any]]

    def read(self, path: str) -> bytes:
        try:
            return self.files[path]
        except KeyError as error:
            raise ValueError(f"native Harbor evidence omits {path}") from error


@dataclasses.dataclass(frozen=True, slots=True)
class ProofOutputAuthority:
    parent_fd: int
    name: str
    descriptors: tuple[int, ...]
    anchors: tuple[tuple[int, int, str, tuple[int, int, int, int]], ...]
    parents: tuple[tuple[int, tuple[int, int, int, int]], ...]

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            with suppress(OSError):
                os.close(descriptor)


@dataclasses.dataclass(frozen=True, slots=True)
class FullRunOutcome:
    records: list[dict[str, Any]]
    error: str | None


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strict_json(data: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("JSON contains duplicate keys")
            value[key] = item
        return value

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error


def safe_error(error: BaseException) -> str:
    message = str(error)
    if (
        not message
        or len(message) > 256
        or "/" in message
        or "\\" in message
        or any(character in message for character in "\r\n\0")
    ):
        message = "operation failed"
    return f"{type(error).__name__}: {message}"


def hidden_case_input(case_manifest: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for item in case_manifest["cases"]:
        value = {
            "gate_ids": item["gate_ids"],
            "id": item["id"],
            "scenario": item["scenario"],
            "seed": item["seed"],
        }
        if "fault_schedule_id" in item:
            value["fault_schedule_id"] = item["fault_schedule_id"]
        cases.append(value)
    return {
        "cases": cases,
        "fault_schedules": case_manifest["fault_schedules"],
        "schema_version": case_manifest["schema_version"],
        "task_id": case_manifest["task_id"],
    }


def validate_hidden_case_binding_for(task: Path) -> str:
    contract = tomllib.loads((task / "contract.toml").read_text())
    cases = tomllib.loads((task / "tests/cases.toml").read_text())
    value = hashlib.sha256(canonical(hidden_case_input(cases)).encode()).hexdigest()
    if value != cases.get("input_manifest_sha256"):
        raise ValueError("hidden case manifest digest mismatch")
    if value != contract.get("hidden_case_input_sha256"):
        raise ValueError("public contract hidden-case digest mismatch")
    return value


def validate_hidden_case_binding() -> str:
    return validate_hidden_case_binding_for(TASK)


def mutant_attribution_passes(gates: dict[str, Any], intended_gate: str) -> bool:
    return set(gates) == set(GATES) and all(
        gates[gate] == (0 if gate == intended_gate else 1) for gate in GATES
    )


def _manifest_entry(path: Path, relative: Path) -> dict[str, Any]:
    metadata = path.lstat()
    entry: dict[str, Any] = {
        "mode": stat.S_IMODE(metadata.st_mode),
        "path": "." if relative == Path(".") else relative.as_posix(),
    }
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("manifest input contains a symlink")
    if stat.S_ISDIR(metadata.st_mode):
        entry["type"] = "directory"
        return entry
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("manifest input contains a special file")
    data = path.read_bytes()
    entry.update(
        {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "type": "file",
        }
    )
    return entry


def tree_manifest(root: Path) -> list[dict[str, Any]]:
    manifest = [_manifest_entry(root, Path("."))]
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir(), reverse=True):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            entry = _manifest_entry(path, relative)
            manifest.append(entry)
            if entry["type"] == "directory":
                pending.append(path)
    return sorted(manifest, key=lambda item: item["path"])


def manifest_digest(manifest: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical(manifest).encode()).hexdigest()


def tree_digest(root: Path) -> str:
    return manifest_digest(tree_manifest(root))


def tests_context_digest(root: Path = TESTS) -> str:
    return tree_digest(root)


def source_manifest(
    paths: tuple[Path, ...] = SOURCE_PATHS, *, root: Path = ROOT
) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for selected in paths:
        selected = selected.absolute()
        try:
            relative = selected.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("source manifest path escapes repository") from exc
        entries = (
            tree_manifest(selected)
            if selected.is_dir()
            else [_manifest_entry(selected, Path("."))]
        )
        for entry in entries:
            suffix = "" if entry["path"] == "." else f"/{entry['path']}"
            value = {**entry, "path": relative.as_posix() + suffix}
            if value["path"] in seen:
                raise ValueError("source manifest paths overlap")
            seen.add(value["path"])
            manifest.append(value)
    return sorted(manifest, key=lambda item: item["path"])


def source_git_state(
    manifest: list[dict[str, Any]], *, root: Path = ROOT
) -> tuple[str | None, str]:
    git = shutil.which("git")
    if git is None:
        return None, "dirty"
    try:
        revision = subprocess.run(  # nosec B603
            [git, "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if len(revision) != 40:
            return None, "dirty"
        for entry in manifest:
            object_type = subprocess.run(  # nosec B603
                [git, "cat-file", "-t", f"HEAD:{entry['path']}"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            expected_type = "tree" if entry["type"] == "directory" else "blob"
            if object_type != expected_type:
                return None, "dirty"
            if entry["type"] == "directory":
                if entry["mode"] != 0o755:
                    return None, "dirty"
                continue
            head_data = subprocess.run(  # nosec B603
                [git, "show", f"HEAD:{entry['path']}"],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout
            mode_output = subprocess.run(  # nosec B603
                [git, "ls-tree", "HEAD", "--", entry["path"]],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            head_mode = 0o755 if mode_output.startswith("100755 ") else 0o644
            if (
                entry["size"] != len(head_data)
                or entry["sha256"] != hashlib.sha256(head_data).hexdigest()
                or entry["mode"] != head_mode
            ):
                return None, "dirty"
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None, "dirty"
    return revision, "clean"


def _git(command: list[str], *, root: Path = ROOT) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for admission source identity")
    return subprocess.run(  # nosec B603
        [git, *command],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def _captured_head(*, root: Path = ROOT) -> str:
    revision = _git(["rev-parse", "--verify", "HEAD"], root=root).decode().strip()
    if len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("Git HEAD is not a full lowercase object ID")
    return revision


def _require_clean_source(revision: str, *, root: Path = ROOT) -> None:
    status = _git(["status", "--porcelain=v1", "--untracked-files=all"], root=root)
    if status:
        raise ValueError("admissible proof requires a clean Git worktree and index")
    for path in SOURCE_RELATIVE_PATHS:
        _git(["cat-file", "-e", f"{revision}:{path.as_posix()}"], root=root)


def _safe_extract_archive(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:") as stream:
        for member in stream.getmembers():
            member_path = Path(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or member.isdev()
                or member.isfifo()
            ):
                raise ValueError("Git archive contains an unsafe member")
        stream.extractall(destination, filter="data")


def create_clean_source_snapshot(
    root: Path, *, repository: Path = ROOT
) -> SourceSnapshot:
    revision = _captured_head(root=repository)
    _require_clean_source(revision, root=repository)
    archive = root / "source.tar"
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for admission source identity")
    with archive.open("xb") as stream:
        subprocess.run(  # nosec B603
            [git, "archive", "--format=tar", revision],
            cwd=repository,
            check=True,
            stdout=stream,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    archive_sha256 = sha256_bytes(archive.read_bytes())
    snapshot_root = root / "source"
    snapshot_root.mkdir(mode=0o700)
    _safe_extract_archive(archive, snapshot_root)
    archive.unlink()
    return SourceSnapshot(
        root=snapshot_root,
        revision=revision,
        source_state="clean",
        archive_sha256=archive_sha256,
        mode="git-archive-head",
    )


def create_debug_source_snapshot(
    root: Path, *, repository: Path = ROOT
) -> SourceSnapshot:
    revision = _captured_head(root=repository)
    names = _git(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        root=repository,
    ).split(b"\0")
    snapshot_root = root / "source"
    snapshot_root.mkdir(mode=0o700)
    for encoded in names:
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("tracked source path is unsafe")
        source = repository / relative
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_metadata = source.lstat()
        if stat.S_ISLNK(source_metadata.st_mode):
            destination.symlink_to(os.readlink(source))
        elif stat.S_ISREG(source_metadata.st_mode):
            destination.write_bytes(source.read_bytes())
            destination.chmod(stat.S_IMODE(source_metadata.st_mode))
        else:
            raise ValueError("tracked source contains an unsupported entry")
    return SourceSnapshot(
        root=snapshot_root,
        revision=revision,
        source_state="dirty-debug",
        archive_sha256=None,
        mode="tracked-worktree-debug-copy",
    )


def snapshot_source_manifest(snapshot: SourceSnapshot) -> list[dict[str, Any]]:
    paths = tuple(snapshot.root / path for path in SOURCE_RELATIVE_PATHS)
    return source_manifest(paths, root=snapshot.root)


def _run_checked(command: list[str], *, cwd: Path, timeout: int = 600) -> bytes:
    result = subprocess.run(  # nosec B603
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=timeout,
    )
    return result.stdout


def install_snapshot_cli(snapshot: SourceSnapshot, private_root: Path) -> InstalledCLI:
    uv_text = shutil.which("uv")
    if uv_text is None:
        raise RuntimeError("uv is required to build the proof CLI")
    uv = Path(uv_text).resolve()
    requirements = private_root / "requirements.txt"
    build_constraints = private_root / "build-constraints.txt"
    dist = private_root / "dist"
    dist.mkdir(mode=0o700)
    _run_checked(
        [
            str(uv),
            "export",
            "--quiet",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
        ],
        cwd=snapshot.root,
    )
    _run_checked(
        [
            str(uv),
            "export",
            "--quiet",
            "--locked",
            "--all-groups",
            "--no-emit-project",
            "--output-file",
            str(build_constraints),
        ],
        cwd=snapshot.root,
    )
    _run_checked(
        [
            str(uv),
            "build",
            "--wheel",
            "--python",
            "3.12",
            "--build-constraints",
            str(build_constraints),
            "--require-hashes",
            "--out-dir",
            str(dist),
        ],
        cwd=snapshot.root,
    )
    wheels = list(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("snapshot build did not produce exactly one wheel")
    wheel = wheels[0]
    venv = private_root / "venv"
    _run_checked([str(uv), "venv", "--python", "3.12", str(venv)], cwd=snapshot.root)
    python = (venv / "bin/python").absolute()
    executable = (venv / "bin/tetrabench").absolute()
    _run_checked(
        [
            str(uv),
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "--no-deps",
            "-r",
            str(requirements),
        ],
        cwd=snapshot.root,
    )
    _run_checked(
        [
            str(uv),
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            str(wheel),
        ],
        cwd=snapshot.root,
    )
    _run_checked([str(uv), "pip", "check", "--python", str(python)], cwd=snapshot.root)
    inspect_script = r"""
import importlib.metadata as m, json, platform, sys
package = m.distribution('tetrabench')
installed = sorted(
    ({'name': d.metadata['Name'], 'version': d.version} for d in m.distributions()),
    key=lambda item: (item['name'].lower(), item['version']),
)
value = {
    'entry_points': sorted(
        {'name': e.name, 'group': e.group, 'value': e.value}
        for e in package.entry_points
    ),
    'installed_distributions': installed,
    'metadata': {
        'name': package.metadata['Name'],
        'requires_dist': sorted(package.requires or []),
        'requires_python': package.metadata['Requires-Python'],
        'version': package.version,
    },
    'python': {'executable': sys.executable, 'version': platform.python_version()},
}
print(json.dumps(value, separators=(',', ':'), sort_keys=True))
"""
    installed = strict_json(
        _run_checked([str(python), "-c", inspect_script], cwd=snapshot.root)
    )
    if not executable.is_file() or installed["python"]["executable"] != str(python):
        raise ValueError("isolated CLI executable identity mismatch")
    installed["python"]["executable"] = "<private-venv>/bin/python"
    return InstalledCLI(
        executable=executable,
        python=python,
        attestation={
            "dependency_export_sha256": sha256_bytes(requirements.read_bytes()),
            "distribution": installed,
            "executable": "<private-venv>/bin/tetrabench",
            "python": {
                **installed["python"],
            },
            "uv": {
                "executable": "<trusted-uv>",
                "version": _run_checked([str(uv), "--version"], cwd=snapshot.root)
                .decode()
                .strip(),
            },
            "wheel": {
                "filename": wheel.name,
                "sha256": sha256_bytes(wheel.read_bytes()),
                "size": wheel.stat().st_size,
            },
        },
    )


def verifier_base_image(tests: Path = TESTS) -> str:
    first = (tests / "Dockerfile").read_text().splitlines()[0]
    prefix = "FROM "
    if not first.startswith(prefix) or not first.removeprefix(prefix):
        raise ValueError("verifier Dockerfile has no fixed base image")
    return first.removeprefix(prefix)


def matrix_contract(mutants: list[dict[str, Any]]) -> dict[str, Any]:
    exploit_names = [
        "exploit-hardcoded-public",
        "exploit-fake-reward",
        "exploit-hidden-discovery",
        "exploit-output-forgery",
        "exploit-symlink",
        "exploit-special-file",
        "exploit-extra-executable",
        "exploit-background-process",
    ]
    candidates = [
        {"expected_reward": 1, "intended_gate": None, "name": "gold"},
        {"expected_reward": 0, "intended_gate": None, "name": "no-op"},
        *[
            {
                "expected_reward": 0,
                "intended_gate": item["gate_id"],
                "name": item["id"],
            }
            for item in mutants
        ],
        {
            "expected_reward": 0,
            "intended_gate": "single-authority",
            "name": "attribution-probe-broad-mutant",
        },
        *[
            {"expected_reward": 0, "intended_gate": None, "name": name}
            for name in exploit_names
        ],
    ]
    return {
        "candidates": candidates,
        "gates": list(GATES),
    }


def matrix_projection(
    candidates: list[tuple[str, str | None, str, str | None]],
) -> list[dict[str, Any]]:
    return [
        {
            "expected_reward": int(name == "gold"),
            "intended_gate": gate,
            "name": name,
        }
        for name, gate, _source, _tree_mutation in candidates
    ]


def admission_subject(
    *,
    hidden_case_input_sha256: str,
    mutants: list[dict[str, Any]],
    tool_version_values: dict[str, Any],
    verifier_context_sha256: str,
    source_paths: tuple[Path, ...] = SOURCE_PATHS,
    source_root: Path = ROOT,
    verifier_context_manifest: list[dict[str, Any]] | None = None,
    task: Path = TASK,
    tests: Path = TESTS,
    source_revision: str | None = None,
    source_state: str | None = None,
) -> dict[str, Any]:
    verifier_manifest = (
        tree_manifest(tests)
        if verifier_context_manifest is None
        else verifier_context_manifest
    )
    if manifest_digest(verifier_manifest) != verifier_context_sha256:
        raise ValueError("verifier context manifest digest mismatch")
    candidate_manifest = source_manifest(source_paths, root=source_root)
    if source_state is None:
        revision, state = source_git_state(candidate_manifest, root=source_root)
    else:
        revision, state = source_revision, source_state
    return {
        "base_image": verifier_base_image(tests),
        "hidden_case_input_sha256": hidden_case_input_sha256,
        "matrix_contract": matrix_contract(mutants),
        "source_manifest": candidate_manifest,
        "source_manifest_sha256": manifest_digest(candidate_manifest),
        "source_revision": revision,
        "source_state": state,
        "task_context_sha256": tree_digest(task),
        "task_tests_manifest_sha256": manifest_digest(tree_manifest(tests)),
        "task_id": "systems-design/authority-fencing",
        "tool_versions": tool_version_values,
        "verifier_context_manifest": verifier_manifest,
        "verifier_context_sha256": verifier_context_sha256,
    }


def docker_versions(*, cwd: Path = ROOT) -> dict[str, str]:
    value = subprocess.run(  # nosec B603
        [
            DOCKER,
            "version",
            "--format",
            "{{.Client.Version}}|{{.Server.Version}}",
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    client, separator, server = value.partition("|")
    if not separator or not client or not server:
        raise ValueError("Docker version output mismatch")
    return {"client": client, "server": server}


def build_admission_image(
    context: Path, context_sha256: str, *, cwd: Path = ROOT
) -> tuple[str, dict[str, Any]]:
    nonce = uuid.uuid4().hex
    tag = f"tetrabench-authority-fencing:{context_sha256[:20]}-{nonce[:12]}"
    with tempfile.NamedTemporaryFile(
        prefix="authority-image-id-", delete=False
    ) as file:
        iidfile = Path(file.name)
    iidfile.unlink()
    command = [
        DOCKER,
        "build",
        "--no-cache",
        "--iidfile",
        str(iidfile),
        "--build-arg",
        f"TETRABENCH_ADMISSION_BUILD_NONCE={nonce}",
        "--tag",
        tag,
        str(context),
    ]
    try:
        subprocess.run(  # nosec B603
            command,
            cwd=cwd,
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=300,
        )
        image_id = iidfile.read_text().strip()
    finally:
        iidfile.unlink(missing_ok=True)
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError("Docker did not return an immutable image ID")
    repo_digests_text = subprocess.run(  # nosec B603
        [DOCKER, "image", "inspect", image_id, "--format", "{{json .RepoDigests}}"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    repo_digests = json.loads(repo_digests_text)
    if repo_digests is None:
        repo_digests = []
    if not isinstance(repo_digests, list) or not all(
        isinstance(item, str) for item in repo_digests
    ):
        raise ValueError("Docker image digest output mismatch")
    normalized_command = [
        DOCKER,
        "build",
        "--no-cache",
        "--iidfile",
        "<temporary-iidfile>",
        "--build-arg",
        f"TETRABENCH_ADMISSION_BUILD_NONCE={nonce}",
        "--tag",
        tag,
        "<verifier-context>",
    ]
    return image_id, {
        "build_command": normalized_command,
        "build_nonce": nonce,
        "image_id": image_id,
        "mode": "fresh-no-cache-build",
        "repo_digests": sorted(repo_digests),
        "tag": tag,
    }


def tool_versions(*, cwd: Path = ROOT) -> dict[str, Any]:
    return {
        "docker": docker_versions(cwd=cwd),
        "harbor": metadata.version("harbor"),
        "python": platform.python_version(),
        "tetrabench": metadata.version("tetrabench"),
    }


def reference_source(task: Path = TASK) -> str:
    solution = (task / "solution/solve.sh").read_text()
    start = "cat > /workspace/authority.py <<'PY'\n"
    prefix, separator, remainder = solution.partition(start)
    if not separator or not prefix.startswith("#!/bin/sh"):
        raise ValueError("solution source marker missing")
    source, end, _suffix = remainder.partition("\nPY\n")
    if not end:
        raise ValueError("solution source terminator missing")
    return source + "\n"


def stage(root: Path, source: str, *, task: Path = TASK) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    for name in ("README.md", "contract.toml", "test_public.py"):
        shutil.copyfile(task / name, workspace / name)
    (workspace / "authority.py").write_text(source)
    return workspace


def exploit_source(kind: str, reference: str) -> str:
    if kind == "hidden-discovery":
        return """#!/usr/bin/env python3
from pathlib import Path
Path('/tests/cases.toml').read_text()
"""
    if kind == "output-forgery":
        return """#!/usr/bin/env python3
print('{"job":{"deadline":1,"fence_token":1,"job_id":"forged","status":"running","terminal_op":null,"worker_id":"forged"},"ok":true}')
"""
    if kind == "background-process":
        marker = "from __future__ import annotations\n"
        payload = """
import os as _os
import time as _time
if _os.fork() == 0:
    _os.setsid()
    _time.sleep(30)
    _os._exit(0)
"""
        return reference.replace(marker, marker + payload, 1)
    raise ValueError(f"unknown source exploit: {kind}")


def mutate_tree(workspace: Path, kind: str | None) -> None:
    if kind is None:
        return
    if kind == "fake-reward":
        (workspace / "reward.json").write_text('{"reward":1}\n')
    elif kind == "symlink":
        (workspace / "authority.py").unlink()
        (workspace / "authority.py").symlink_to("/etc/passwd")
    elif kind == "special-file":
        (workspace / "authority.py").unlink()
        os.mkfifo(workspace / "authority.py")
    elif kind == "extra-executable":
        extra = workspace / "python"
        extra.write_text("#!/bin/sh\nexit 0\n")
        extra.chmod(0o755)
    else:
        raise ValueError(f"unknown tree exploit: {kind}")


def run_verifier(
    image: str, workspace: Path, output: Path
) -> tuple[int, dict[str, Any]]:
    output.mkdir()
    command = [
        DOCKER,
        "run",
        "--rm",
        "--network",
        "none",
        "--volume",
        f"{workspace}:/workspace:ro",
        "--volume",
        f"{output}:/logs/verifier",
        image,
        "/bin/sh",
        "-c",
        "/tests/test.sh; status=$?; chmod -R a+rwx /logs/verifier; exit $status",
    ]
    subprocess.run(  # nosec B603
        command,
        cwd=ROOT,
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
        timeout=180,
    )
    reward = json.loads((output / "reward.json").read_text())["reward"]
    diagnostics = json.loads((output / "diagnostics.json").read_text())
    if type(reward) is not int or reward not in {0, 1}:
        raise ValueError("verifier emitted a non-binary reward")
    return reward, diagnostics


def concurrent_claim_evidence(
    diagnostics: dict[str, Any], *, required: bool
) -> dict[str, Any] | None:
    value = diagnostics.get("concurrent_claim")
    if value is None:
        if required:
            raise ValueError(
                "passing verifier diagnostics omit concurrent claim evidence"
            )
        return None
    if (
        type(value) is not dict
        or set(value)
        != {
            "durable_owner_token_match",
            "loser_worker_id",
            "winner_worker_id",
            "winning_fence_token",
        }
        or type(value["winner_worker_id"]) is not str
        or type(value["loser_worker_id"]) is not str
        or not value["winner_worker_id"]
        or not value["loser_worker_id"]
        or value["winner_worker_id"] == value["loser_worker_id"]
        or type(value["winning_fence_token"]) is not int
        or value["winning_fence_token"] != 2
        or value["durable_owner_token_match"] is not True
    ):
        raise ValueError("verifier concurrent claim evidence mismatch")
    return value


def become_child_subreaper() -> None:
    if sys.platform != "linux" or not Path("/proc/self/status").is_file():
        raise RuntimeError("proof containment requires Linux /proc")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "cannot become a child subreaper")


def _process_parents() -> dict[int, int]:
    parents: dict[int, int] = {}
    try:
        entries = list(os.scandir("/proc"))
    except OSError as error:
        raise RuntimeError("process containment evidence unavailable") from error
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/status", encoding="ascii") as stream:
                parent_lines = [line for line in stream if line.startswith("PPid:")]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, UnicodeError) as error:
            raise RuntimeError("process containment evidence unavailable") from error
        if len(parent_lines) != 1:
            raise RuntimeError("process containment evidence malformed")
        value = parent_lines[0].split(":", 1)[1].strip()
        if not value.isdigit():
            raise RuntimeError("process containment evidence malformed")
        parents[int(entry.name)] = int(value)
    return parents


def _descendants(root_pid: int, parents: dict[int, int]) -> set[int]:
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        children = {pid for pid, ppid in parents.items() if ppid == parent}
        new = children - found
        found.update(new)
        pending.extend(new)
    return found


def _reap_adopted_children() -> list[int]:
    reaped: list[int] = []
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
        reaped.append(pid)
    return reaped


def _kill_processes(pids: set[int]) -> None:
    own_group = os.getpgrp()
    groups: set[int] = set()
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            group = os.getpgid(pid)
            if group != own_group:
                groups.add(group)
    for group in groups:
        with suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
    for pid in pids:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _cleanup_command_processes(
    process: subprocess.Popen[bytes], baseline: set[int]
) -> dict[str, Any]:
    deadline = time.monotonic() + PROCESS_CLEANUP_SECONDS
    observed: set[int] = set()
    reaped: set[int] = set()
    while True:
        parents = _process_parents()
        current = _descendants(os.getpid(), parents) - baseline
        current.discard(process.pid)
        observed.update(current)
        targets = set(current)
        if process.poll() is None:
            targets.add(process.pid)
        _kill_processes(targets)
        reaped.update(_reap_adopted_children())
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.05)
        parents = _process_parents()
        survivors = _descendants(os.getpid(), parents) - baseline
        survivors.discard(process.pid)
        direct_alive = process.poll() is None
        if not survivors and not direct_alive:
            return {
                "descendants_observed_after_exit": len(observed),
                "reaped_children": len(reaped),
                "survivors": 0,
                "subreaper": True,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError("proof process containment left surviving descendants")
        time.sleep(0.01)


def _drain_pipes(
    selector: selectors.BaseSelector,
    retained: dict[str, bytearray],
    retained_size: int,
    *,
    deadline: float,
) -> tuple[int, Exception | None]:
    failure: Exception | None = None
    while selector.get_map() and time.monotonic() < deadline:
        events = selector.select(min(deadline - time.monotonic(), 0.05))
        for key, _mask in events:
            chunk = os.read(key.fd, 65_536)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            if retained_size + len(chunk) > MAX_CLI_OUTPUT_BYTES:
                failure = ValueError("production CLI output exceeded limit")
                return retained_size, failure
            retained[key.data].extend(chunk)
            retained_size += len(chunk)
    return retained_size, failure


def _bounded_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    check: bool = True,
) -> CommandResult:
    become_child_subreaper()
    baseline = _descendants(os.getpid(), _process_parents())
    process = subprocess.Popen(  # nosec B603
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("production CLI pipes were not created")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    retained = {"stdout": bytearray(), "stderr": bytearray()}
    retained_size = 0
    deadline = time.monotonic() + timeout
    failure: Exception | None = None
    direct_exited_at: float | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = TimeoutError("production CLI timed out")
                break
            events = selector.select(min(remaining, 1.0))
            if process.poll() is not None and direct_exited_at is None:
                direct_exited_at = time.monotonic()
            if (
                direct_exited_at is not None
                and time.monotonic() - direct_exited_at >= PIPE_DRAIN_SECONDS
            ):
                failure = RuntimeError(
                    "production CLI descendants retained output pipes"
                )
                break
            for key, _mask in events:
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output = retained[key.data]
                if retained_size + len(chunk) > MAX_CLI_OUTPUT_BYTES:
                    failure = ValueError("production CLI output exceeded limit")
                    break
                output.extend(chunk)
                retained_size += len(chunk)
            if failure is not None:
                break
    finally:
        containment = _cleanup_command_processes(process, baseline)
        retained_size, drain_failure = _drain_pipes(
            selector,
            retained,
            retained_size,
            deadline=time.monotonic() + PIPE_DRAIN_SECONDS,
        )
        if failure is None and drain_failure is not None:
            failure = drain_failure
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if failure is not None:
        raise failure
    stdout = bytes(retained["stdout"])
    stderr = bytes(retained["stderr"])
    if check and process.returncode != 0:
        raise ValueError(f"production CLI exited {process.returncode}")
    return CommandResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        containment=containment,
    )


def _copy_verified_tree(source: Path, destination: Path) -> list[dict[str, Any]]:
    before = tree_manifest(source)
    shutil.copytree(
        source,
        destination,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    after = tree_manifest(source)
    copied = tree_manifest(destination)
    if before != after or before != copied:
        raise ValueError("source snapshot changed while copying task tree")
    return before


def _write_proof_project(root: Path, *, task_source: Path = TASK) -> tuple[Path, Path]:
    project = root / "project"
    task = project / "tasks/authority-fencing"
    task.parent.mkdir(parents=True)
    _copy_verified_tree(task_source, task)
    benchmarks = project / "benchmarks"
    benchmarks.mkdir()
    (benchmarks / "systems.md").write_text("# systems-design\n", encoding="utf-8")
    (benchmarks / "github.md").write_text("# github-workflow\n", encoding="utf-8")
    catalog = (
        """schema_version = 1
[sections.systems-design]
readme = "systems.md"
"""
        'tasks = [{ id = "authority-fencing", '
        'harbor_task = "tasks/authority-fencing", '
        'reward_policy = "binary" }]\n'
        """
[sections.github-workflow]
readme = "github.md"
tasks = []
"""
    )
    (benchmarks / "catalog.toml").write_text(catalog, encoding="utf-8")
    (project / "tetrabench.toml").write_text(
        """schema_version = 1
catalog_path = "benchmarks/catalog.toml"
[controller]
kind = "modal"
[execution]
kind = "modal"
""",
        encoding="utf-8",
    )
    config_root = root / "config"
    user_config = config_root / "tetrabench/config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        f"""schema_version = 1
[profiles.{PROOF_PROFILE}.controller]
kind = "local"
[profiles.{PROOF_PROFILE}.execution]
kind = "docker"
[profiles.{PROOF_PROFILE}.selection]
include = ["authority-fencing"]
[profiles.{PROOF_PROFILE}.harbor]
agent_name = "oracle"
attempts = 1
concurrency = 1
""",
        encoding="utf-8",
    )
    return project, config_root


def _read_fd_bytes(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining or os.read(descriptor, 1):
        raise ValueError("native Harbor evidence file changed while reading")
    return b"".join(chunks)


def snapshot_native_output(root: Path) -> NativeSnapshot:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if sys.platform != "linux" or any(not hasattr(os, name) for name in required):
        raise RuntimeError("native evidence snapshot requires Linux no-follow openat")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    entry_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    root_fd = os.open(root, directory_flags)
    files: dict[str, bytes] = {}
    manifest: list[dict[str, Any]] = []
    count = 0
    total = 0

    def visit(directory_fd: int, relative: tuple[str, ...]) -> None:
        nonlocal count, total
        try:
            iterator = os.scandir(directory_fd)
        except OSError as error:
            raise ValueError("cannot enumerate native Harbor evidence") from error
        with iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            count += 1
            if count > MAX_NATIVE_FILES:
                raise ValueError("native Harbor evidence entry count exceeds limit")
            path_parts = (*relative, entry.name)
            logical_path = "/".join(path_parts)
            try:
                descriptor = os.open(entry.name, entry_flags, dir_fd=directory_fd)
            except OSError as error:
                raise ValueError(
                    "native Harbor evidence contains an unsafe entry"
                ) from error
            try:
                before = os.fstat(descriptor)
                mode = stat.S_IMODE(before.st_mode)
                if stat.S_ISDIR(before.st_mode):
                    manifest.append(
                        {"mode": mode, "path": logical_path, "type": "directory"}
                    )
                    visit(descriptor, path_parts)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1:
                        raise ValueError("native Harbor evidence contains a hard link")
                    if before.st_size > MAX_NATIVE_FILE_BYTES:
                        raise ValueError("native Harbor evidence file exceeds limit")
                    if before.st_size > MAX_NATIVE_TOTAL_BYTES - total:
                        raise ValueError(
                            "native Harbor evidence total size exceeds limit"
                        )
                    data = _read_fd_bytes(descriptor, before.st_size)
                    after = os.fstat(descriptor)
                    if (
                        before.st_dev,
                        before.st_ino,
                        before.st_mode,
                        before.st_size,
                        before.st_ctime_ns,
                        before.st_mtime_ns,
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_mode,
                        after.st_size,
                        after.st_ctime_ns,
                        after.st_mtime_ns,
                    ):
                        raise ValueError("native Harbor evidence changed while reading")
                    total += len(data)
                    files[logical_path] = data
                    manifest.append(
                        {
                            "mode": mode,
                            "path": logical_path,
                            "sha256": sha256_bytes(data),
                            "size": len(data),
                            "type": "file",
                        }
                    )
                else:
                    raise ValueError("native Harbor evidence contains a special file")
            finally:
                os.close(descriptor)

    try:
        root_metadata = os.fstat(root_fd)
        if stat.S_IMODE(root_metadata.st_mode) != 0o700:
            raise ValueError("production run output is not private mode 0700")
        manifest.append({"mode": 0o700, "path": ".", "type": "directory"})
        visit(root_fd, ())
    finally:
        os.close(root_fd)
    return NativeSnapshot(files=files, manifest=manifest)


def _snapshot_digest(snapshot: NativeSnapshot, path: str) -> str:
    return sha256_bytes(snapshot.read(path))


def _native_run_record(
    snapshot: NativeSnapshot,
    cli_document: dict[str, Any],
    *,
    ordinal: int,
    expected_task_checksum: str,
    expected_task_digest: str,
    expected_agent_name: str = "oracle",
    expected_model_name: str | None = None,
    expected_reward: int = 1,
    require_atif: bool = False,
) -> dict[str, Any]:
    job_prefix = "harbor-job"
    root_entries = [
        (item["path"], item["type"])
        for item in snapshot.manifest
        if item["path"] != "." and "/" not in item["path"]
    ]
    if root_entries != [(job_prefix, "directory")]:
        raise ValueError("production output root differs from one native Harbor job")
    config_path = f"{job_prefix}/config.json"
    lock_path = f"{job_prefix}/lock.json"
    result_path = f"{job_prefix}/result.json"
    config = JobConfig.model_validate_json(snapshot.read(config_path))
    lock = JobLock.model_validate_json(snapshot.read(lock_path))
    result = JobResult.model_validate_json(snapshot.read(result_path))
    if (
        config.job_name != "harbor-job"
        or config.n_attempts != 1
        or config.n_concurrent_trials != 1
        or config.quiet is not True
        or config.environment.type is None
        or config.environment.type.value != "docker"
        or len(config.agents) != 1
        or config.agents[0].name != expected_agent_name
        or config.agents[0].model_name != expected_model_name
        or config.retry.max_retries != 0
        or lock.retry.max_retries != 0
        or lock.n_concurrent_trials != 1
        or len(lock.trials) != 1
        or len(config.tasks) != 1
        or config.tasks[0].path is None
        or config.tasks[0].path.name != "authority-fencing"
    ):
        raise ValueError("native Harbor production config mismatch")
    if (
        result.n_total_trials != 1
        or result.stats.n_completed_trials != 1
        or result.stats.n_errored_trials != 0
        or result.stats.n_retries != 0
        or result.stats.n_cancelled_trials != 0
        or result.stats.n_running_trials != 0
        or result.stats.n_pending_trials != 0
        or result.finished_at is None
    ):
        raise ValueError("native Harbor job outcome mismatch")
    directories = {
        item["path"]
        for item in snapshot.manifest
        if item["type"] == "directory" and item["path"].count("/") == 1
    }
    trial_directories = [
        path
        for path in directories
        if path.startswith(f"{job_prefix}/")
        and all(
            f"{path}/{name}" in snapshot.files
            for name in ("config.json", "lock.json", "result.json")
        )
    ]
    if len(trial_directories) != 1:
        raise ValueError("native Harbor trial directory count mismatch")
    trial_directory = trial_directories[0]
    trial_config_path = f"{trial_directory}/config.json"
    trial_lock_path = f"{trial_directory}/lock.json"
    trial_result_path = f"{trial_directory}/result.json"
    trial_config = TrialConfig.model_validate_json(snapshot.read(trial_config_path))
    trial_lock = TrialLock.model_validate_json(snapshot.read(trial_lock_path))
    trial = TrialResult.model_validate_json(snapshot.read(trial_result_path))
    raw_trial = strict_json(snapshot.read(trial_result_path))
    raw_verifier = (
        raw_trial.get("verifier_result") if isinstance(raw_trial, dict) else None
    )
    raw_rewards = (
        raw_verifier.get("rewards") if isinstance(raw_verifier, dict) else None
    )
    if (
        trial.exception_info is not None
        or trial.step_results is not None
        or trial.started_at is None
        or trial.finished_at is None
        or trial.verifier_environment_mode is None
        or trial.verifier_environment_mode.value != "separate"
        or trial.verifier_result is None
        or type(raw_rewards) is not dict
        or set(raw_rewards) != {"reward"}
        or type(raw_rewards["reward"]) is not int
        or raw_rewards["reward"] != expected_reward
        or trial.task_checksum != expected_task_checksum
        or trial_lock.task.digest != expected_task_digest
        or trial_lock != lock.trials[0]
        or trial.config != trial_config
        or trial_config.agent.name != expected_agent_name
        or trial_config.agent.model_name != expected_model_name
        or trial_config.task.path is None
        or trial_config.task.path.name != "authority-fencing"
    ):
        raise ValueError("native Harbor trial evidence mismatch")
    expected_provider: str | None = None
    expected_native_model: str | None = None
    if expected_model_name is not None:
        expected_provider, expected_native_model = expected_model_name.split("/", 1)
    if (
        trial.agent_info.name != expected_agent_name
        or (expected_model_name is None and trial.agent_info.model_info is not None)
        or (
            expected_model_name is not None
            and (
                trial.agent_info.model_info is None
                or trial.agent_info.model_info.provider != expected_provider
                or trial.agent_info.model_info.name != expected_native_model
            )
        )
    ):
        raise ValueError("native Harbor agent identity mismatch")
    manifest_path = f"{trial_directory}/artifacts/manifest.json"
    manifest_data = strict_json(snapshot.read(manifest_path))
    if not isinstance(manifest_data, list):
        raise ValueError("native artifact manifest root mismatch")
    manifest = [ArtifactManifestEntry.model_validate(item) for item in manifest_data]
    manifest_projection = [
        (item.source, item.destination, item.type, item.status, item.service)
        for item in manifest
    ]
    if manifest_projection != [
        (
            "/logs/artifacts",
            "artifacts/logs/artifacts",
            "directory",
            "empty",
            None,
        ),
        ("/workspace", "artifacts/workspace", "directory", "ok", "main"),
    ]:
        raise ValueError("native artifact manifest provenance mismatch")
    summary = cli_document.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("production CLI binary reward summary mismatch")
    summary_record = SectionRewardSummary.model_validate_json(canonical(summary))
    if (
        summary_record.policy != "binary"
        or summary_record.aggregate_kind != "binary_pass_rate"
        or summary_record.aggregate != str(expected_reward)
        or summary_record.pass_count != expected_reward
        or summary_record.sample_count != 1
        or summary_record.task_count != 1
        or len(summary_record.tasks) != 1
        or summary_record.tasks[0].task_id != "authority-fencing"
        or len(summary_record.trials) != 1
        or summary_record.trials[0].task_id != "authority-fencing"
        or summary_record.trials[0].trial_name != trial.trial_name
        or summary_record.trials[0].value != str(expected_reward)
    ):
        raise ValueError("production CLI binary reward summary mismatch")
    trajectory_path = f"{trial_directory}/agent/trajectory.json"
    trajectory_record: dict[str, Any] | None = None
    if trajectory_path in snapshot.files:
        trajectory = Trajectory.model_validate_json(snapshot.read(trajectory_path))
        metrics = trajectory.final_metrics
        trajectory_record = {
            "agent": {
                "model_name": trajectory.agent.model_name,
                "name": trajectory.agent.name,
                "version": trajectory.agent.version,
            },
            "final_metrics": (
                metrics.model_dump(mode="json") if metrics is not None else None
            ),
            "schema_version": trajectory.schema_version,
            "sha256": _snapshot_digest(snapshot, trajectory_path),
            "step_count": len(trajectory.steps),
        }
    if require_atif and trajectory_record is None:
        raise ValueError("production OpenCode run omitted its ATIF trajectory")
    return {
        "artifact_manifest": {
            "entries": [item.model_dump(mode="json") for item in manifest],
            "sha256": _snapshot_digest(snapshot, manifest_path),
        },
        "cli": {
            "outcome": cli_document["outcome"],
            "reward": cli_document["reward"],
            "schema_version": cli_document["schema_version"],
            "summary": summary_record.model_dump(mode="json"),
        },
        "native": {
            "job": {
                "config_sha256": _snapshot_digest(snapshot, config_path),
                "finished_at": result.finished_at.isoformat(),
                "id": str(result.id),
                "lock_sha256": _snapshot_digest(snapshot, lock_path),
                "result_sha256": _snapshot_digest(snapshot, result_path),
                "started_at": result.started_at.isoformat(),
            },
            "trial": {
                "agent": {
                    "model": (
                        trial.agent_info.model_info.model_dump(mode="json")
                        if trial.agent_info.model_info is not None
                        else None
                    ),
                    "name": trial.agent_info.name,
                    "version": trial.agent_info.version,
                },
                "config_sha256": _snapshot_digest(snapshot, trial_config_path),
                "finished_at": trial.finished_at.isoformat(),
                "id": str(trial.id),
                "lock_sha256": _snapshot_digest(snapshot, trial_lock_path),
                "result_sha256": _snapshot_digest(snapshot, trial_result_path),
                "started_at": trial.started_at.isoformat(),
                "task_checksum": trial.task_checksum,
                "task_digest": trial_lock.task.digest,
                "trial_name": trial.trial_name,
            },
        },
        "output_snapshot": {
            "manifest": snapshot.manifest,
            "manifest_sha256": manifest_digest(snapshot.manifest),
        },
        "ordinal": ordinal,
        "trajectory": trajectory_record,
    }


def execute_ordered_calls(
    repetitions: int, invoke: Callable[[int], dict[str, Any]]
) -> FullRunOutcome:
    records: list[dict[str, Any]] = []
    for ordinal in range(1, repetitions + 1):
        try:
            record = invoke(ordinal)
        except BaseException as error:
            return FullRunOutcome(
                records=records,
                error=safe_error(error),
            )
        if record.get("ordinal") != ordinal:
            return FullRunOutcome(
                records=records, error="production run ordinal mismatch"
            )
        records.append(record)
    return FullRunOutcome(records=records, error=None)


def proof_status(
    *,
    debug: bool,
    source_state: str,
    matrix_ok: bool,
    requested_runs: int | None,
    outcome: FullRunOutcome,
) -> tuple[bool, bool, bool]:
    diagnostic_runs_ok = (
        requested_runs is not None
        and 1 <= requested_runs <= MAX_PROOF_RUNS
        and outcome.error is None
        and len(outcome.records) == requested_runs
    )
    full_runs_ok = requested_runs == MAX_PROOF_RUNS and diagnostic_runs_ok
    admissible = not debug and source_state == "clean" and matrix_ok and full_runs_ok
    return diagnostic_runs_ok, full_runs_ok, admissible


def run_production_proofs(
    repetitions: int,
    *,
    installed_cli: InstalledCLI,
    task_source: Path,
    private_root: Path,
) -> FullRunOutcome:
    temporary_root = private_root / "production"
    temporary_root.mkdir(mode=0o700)
    project, config_root = _write_proof_project(temporary_root, task_source=task_source)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        expected_task_checksum = Task(project / "tasks/authority-fencing").checksum
    expected_task_digest = (
        "sha256:"
        + Packager.compute_content_hash(project / "tasks/authority-fencing")[0]
    )

    def invoke(ordinal: int) -> dict[str, Any]:
        output = temporary_root / f"run-{ordinal}"
        command = [
            str(installed_cli.executable),
            "run",
            "systems-design",
            "--profile",
            PROOF_PROFILE,
            "--output",
            str(output),
            "--json",
        ]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("AWS_", "TIGRIS_"))
            and key.upper() not in {"BOTO_CONFIG", "BOTOCORE_TCP_KEEPALIVE"}
        }
        environment["XDG_CONFIG_HOME"] = str(config_root)
        result = _bounded_command(
            command,
            cwd=project,
            env=environment,
            timeout=PRODUCTION_RUN_TIMEOUT_SECONDS,
        )
        if result.stderr:
            raise ValueError("production CLI stderr was not empty")
        document = strict_json(result.stdout)
        if (
            not isinstance(document, dict)
            or result.stdout != (canonical(document) + "\n").encode()
        ):
            raise ValueError("production CLI output was not canonical JSON")
        expected_keys = {
            "job_directory",
            "outcome",
            "reward",
            "schema_version",
            "summary",
        }
        if set(document) != expected_keys:
            raise ValueError("production CLI JSON schema changed")
        expected_job = output / "harbor-job"
        if (
            document["job_directory"] != str(expected_job)
            or document["schema_version"] != 1
            or document["outcome"] != "succeeded"
            or document["reward"] != "1"
        ):
            raise ValueError("production CLI result mismatch")
        native = snapshot_native_output(output)
        record = _native_run_record(
            native,
            document,
            ordinal=ordinal,
            expected_task_checksum=expected_task_checksum,
            expected_task_digest=expected_task_digest,
        )
        record["cli"]["canonical_sha256"] = sha256_bytes(result.stdout[:-1])
        record["containment"] = result.containment
        return record

    return execute_ordered_calls(repetitions, invoke)


def _output_directory_flags() -> int:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if os.name != "posix" or any(not hasattr(os, name) for name in required):
        raise RuntimeError("proof output requires POSIX no-follow openat")
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _output_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _verify_trusted_output_ancestor(metadata: os.stat_result) -> None:
    mode = metadata.st_mode
    writable = bool(stat.S_IMODE(mode) & 0o022)
    root_sticky_world_writable = bool(
        metadata.st_uid == 0 and mode & stat.S_ISVTX and mode & stat.S_IWOTH
    )
    if (
        not stat.S_ISDIR(mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or (writable and not root_sticky_world_writable)
    ):
        raise PermissionError(
            "proof output ancestor must be owned by root or the current euid and "
            "protected from group/world replacement"
        )


def _verify_trusted_output_parent(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise PermissionError(
            "proof output parent must be owned by the current euid and private"
        )


def _verify_output_anchors(authority: ProofOutputAuthority) -> None:
    for descriptor, identity in authority.parents[:-1]:
        held = os.fstat(descriptor)
        _verify_trusted_output_ancestor(held)
        if _output_identity(held) != identity:
            raise OSError("proof output parent identity changed")
    final_descriptor, final_identity = authority.parents[-1]
    final_parent = os.fstat(final_descriptor)
    _verify_trusted_output_parent(final_parent)
    if _output_identity(final_parent) != final_identity:
        raise OSError("proof output parent identity changed")
    for parent_fd, child_fd, name, identity in authority.anchors:
        held = os.fstat(child_fd)
        try:
            visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            raise OSError("proof output parent identity changed") from error
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or _output_identity(held) != identity
            or _output_identity(visible) != identity
        ):
            raise OSError("proof output parent identity changed")


def open_proof_output_authority(path: Path) -> ProofOutputAuthority:
    selected = path.expanduser()
    if not selected.name or selected.name in {".", ".."}:
        raise ValueError("proof output must name a file")
    flags = _output_directory_flags()
    descriptors: list[int] = []
    anchors: list[tuple[int, int, str, tuple[int, int, int, int]]] = []
    if selected.is_absolute():
        current = os.open(os.sep, flags)
        components = selected.parts[1:-1]
    else:
        current = os.open(".", flags)
        components = selected.parts[:-1]
    descriptors.append(current)
    try:
        current_metadata = os.fstat(current)
        parents = [(current, _output_identity(current_metadata))]
        for component in components:
            if component in {"", ".", ".."}:
                raise ValueError("proof output path contains an unsupported component")
            child = os.open(component, flags, dir_fd=current)
            descriptors.append(child)
            child_metadata = os.fstat(child)
            identity = _output_identity(child_metadata)
            anchors.append((current, child, component, identity))
            parents.append((child, identity))
            current = child
        authority = ProofOutputAuthority(
            parent_fd=current,
            name=selected.name,
            descriptors=tuple(descriptors),
            anchors=tuple(anchors),
            parents=tuple(parents),
        )
        _verify_output_anchors(authority)
        try:
            existing = os.stat(selected.name, dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            kind = "symlink" if stat.S_ISLNK(existing.st_mode) else "existing path"
            raise ValueError(f"proof output refuses {kind}: {selected.name}")
        return authority
    except BaseException:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
        raise


def _read_proof_fd(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(size - offset, 1 << 20), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    if offset != size or os.pread(descriptor, 1, offset):
        raise OSError("proof output bytes changed")
    return b"".join(chunks)


def _verify_proof_file(
    authority: ProofOutputAuthority,
    descriptor: int,
    data: bytes,
    identity: tuple[int, int, int, int],
) -> None:
    before = os.fstat(descriptor)
    actual = _read_proof_fd(descriptor, len(data))
    held = os.fstat(descriptor)
    try:
        visible = os.stat(
            authority.name, dir_fd=authority.parent_fd, follow_symlinks=False
        )
    except FileNotFoundError as error:
        raise OSError("proof output name disappeared") from error
    if (
        not stat.S_ISREG(held.st_mode)
        or _output_identity(before) != identity
        or not stat.S_ISREG(visible.st_mode)
        or _output_identity(held) != identity
        or _output_identity(visible) != identity
        or stat.S_IMODE(held.st_mode) != 0o600
        or held.st_uid != os.geteuid()
        or held.st_size != len(data)
        or held.st_nlink != 1
        or actual != data
    ):
        raise OSError("proof output identity or bytes changed")


def _unlink_created_proof(
    authority: ProofOutputAuthority,
    identity: tuple[int, int, int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        visible = os.stat(
            authority.name, dir_fd=authority.parent_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        return
    if _output_identity(visible) == identity:
        os.unlink(authority.name, dir_fd=authority.parent_fd)


def write_exclusive_proof(authority: ProofOutputAuthority, data: bytes) -> None:
    if len(data) > 8 << 20:
        raise ValueError("proof output exceeds limit")
    _verify_output_anchors(authority)
    file_fd: int | None = None
    created = False
    identity: tuple[int, int, int, int] | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        file_fd = os.open(authority.name, flags, 0o600, dir_fd=authority.parent_fd)
        created = True
        os.fchmod(file_fd, 0o600)
        identity = _output_identity(os.fstat(file_fd))
        view = memoryview(data)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        _verify_output_anchors(authority)
        _verify_proof_file(authority, file_fd, data, identity)
        os.fsync(authority.parent_fd)
        _verify_output_anchors(authority)
        _verify_proof_file(authority, file_fd, data, identity)
        os.close(file_fd)
        file_fd = None
        try:
            visible = os.stat(
                authority.name, dir_fd=authority.parent_fd, follow_symlinks=False
            )
        except FileNotFoundError as error:
            raise OSError("proof output identity changed during close") from error
        if _output_identity(visible) != identity:
            raise OSError("proof output identity changed during close")
    except BaseException:
        if file_fd is not None:
            with suppress(OSError):
                os.close(file_fd)
            file_fd = None
        if created:
            with suppress(OSError):
                _unlink_created_proof(authority, identity)
            with suppress(OSError):
                os.fsync(authority.parent_fd)
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)


def evidence_argv(argv: list[str]) -> list[str]:
    normalized: list[str] = []
    replace_next = False
    for value in argv:
        if replace_next:
            normalized.append("<exclusive-proof-output>")
            replace_next = False
        elif value == "--output":
            normalized.append(value)
            replace_next = True
        elif value.startswith("--output="):
            normalized.append("--output=<exclusive-proof-output>")
        else:
            normalized.append(value)
    if replace_next:
        raise ValueError("proof output argument is missing its path")
    return normalized


def remove_admission_image(image: str) -> None:
    subprocess.run(  # nosec B603
        [DOCKER, "image", "rm", "--force", image],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


def _matrix_candidates(
    source: str, mutants: list[dict[str, Any]], task: Path
) -> list[tuple[str, str | None, str, str | None]]:
    candidates: list[tuple[str, str | None, str, str | None]] = [
        ("gold", None, source, None),
        ("no-op", None, (task / "authority.py").read_text(), None),
    ]
    for mutant in mutants:
        count = source.count(mutant["old"])
        expected_count = count if mutant.get("replace_all") is True else 1
        if count != expected_count or count == 0:
            raise ValueError(f"mutant replacement count mismatch: {mutant['id']}")
        candidates.append(
            (
                mutant["id"],
                mutant["gate_id"],
                source.replace(
                    mutant["old"],
                    mutant["new"],
                    -1 if mutant.get("replace_all") is True else 1,
                ),
                None,
            )
        )
    broad = source.replace(
        "def execute(args: argparse.Namespace) -> dict[str, object]:\n",
        "def execute(args: argparse.Namespace) -> dict[str, object]:\n"
        "    raise Rejected('broad attribution probe')\n",
        1,
    )
    candidates.append(
        ("attribution-probe-broad-mutant", "single-authority", broad, None)
    )
    candidates.extend(
        [
            (
                "exploit-hardcoded-public",
                None,
                (task / "authority.py").read_text(),
                None,
            ),
            ("exploit-fake-reward", None, source, "fake-reward"),
            (
                "exploit-hidden-discovery",
                None,
                exploit_source("hidden-discovery", source),
                None,
            ),
            (
                "exploit-output-forgery",
                None,
                exploit_source("output-forgery", source),
                None,
            ),
            ("exploit-symlink", None, source, "symlink"),
            ("exploit-special-file", None, source, "special-file"),
            ("exploit-extra-executable", None, source, "extra-executable"),
            (
                "exploit-background-process",
                None,
                exploit_source("background-process", source),
                None,
            ),
        ]
    )
    return candidates


def _run_matrix(
    image: str,
    task: Path,
    candidates: list[tuple[str, str | None, str, str | None]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="authority-admission-") as directory:
        root = Path(directory)
        for index, (name, gate, candidate, tree_mutation) in enumerate(candidates):
            case_root = root / f"case-{index}"
            case_root.mkdir()
            workspace = stage(case_root, candidate, task=task)
            mutate_tree(workspace, tree_mutation)
            reward, diagnostics = run_verifier(image, workspace, case_root / "output")
            claim_evidence = concurrent_claim_evidence(
                diagnostics, required=reward == 1
            )
            gates = diagnostics.get("gates", {})
            expected_reward = int(name == "gold")
            attribution_admitted = (
                mutant_attribution_passes(gates, gate) if gate is not None else None
            )
            passed = (
                reward == 0 and attribution_admitted is False
                if name == "attribution-probe-broad-mutant"
                else reward == expected_reward
                and (gate is None or attribution_admitted is True)
            )
            entries.append(
                {
                    "attribution_admitted": attribution_admitted,
                    "concurrent_claim": claim_evidence,
                    "expected_reward": expected_reward,
                    "gate_vector": gates,
                    "intended_gate": gate,
                    "intended_gate_value": gates.get(gate)
                    if gate is not None
                    else None,
                    "name": name,
                    "passed": int(passed),
                    "reward": reward,
                }
            )
    return entries


def _failure_evidence(args: argparse.Namespace, error: BaseException) -> dict[str, Any]:
    return {
        "admissible": False,
        "debug": args.debug,
        "diagnostic_runs_ok": False,
        "error": safe_error(error),
        "full_run_count": 0,
        "full_runs": [],
        "full_runs_ok": False,
        "matrix_ok": False,
        "ok": False,
        "proof_repetitions": args.proof_runs,
        "schema_version": 3,
        "task_id": "systems-design/authority-fencing",
    }


def _emit_evidence(evidence: dict[str, Any]) -> None:
    sys.stdout.buffer.write((canonical(evidence) + "\n").encode())
    sys.stdout.buffer.flush()


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--proof-runs",
        nargs="?",
        const=3,
        type=int,
        metavar="N",
        help="Run the matrix and N production CLI proofs (default: 3).",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.debug and (args.image is not None or args.skip_build):
        parser.error("--image and --skip-build require explicit --debug mode")
    if args.image is not None and not args.skip_build:
        parser.error("--image requires --skip-build")
    if args.proof_runs is not None and not 1 <= args.proof_runs <= MAX_PROOF_RUNS:
        parser.error(f"--proof-runs must be between 1 and {MAX_PROOF_RUNS}")
    if args.output is not None and args.proof_runs is None:
        parser.error("--output requires --proof-runs")
    if args.output is not None and args.proof_runs != MAX_PROOF_RUNS:
        parser.error(f"--output requires exactly {MAX_PROOF_RUNS} proof runs")
    if args.debug and args.output is not None:
        parser.error("debug mode cannot write proof evidence")
    return args


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_arguments(effective_argv)

    output_authority: ProofOutputAuthority | None = None
    image: str | None = None
    image_registered = False
    try:
        if args.output is not None:
            output_authority = open_proof_output_authority(args.output)
        with tempfile.TemporaryDirectory(prefix="authority-source-") as directory:
            private_root = Path(directory)
            snapshot = (
                create_debug_source_snapshot(private_root)
                if args.debug
                else create_clean_source_snapshot(private_root)
            )
            task = snapshot.task
            tests = snapshot.tests
            initial_source_manifest = snapshot_source_manifest(snapshot)
            hidden_case_input_sha256 = validate_hidden_case_binding_for(task)
            mutants = tomllib.loads((tests / "mutants.toml").read_text())["mutants"]
            installed_cli = install_snapshot_cli(snapshot, private_root)
            installed_versions = {
                item["name"].lower(): item["version"]
                for item in installed_cli.attestation["distribution"][
                    "installed_distributions"
                ]
            }
            version_values = {
                "docker": docker_versions(cwd=snapshot.root),
                "harbor": installed_versions["harbor"],
                "python": installed_cli.attestation["python"]["version"],
                "tetrabench": installed_versions["tetrabench"],
            }
            context = private_root / "verifier-context"
            tests_manifest = tree_manifest(tests)
            copied_manifest = _copy_verified_tree(tests, context)
            context_manifest = tree_manifest(context)
            if copied_manifest != tests_manifest or context_manifest != tests_manifest:
                raise ValueError(
                    "task tests manifest differs from verifier build context"
                )
            context_sha256 = manifest_digest(context_manifest)
            if args.debug and args.skip_build:
                image = args.image or DEFAULT_IMAGE
                run_attestation = {
                    "build_command": None,
                    "build_nonce": None,
                    "image_id": None,
                    "mode": "caller-controlled-debug-image",
                    "repo_digests": [],
                    "tag": image,
                }
            else:
                image, run_attestation = build_admission_image(
                    context, context_sha256, cwd=snapshot.root
                )
                atexit.register(remove_admission_image, image)
                image_registered = True
            source = reference_source(task)
            source_paths = tuple(snapshot.root / path for path in SOURCE_RELATIVE_PATHS)
            subject = admission_subject(
                hidden_case_input_sha256=hidden_case_input_sha256,
                mutants=mutants,
                tool_version_values=version_values,
                verifier_context_manifest=context_manifest,
                verifier_context_sha256=context_sha256,
                source_paths=source_paths,
                source_root=snapshot.root,
                task=task,
                tests=tests,
                source_revision=snapshot.revision,
                source_state=snapshot.source_state,
            )
            if subject["source_manifest"] != initial_source_manifest:
                raise ValueError("source snapshot changed before matrix execution")
            candidates = _matrix_candidates(source, mutants, task)
            if (
                matrix_projection(candidates)
                != subject["matrix_contract"]["candidates"]
            ):
                raise ValueError("executed matrix differs from admission subject")
            entries = _run_matrix(image, task, candidates)
            matrix_ok = all(item["passed"] == 1 for item in entries)
            gold_claim = next(
                item["concurrent_claim"] for item in entries if item["name"] == "gold"
            )
            if gold_claim is None:
                raise ValueError("gold admission evidence omits concurrent claim")
            outcome = (
                run_production_proofs(
                    args.proof_runs,
                    installed_cli=installed_cli,
                    task_source=task,
                    private_root=private_root,
                )
                if args.proof_runs is not None and matrix_ok
                else FullRunOutcome(
                    records=[],
                    error=(
                        "matrix failed" if not matrix_ok else "proof runs not requested"
                    ),
                )
            )
            diagnostic_runs_ok, full_runs_ok, admissible = proof_status(
                debug=args.debug,
                source_state=snapshot.source_state,
                matrix_ok=matrix_ok,
                requested_runs=args.proof_runs,
                outcome=outcome,
            )
            if snapshot_source_manifest(snapshot) != initial_source_manifest:
                raise ValueError("source snapshot changed during proof execution")
            evidence = {
                "admissible": admissible,
                "candidate_count": len(entries),
                "cli_distribution": installed_cli.attestation,
                "command": [
                    "python",
                    "tools/run_authority_fencing_admission.py",
                    *evidence_argv(effective_argv),
                ],
                "concurrent_claim": gold_claim,
                "debug": args.debug,
                "diagnostic_runs_ok": diagnostic_runs_ok,
                "entries": entries,
                "full_run_count": len(outcome.records),
                "full_run_error": outcome.error,
                "full_runs": outcome.records,
                "full_runs_ok": full_runs_ok,
                "hidden_case_input_sha256": hidden_case_input_sha256,
                "matrix_ok": matrix_ok,
                "non_admission_reason": (
                    "explicit dirty debug mode"
                    if args.debug
                    else None
                    if admissible
                    else "diagnostic run count"
                    if diagnostic_runs_ok and args.proof_runs != MAX_PROOF_RUNS
                    else "incomplete proof"
                ),
                "ok": admissible,
                "proof_repetitions": args.proof_runs,
                "run_attestation": run_attestation,
                "schema_version": 3,
                "source_snapshot": {
                    "archive_sha256": snapshot.archive_sha256,
                    "manifest": subject["source_manifest"],
                    "manifest_sha256": subject["source_manifest_sha256"],
                    "mode": snapshot.mode,
                    "revision": snapshot.revision,
                    "state": snapshot.source_state,
                },
                "subject": subject,
                "subject_sha256": sha256_bytes(canonical(subject).encode()),
                "task_id": "systems-design/authority-fencing",
            }
            encoded = (canonical(evidence) + "\n").encode()
            if output_authority is not None and admissible:
                write_exclusive_proof(output_authority, encoded)
            _emit_evidence(evidence)
            return 0 if admissible else 1
    except BaseException as error:
        _emit_evidence(_failure_evidence(args, error))
        return 1
    finally:
        if image is not None and image_registered:
            remove_admission_image(image)
            atexit.unregister(remove_admission_image)
        if output_authority is not None:
            output_authority.close()


if __name__ == "__main__":
    raise SystemExit(main())
