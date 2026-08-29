#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import resource
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

GIT = "/usr/bin/git"
PYTHON = sys.executable
RUNNER_UID = 65532
RUNNER_GID = 65532
OID_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{15,63}$")
FORBIDDEN_NAMES = {
    ".forge",
    ".gitmodules",
    "diagnostics.json",
    "events.jsonl",
    "forge-export",
    "reward.txt",
    "snapshot.json",
}
EXPECTED_CONFIG = {
    "core": {
        "bare": "false",
        "filemode": "true",
        "logallrefupdates": "true",
        "repositoryformatversion": "0",
    },
    "user": {"email": "fixture@example.invalid", "name": "Fixture"},
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def strict_json(data: bytes, *, label: str, newline: bool = True) -> Any:
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if data != canonical(value) + (b"\n" if newline else b""):
        raise ValueError(f"non-canonical {label} JSON")
    return value


def validate_tree(root: Path) -> None:
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"missing or unsafe artifact: {root}")
    count = 0
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(current) / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"artifact contains symlink: {path}")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise ValueError(f"artifact contains special file: {path}")
            count += 1
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
            if count > 2000 or metadata.st_size > 32 * 1024 * 1024:
                raise ValueError("artifact budget exceeded")
            if total > 256 * 1024 * 1024:
                raise ValueError("artifact budget exceeded")


def _validate_initial(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "base_oid",
        "base_ref",
        "pull_request",
        "schema_version",
    }:
        raise ValueError("initial state schema mismatch")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["base_ref"] != "main"
        or value["pull_request"] is not None
        or type(value["base_oid"]) is not str
        or not OID_RE.fullmatch(value["base_oid"])
    ):
        raise ValueError("initial state value mismatch")


def _validate_transition(transition: Any, *, expected_type: str) -> None:
    if not isinstance(transition, dict) or set(transition) != {
        "base",
        "head",
        "head_oid",
        "request_id",
        "schema_version",
        "type",
    }:
        raise ValueError("transition schema mismatch")
    if (
        type(transition["schema_version"]) is not int
        or transition["schema_version"] != 1
        or transition["type"] != expected_type
        or transition["base"] != "main"
        or transition["head"] != "feature"
        or type(transition["head_oid"]) is not str
        or not OID_RE.fullmatch(transition["head_oid"])
        or type(transition["request_id"]) is not str
        or not REQUEST_ID_RE.fullmatch(transition["request_id"])
    ):
        raise ValueError("transition value mismatch")


def validate_forge(export: Path, expected_initial: dict[str, Any]) -> dict[str, Any]:
    validate_tree(export)
    expected_names = {"events.jsonl", "manifest.json", "snapshot.json"}
    actual_names = {path.name for path in export.iterdir()}
    if actual_names != expected_names:
        raise ValueError("forge export paths mismatch")
    files = {name: (export / name).read_bytes() for name in expected_names}
    manifest = strict_json(files["manifest.json"], label="manifest")
    if not isinstance(manifest, dict) or set(manifest) != {"files", "schema_version"}:
        raise ValueError("forge manifest schema mismatch")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or not isinstance(manifest["files"], dict)
        or set(manifest["files"]) != {"events.jsonl", "snapshot.json"}
        or any(
            type(value) is not str or not HASH_RE.fullmatch(value)
            for value in manifest["files"].values()
        )
    ):
        raise ValueError("forge manifest value mismatch")
    expected_manifest = {
        "files": {
            "events.jsonl": digest(files["events.jsonl"]),
            "snapshot.json": digest(files["snapshot.json"]),
        },
        "schema_version": 1,
    }
    if manifest != expected_manifest:
        raise ValueError("forge export manifest hash mismatch")
    snapshot = strict_json(files["snapshot.json"], label="snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "current",
        "event_count",
        "head_hash",
        "initial",
        "schema_version",
        "sealed",
        "terminal_state",
    }:
        raise ValueError("forge snapshot schema mismatch")
    if (
        snapshot["initial"] != expected_initial
        or snapshot["sealed"] is not True
        or snapshot["terminal_state"] != "pr_submitted"
        or type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != 1
        or type(snapshot["event_count"]) is not int
        or snapshot["event_count"] != 2
        or type(snapshot["head_hash"]) is not str
        or not HASH_RE.fullmatch(snapshot["head_hash"])
    ):
        raise ValueError("forge snapshot value mismatch")
    if not files["events.jsonl"].endswith(b"\n"):
        raise ValueError("events JSONL lacks final newline")
    lines = files["events.jsonl"].splitlines(keepends=True)
    events = [strict_json(line, label="event") for line in lines]
    if len(events) != snapshot["event_count"]:
        raise ValueError("forge event count mismatch")
    previous = digest(canonical(expected_initial))
    request_ids: set[str] = set()
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict) or set(event) != {
            "event_hash",
            "prev_hash",
            "sequence",
            "transition",
        }:
            raise ValueError("forge event schema mismatch")
        if (
            type(event["sequence"]) is not int
            or event["sequence"] != sequence
            or type(event["prev_hash"]) is not str
            or event["prev_hash"] != previous
            or type(event["event_hash"]) is not str
            or not HASH_RE.fullmatch(event["event_hash"])
        ):
            raise ValueError("forge event chain mismatch")
        _validate_transition(
            event["transition"],
            expected_type=(
                "pull_request.opened" if sequence == 1 else "pull_request.submitted"
            ),
        )
        request_id = event["transition"]["request_id"]
        if request_id in request_ids:
            raise ValueError("forge request_id replay")
        request_ids.add(request_id)
        core = {
            "prev_hash": event["prev_hash"],
            "sequence": event["sequence"],
            "transition": event["transition"],
        }
        if event["event_hash"] != digest(canonical(core)):
            raise ValueError("forge event hash mismatch")
        previous = event["event_hash"]
    opened, submitted = (event["transition"] for event in events)
    if any(opened[field] != submitted[field] for field in ("base", "head", "head_oid")):
        raise ValueError("final transition changed pull request state")
    if snapshot["head_hash"] != previous:
        raise ValueError("forge snapshot head hash mismatch")
    if snapshot["current"] != {"pull_request": submitted}:
        raise ValueError("forge current state mismatch")
    return submitted


def _parse_local_config(git_dir: Path) -> None:
    config_path = git_dir / "config"
    metadata = config_path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("unsafe Git local config")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(config_path.read_text(encoding="utf-8", errors="strict"))
    except (configparser.Error, UnicodeError) as exc:
        raise ValueError("invalid Git local config") from exc
    actual = {
        section.lower(): dict(parser.items(section)) for section in parser.sections()
    }
    if actual != EXPECTED_CONFIG:
        raise ValueError("submission Git config mismatch")


def inspect_git_structure(workspace: Path) -> None:
    git_dir = workspace / ".git"
    metadata = git_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(".git must be a real directory")
    _parse_local_config(git_dir)
    top_level = {path.name for path in git_dir.iterdir()}
    allowed_top_level = {
        "COMMIT_EDITMSG",
        "HEAD",
        "branches",
        "config",
        "description",
        "index",
        "info",
        "logs",
        "objects",
        "refs",
    }
    if top_level not in (
        allowed_top_level,
        allowed_top_level - {"branches"},
    ):
        raise ValueError("unexpected Git administrative path")
    branches = git_dir / "branches"
    if branches.exists() and any(branches.iterdir()):
        raise ValueError("unexpected Git branches mechanism")
    forbidden = (
        git_dir / "commondir",
        git_dir / "gitdir",
        git_dir / "info/grafts",
        git_dir / "objects/info/alternates",
        git_dir / "shallow",
        git_dir / "worktrees",
        git_dir / "modules",
        git_dir / "refs/replace",
        workspace / ".gitmodules",
    )
    if any(path.exists() or path.is_symlink() for path in forbidden):
        raise ValueError("forbidden Git repository mechanism")
    hooks = git_dir / "hooks"
    if hooks.exists() and any(hooks.iterdir()):
        raise ValueError("Git hooks are forbidden")
    if {path.name for path in (git_dir / "info").iterdir()} != {"exclude"}:
        raise ValueError("unexpected Git info mechanism")
    for path in (git_dir / "objects").iterdir():
        if path.name in {"info", "pack"}:
            if any(path.iterdir()):
                raise ValueError("packed or indirect Git objects are forbidden")
            continue
        if not path.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", path.name):
            raise ValueError("unexpected Git object directory")
        if any(
            not item.is_file() or not re.fullmatch(r"[0-9a-f]{38}", item.name)
            for item in path.iterdir()
        ):
            raise ValueError("unexpected Git loose object")
    log_files = {
        path.relative_to(git_dir / "logs").as_posix()
        for path in (git_dir / "logs").rglob("*")
        if path.is_file()
    }
    if log_files != {"HEAD", "refs/heads/feature", "refs/heads/main"}:
        raise ValueError("unexpected Git reflog structure")
    head = (git_dir / "HEAD").read_bytes()
    if head != b"ref: refs/heads/feature\n":
        raise ValueError("Git HEAD is not canonical")
    for ref in ("main", "feature"):
        value = (git_dir / "refs/heads" / ref).read_bytes()
        if not re.fullmatch(rb"[0-9a-f]{40}\n", value):
            raise ValueError("Git loose ref is invalid")
    refs = [
        path.relative_to(git_dir / "refs").as_posix()
        for path in (git_dir / "refs").rglob("*")
        if path.is_file()
    ]
    if sorted(refs) != ["heads/feature", "heads/main"]:
        raise ValueError("submission refs mismatch")


def git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def run_git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        [
            GIT,
            "-c",
            f"safe.directory={workspace}",
            "-c",
            f"safe.directory={workspace / '.git'}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.sshCommand=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "protocol.file.allow=always",
            "-C",
            str(workspace),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=git_environment(),
    )
    return result.stdout.strip()


def _runner_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))


RUNNER_RESOURCE_BOUNDS = {
    "cpu_seconds": 5,
    "file_bytes": 1024 * 1024,
    "nofile": 32,
    "nproc_rlimit": 16,
    "timeout_seconds": 10,
}


def execute_product(source: Path) -> tuple[str, dict[str, int]]:
    with tempfile.TemporaryDirectory(prefix="tetrabench-runner-") as temporary:
        scratch = Path(temporary)
        target = scratch / "app.py"
        shutil.copyfile(source, target, follow_symlinks=False)
        os.chmod(target, 0o400)
        runner_uid = os.geteuid()
        runner_gid = os.getegid()
        command = [PYTHON, "-I", "-B", "app.py"]
        if os.geteuid() == 0:
            runner_uid = RUNNER_UID
            runner_gid = RUNNER_GID
            os.chown(scratch, runner_uid, runner_gid)
            os.chown(target, runner_uid, runner_gid)
            command = [
                "/usr/bin/setpriv",
                f"--reuid={runner_uid}",
                f"--regid={runner_gid}",
                "--clear-groups",
                "--no-new-privs",
                *command,
            ]
        result = subprocess.run(
            command,
            cwd=scratch,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env={
                "HOME": str(scratch),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONHASHSEED": "0",
            },
            preexec_fn=_runner_limits,
        )
        return result.stdout, {"gid": runner_gid, "uid": runner_uid}


def validate_repository(workspace: Path, transition: dict[str, Any]) -> dict[str, int]:
    validate_tree(workspace)
    inspect_git_structure(workspace)
    for path in workspace.rglob("*"):
        if (
            path.name in FORBIDDEN_NAMES
            or path.relative_to(workspace).parts[0] == "tests"
        ):
            raise ValueError(f"forbidden agent file: {path.relative_to(workspace)}")
    _validate_transition(transition, expected_type="pull_request.submitted")
    head_oid = transition["head_oid"]
    run_git(workspace, "fsck", "--strict", "--full", "--no-reflogs")
    run_git(workspace, "cat-file", "-e", f"{head_oid}^{{commit}}")
    if run_git(workspace, "rev-parse", "--verify", "refs/heads/feature") != head_oid:
        raise ValueError("transition head does not match feature")
    base_oid = run_git(workspace, "rev-parse", "--verify", "refs/heads/main")
    if base_oid != "40cf7d08fd09619514bab16351e1e926fde8698c":
        raise ValueError("base ref changed")
    if run_git(workspace, "rev-list", "--count", "main..feature") != "1":
        raise ValueError("submission must contain one commit")
    if run_git(workspace, "rev-parse", f"{head_oid}^") != base_oid:
        raise ValueError("submission parent mismatch")
    if (
        run_git(workspace, "diff", "--name-only", "--no-ext-diff", "main..feature")
        != "app.py"
    ):
        raise ValueError("submission is not focused")
    if run_git(workspace, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("submission worktree is dirty")
    with tempfile.TemporaryDirectory(prefix="tetrabench-clone-") as temporary:
        bundle = Path(temporary) / "submission.bundle"
        run_git(workspace, "bundle", "create", str(bundle), "--all")
        clone = Path(temporary) / "clone"
        subprocess.run(
            [
                GIT,
                "-c",
                f"safe.directory={workspace}",
                "-c",
                f"safe.directory={workspace / '.git'}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--quiet",
                "--no-hardlinks",
                str(bundle),
                str(clone),
            ],
            check=True,
            timeout=15,
            env=git_environment(),
        )
        run_git(clone, "checkout", "--quiet", head_oid)
        run_git(clone, "fsck", "--strict", "--full", "--no-reflogs")
        stdout, runner = execute_product(clone / "app.py")
        if stdout != "hello, tetrabench\n":
            raise ValueError("product behavior mismatch")
    return runner


def _network_probe(kind: str, host: str) -> dict[str, Any]:
    try:
        if kind == "dns":
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        else:
            with socket.create_connection((host, 443), timeout=1) as stream:
                stream.sendall(b"\x16\x03\x01\x00\x00")
                if not stream.recv(1):
                    raise ConnectionError("egress closed")
    except OSError as exc:
        return {"blocked": True, "error": type(exc).__name__, "kind": kind}
    raise ValueError(f"verifier network probe unexpectedly succeeded: {kind}")


def runtime_evidence(
    runner: dict[str, int], *, require_no_network: bool
) -> dict[str, Any]:
    probes = (
        [
            _network_probe("dns", "example.com"),
            _network_probe("direct-ip-tcp", "1.1.1.1"),
            _network_probe("hostname-tcp", "example.com"),
        ]
        if require_no_network
        else [{"blocked": "not-probed-outside-runtime", "kind": "unit"}]
    )
    mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    task_mounts = [
        line.split()[4]
        for line in mounts
        if line.split()[4].startswith(("/workspace/", "/forge/"))
        and line.split()[4] not in {"/workspace/repo", "/forge/export"}
    ]
    if task_mounts:
        raise ValueError("verifier exposes an undeclared task-side mount")
    socket_paths = [
        path
        for path in (
            "/var/run/docker.sock",
            "/run/docker.sock",
            "/run/containerd/containerd.sock",
        )
        if Path(path).exists()
    ]
    if require_no_network and socket_paths:
        raise ValueError("verifier exposes a container runtime socket")
    cgroup = Path("/sys/fs/cgroup")
    cpu = (
        (cgroup / "cpu.max").read_text().strip()
        if (cgroup / "cpu.max").is_file()
        else "unavailable"
    )
    memory = (
        (cgroup / "memory.max").read_text().strip()
        if (cgroup / "memory.max").is_file()
        else "unavailable"
    )
    return {
        "cgroup": {"cpu_max": cpu, "memory_max": memory, "pid_guarantee": "none"},
        "mounts": {
            "artifact_paths": [
                path
                for path in ("/workspace/repo", "/forge/export")
                if Path(path).exists()
            ],
            "docker_socket": bool(socket_paths),
            "mount_count": len(mounts),
            "task_side_volumes": False,
        },
        "network_probes": probes,
        "orchestrator": {"gid": os.getegid(), "uid": os.geteuid()},
        "runner": runner,
        "runner_resource_bounds": RUNNER_RESOURCE_BOUNDS,
    }


def verify_submission(
    workspace: Path,
    forge_export: Path,
    artifact_contract_path: Path,
    expected_initial_path: Path,
    baked_marker: Path,
) -> dict[str, Any]:
    if baked_marker.read_bytes() != b"baked-verifier-source\n":
        raise ValueError("verifier source is not baked into a clean image")
    if not Path("/tests/verify.py").is_file() and baked_marker == Path(
        "/opt/tetrabench-verifier-source"
    ):
        raise ValueError("hidden verifier source is absent")
    artifact_contract = strict_json(
        artifact_contract_path.read_bytes(), label="artifact contract"
    )
    if (
        artifact_contract
        != {
            "artifacts": [
                {"kind": "git-worktree", "source": str(workspace)},
                {"kind": "sealed-forge-export", "source": str(forge_export)},
            ],
            "schema_version": 1,
        }
        or type(artifact_contract.get("schema_version")) is not int
    ):
        raise ValueError("artifact contract or expected paths mismatch")
    expected_initial = strict_json(
        expected_initial_path.read_bytes(), label="expected initial"
    )
    _validate_initial(expected_initial)
    transition = validate_forge(forge_export, expected_initial)
    runner = validate_repository(workspace, transition)
    return {
        "checks": ["artifact", "forge", "git", "clean-clone", "product", "runtime"],
        "runtime": runtime_evidence(
            runner,
            require_no_network=baked_marker == Path("/opt/tetrabench-verifier-source"),
        ),
    }


def _protect_from_runner(paths: list[Path]) -> None:
    if os.geteuid() != 0:
        return
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--forge-export", type=Path, required=True)
    parser.add_argument("--artifact-contract", type=Path, required=True)
    parser.add_argument("--expected-initial", type=Path, required=True)
    parser.add_argument("--baked-marker", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--reward", type=Path, required=True)
    args = parser.parse_args()
    args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
    _protect_from_runner(
        [
            Path("/tests"),
            Path("/artifacts"),
            args.forge_export,
            args.workspace,
            args.diagnostics.parent,
        ]
    )
    reward = 0
    try:
        diagnostics = {
            "ok": True,
            **verify_submission(
                args.workspace,
                args.forge_export,
                args.artifact_contract,
                args.expected_initial,
                args.baked_marker,
            ),
        }
        reward = 1
    except Exception as exc:
        diagnostics = {"error": str(exc), "ok": False}
    args.diagnostics.write_bytes(canonical(diagnostics) + b"\n")
    args.reward.write_text(f"{reward}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
