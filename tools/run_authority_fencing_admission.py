#!/usr/bin/env python3
"""Run the bounded authority-fencing gold, no-op, mutant, and exploit matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
import tomllib
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
TASK = ROOT / "benchmarks/tasks/systems-design/authority-fencing"
TESTS = TASK / "tests"
ADMISSION_TOOL = ROOT / "tools/run_authority_fencing_admission.py"
ADMISSION_TEST = ROOT / "tests/test_authority_fencing_task.py"
ADMISSION_TEST_HELPER = ROOT / "tests/conftest.py"
SOURCE_PATHS = (TASK, ADMISSION_TOOL, ADMISSION_TEST, ADMISSION_TEST_HELPER)
DEFAULT_IMAGE = "tetrabench-authority-fencing-verifier:local"
DOCKER = "/usr/bin/docker"
GATES = (
    "single-authority",
    "monotonic-fence",
    "stale-rejection",
    "restart-durability",
    "transaction-rollback",
    "terminal-idempotence",
)


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


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


def validate_hidden_case_binding() -> str:
    contract = tomllib.loads((TASK / "contract.toml").read_text())
    cases = tomllib.loads((TESTS / "cases.toml").read_text())
    value = hashlib.sha256(canonical(hidden_case_input(cases)).encode()).hexdigest()
    if value != cases.get("input_manifest_sha256"):
        raise ValueError("hidden case manifest digest mismatch")
    if value != contract.get("hidden_case_input_sha256"):
        raise ValueError("public contract hidden-case digest mismatch")
    return value


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


def verifier_base_image() -> str:
    first = (TESTS / "Dockerfile").read_text().splitlines()[0]
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
) -> dict[str, Any]:
    verifier_manifest = (
        tree_manifest(TESTS)
        if verifier_context_manifest is None
        else verifier_context_manifest
    )
    if manifest_digest(verifier_manifest) != verifier_context_sha256:
        raise ValueError("verifier context manifest digest mismatch")
    candidate_manifest = source_manifest(source_paths, root=source_root)
    revision, state = source_git_state(candidate_manifest, root=source_root)
    return {
        "base_image": verifier_base_image(),
        "hidden_case_input_sha256": hidden_case_input_sha256,
        "matrix_contract": matrix_contract(mutants),
        "source_manifest": candidate_manifest,
        "source_manifest_sha256": manifest_digest(candidate_manifest),
        "source_revision": revision,
        "source_state": state,
        "task_context_sha256": tree_digest(TASK),
        "task_id": "systems-design/authority-fencing",
        "tool_versions": tool_version_values,
        "verifier_context_manifest": verifier_manifest,
        "verifier_context_sha256": verifier_context_sha256,
    }


def docker_versions() -> dict[str, str]:
    value = subprocess.run(  # nosec B603
        [
            DOCKER,
            "version",
            "--format",
            "{{.Client.Version}}|{{.Server.Version}}",
        ],
        cwd=ROOT,
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
    context: Path, context_sha256: str
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
            cwd=ROOT,
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
        cwd=ROOT,
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


def tool_versions() -> dict[str, Any]:
    return {
        "docker": docker_versions(),
        "harbor": metadata.version("harbor"),
        "python": platform.python_version(),
        "tetrabench": metadata.version("tetrabench"),
    }


def reference_source() -> str:
    solution = (TASK / "solution/solve.sh").read_text()
    start = "cat > /workspace/authority.py <<'PY'\n"
    prefix, separator, remainder = solution.partition(start)
    if not separator or not prefix.startswith("#!/bin/sh"):
        raise ValueError("solution source marker missing")
    source, end, _suffix = remainder.partition("\nPY\n")
    if not end:
        raise ValueError("solution source terminator missing")
    return source + "\n"


def stage(root: Path, source: str) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    for name in ("README.md", "contract.toml", "test_public.py"):
        shutil.copyfile(TASK / name, workspace / name)
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


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(effective_argv)
    if not args.debug and (args.image is not None or args.skip_build):
        parser.error("--image and --skip-build require explicit --debug mode")
    hidden_case_input_sha256 = validate_hidden_case_binding()
    mutants = tomllib.loads((TESTS / "mutants.toml").read_text())["mutants"]
    version_values = tool_versions()
    run_attestation: dict[str, Any]
    if args.debug:
        context_manifest = tree_manifest(TESTS)
        context_sha256 = manifest_digest(context_manifest)
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
        with tempfile.TemporaryDirectory(
            prefix="authority-build-context-"
        ) as directory:
            context = Path(directory) / "tests"
            shutil.copytree(
                TESTS,
                context,
                symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            context_manifest = tree_manifest(context)
            context_sha256 = manifest_digest(context_manifest)
            image, run_attestation = build_admission_image(context, context_sha256)
    if args.debug and not args.skip_build:
        subprocess.run(  # nosec B603
            [DOCKER, "build", "--tag", image, str(TESTS)],
            cwd=ROOT,
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=300,
        )
    source = reference_source()
    subject = admission_subject(
        hidden_case_input_sha256=hidden_case_input_sha256,
        mutants=mutants,
        tool_version_values=version_values,
        verifier_context_manifest=context_manifest,
        verifier_context_sha256=context_sha256,
    )
    entries: list[dict[str, Any]] = []
    candidates: list[tuple[str, str | None, str, str | None]] = [
        ("gold", None, source, None),
        ("no-op", None, (TASK / "authority.py").read_text(), None),
    ]
    for mutant in mutants:
        count = source.count(mutant["old"])
        expected_count = count if mutant.get("replace_all") is True else 1
        if count != expected_count or count == 0:
            raise ValueError(f"mutant replacement count mismatch: {mutant['id']}")
        mutated = source.replace(
            mutant["old"], mutant["new"], -1 if mutant.get("replace_all") is True else 1
        )
        candidates.append((mutant["id"], mutant["gate_id"], mutated, None))
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
                (TASK / "authority.py").read_text(),
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
    if matrix_projection(candidates) != subject["matrix_contract"]["candidates"]:
        raise ValueError("executed matrix differs from admission subject")
    with tempfile.TemporaryDirectory(prefix="authority-admission-") as directory:
        root = Path(directory)
        for index, (name, gate, candidate, tree_mutation) in enumerate(candidates):
            case_root = root / f"case-{index}"
            case_root.mkdir()
            workspace = stage(case_root, candidate)
            mutate_tree(workspace, tree_mutation)
            reward, diagnostics = run_verifier(image, workspace, case_root / "output")
            claim_evidence = concurrent_claim_evidence(
                diagnostics, required=reward == 1
            )
            gates = diagnostics.get("gates", {})
            expected_reward = 1 if name == "gold" else 0
            attribution_admitted = (
                mutant_attribution_passes(gates, gate) if gate is not None else None
            )
            if name == "attribution-probe-broad-mutant":
                passed = reward == 0 and attribution_admitted is False
            else:
                passed = reward == expected_reward and (
                    gate is None or attribution_admitted is True
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
    matrix_ok = all(item["passed"] == 1 for item in entries)
    gold_claim = next(
        item["concurrent_claim"] for item in entries if item["name"] == "gold"
    )
    if gold_claim is None:
        raise ValueError("gold admission evidence omits concurrent claim")
    admissible = not args.debug
    evidence = {
        "admissible": admissible,
        "candidate_count": len(entries),
        "command": [sys.executable, str(Path(__file__).resolve()), *effective_argv],
        "concurrent_claim": gold_claim,
        "debug": args.debug,
        "entries": entries,
        "hidden_case_input_sha256": hidden_case_input_sha256,
        "matrix_ok": matrix_ok,
        "non_admission_reason": "caller-controlled debug image" if args.debug else None,
        "ok": admissible and matrix_ok,
        "run_attestation": run_attestation,
        "schema_version": 1,
        "subject": subject,
        "subject_sha256": hashlib.sha256(canonical(subject).encode()).hexdigest(),
        "task_id": "systems-design/authority-fencing",
    }
    print(canonical(evidence))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
