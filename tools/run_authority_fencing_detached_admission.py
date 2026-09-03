#!/usr/bin/env python3
# ruff: noqa: E402
"""Run the bounded detached authority-fencing admission sequence."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPOSITORY_ROOT = Path(__file__).parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tetrabench.canonical_json import dumps_canonical_json, sha256_hex
from tetrabench.controller import ModalControllerClient
from tetrabench.harbor import ModalChildObserver, S3ChildIdentitySource
from tetrabench.lifecycle import (
    CancellationResult,
    CancellationService,
    RecoveryService,
)
from tetrabench.receipts import ReceiptStore
from tetrabench.records import validate_run_id
from tetrabench.remote import RemoteResult, RemoteResultService
from tetrabench.s3 import create_s3_store
from tetrabench.submission import (
    PreparedSubmission,
    SubmissionService,
    prepare_submission,
)
from tools.run_authority_fencing_admission import (
    ProofOutputAuthority,
    manifest_digest,
    open_proof_output_authority,
    tree_manifest,
    write_exclusive_proof,
)

TASK_ID = "authority-fencing"
QUALIFIED_TASK_ID = f"systems-design/{TASK_ID}"
TASK = REPOSITORY_ROOT / "benchmarks/tasks/systems-design" / TASK_ID
DRIVER = Path(__file__)
ADMISSION_TOOL = REPOSITORY_ROOT / "tools/run_authority_fencing_admission.py"
CASE_SPECS = (("gold-1", 1), ("gold-2", 1), ("reward-forgery-audit", 0))
MAX_TOTAL_SECONDS = 30 * 60
MAX_REPORT_BYTES = 64 * 1024
RUN_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,35}$")
TERMINAL_CONTROLLER_STATES = frozenset({"succeeded", "failed", "expired"})
REQUIRED_ARTIFACTS: dict[str, Callable[[str], bool]] = {
    "controller-plan": lambda path: path.endswith("/controller-plan.json"),
    "controller-result": lambda path: path.endswith("/controller-result.json"),
    "job-config": lambda path: path.endswith("/jobs/harbor-job/config.json"),
    "job-lock": lambda path: path.endswith("/jobs/harbor-job/lock.json"),
    "job-result": lambda path: path.endswith("/jobs/harbor-job/result.json"),
}
TRIAL_REQUIRED_FILES = {
    "artifact-manifest": "artifacts/manifest.json",
    "diagnostics": "verifier/diagnostics.json",
    "reward": "verifier/reward.json",
    "trial-config": "config.json",
    "trial-lock": "lock.json",
    "trial-result": "result.json",
}
AUDIT_SOLUTION = b"""#!/bin/sh
set -u
for target in /logs/verifier/reward.json /workspace/reward.json; do
    if printf '%s\\n' '{\"reward\":1}' >\"$target\" 2>/dev/null; then
        :
    fi
done
exit 0
"""


class _Submission(Protocol):
    def submit(self, prepared: PreparedSubmission) -> Any: ...


class _Remote(Protocol):
    def result(self, run_id: str) -> RemoteResult: ...


class _Controller(Protocol):
    def inspect(self, call_id: str) -> Any: ...


class _Cancellation(Protocol):
    def cancel(self, run_id: str) -> Any: ...


class _Recovery(Protocol):
    def recover(self, run_id: str) -> Any: ...


class _Children(Protocol):
    def sweep(self, run_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    submission: _Submission
    remote: _Remote
    controller: _Controller
    cancellation: _Cancellation
    recovery: _Recovery
    children: _Children


@dataclass(frozen=True, slots=True)
class DriverDependencies:
    prepare: Callable[..., PreparedSubmission] = prepare_submission
    services: Callable[[PreparedSubmission, Path], RuntimeServices] | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    uuid_hex: Callable[[], str] = lambda: uuid.uuid4().hex


@dataclass(slots=True)
class CaseProgress:
    stage: str = "project"
    run_id: str | None = None
    post_submit: bool = False
    cancellation_attempted: bool = False
    cancellation_completed: bool = False


class DetachedAdmissionTimeout(TimeoutError):
    """The bounded detached admission deadline elapsed."""


def _production_services(
    prepared: PreparedSubmission, receipt_root: Path
) -> RuntimeServices:
    storage = prepared.plan.storage
    launch = prepared.controller_launch
    if storage is None or storage.provider != "tigris":
        raise ValueError("detached admission requires a Tigris storage profile")
    if launch is None or prepared.plan.controller.kind != "modal":
        raise ValueError("detached admission requires a deployed Modal controller")
    store = create_s3_store(storage)
    controller = ModalControllerClient(
        launch.app_name,
        launch.function_name,
        environment_name=launch.environment_name,
    )
    receipts = ReceiptStore(receipt_root)
    submission = SubmissionService(store, controller, receipts)
    children = ModalChildObserver(
        S3ChildIdentitySource(store),
        environment_name=launch.environment_name,
    )
    return RuntimeServices(
        submission=submission,
        remote=RemoteResultService(store),
        controller=controller,
        cancellation=CancellationService(
            store,
            controller,
            children,
        ),
        recovery=RecoveryService(store, controller, children, submission),
        children=children,
    )


def _safe_run_prefix(value: str) -> str:
    if RUN_PREFIX_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "run prefix must be 1-36 lowercase key-safe characters"
        )
    return value


def _bounded_total_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be an integer") from error
    if not 1 <= parsed <= MAX_TOTAL_SECONDS:
        raise argparse.ArgumentTypeError(
            f"timeout must be between 1 and {MAX_TOTAL_SECONDS} seconds"
        )
    return parsed


def _bounded_poll_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("poll interval must be an integer") from error
    if not 1 <= parsed <= 60:
        raise argparse.ArgumentTypeError(
            "poll interval must be between 1 and 60 seconds"
        )
    return parsed


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=_safe_run_prefix)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-prefix", type=_safe_run_prefix)
    parser.add_argument(
        "--timeout-seconds",
        type=_bounded_total_seconds,
        default=MAX_TOTAL_SECONDS,
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=_bounded_poll_seconds,
        default=5,
    )
    return parser.parse_args(argv)


def _private_directory(path: Path) -> None:
    os.chmod(path, 0o700)
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise OSError("temporary admission root is not private")


def _candidate_manifest(path: Path) -> list[dict[str, Any]]:
    return [
        entry
        for entry in tree_manifest(path)
        if "__pycache__" not in Path(entry["path"]).parts
        and not entry["path"].endswith(".pyc")
    ]


def _copy_candidate(source: Path, destination: Path) -> str:
    before = _candidate_manifest(source)
    shutil.copytree(
        source,
        destination,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    after = _candidate_manifest(source)
    copied = _candidate_manifest(destination)
    if before != after or before != copied:
        raise ValueError("candidate changed while creating verified copy")
    return manifest_digest(copied)


def _write_project(root: Path, source: Path, *, audit: bool) -> tuple[Path, str]:
    project = root / "project"
    task = project / "tasks" / TASK_ID
    task.parent.mkdir(parents=True)
    source_digest = _copy_candidate(source, task)
    if audit:
        before = tree_manifest(task)
        solution = task / "solution/solve.sh"
        solution.write_bytes(AUDIT_SOLUTION)
        solution.chmod(0o755)
        after = tree_manifest(task)
        changed = {
            entry["path"]
            for manifest in (before, after)
            for entry in manifest
            if entry not in (after if manifest is before else before)
        }
        if changed != {"solution/solve.sh"}:
            raise ValueError("audit copy changed outside solution/solve.sh")
    fixture_digest = manifest_digest(tree_manifest(task))
    if not audit and fixture_digest != source_digest:
        raise ValueError("gold task copy differs from the candidate")

    benchmarks = project / "benchmarks"
    benchmarks.mkdir()
    (benchmarks / "systems.md").write_text("# systems-design\n", encoding="utf-8")
    (benchmarks / "github.md").write_text("# github-workflow\n", encoding="utf-8")
    (benchmarks / "catalog.toml").write_text(
        """schema_version = 1
[sections.systems-design]
readme = "systems.md"
[sections.github-workflow]
readme = "github.md"
tasks = []
[[sections.systems-design.tasks]]
id = "authority-fencing"
harbor_task = "tasks/authority-fencing"
reward_policy = "binary"
""",
        encoding="utf-8",
    )
    (project / "tetrabench.toml").write_text(
        """schema_version = 1
catalog_path = "benchmarks/catalog.toml"
[controller]
kind = "modal"
[execution]
kind = "modal"
[selection]
include = ["authority-fencing"]
[harbor]
agent_name = "oracle"
attempts = 1
concurrency = 1
""",
        encoding="utf-8",
    )
    return project, fixture_digest


def _source_identity() -> dict[str, str]:
    revision = subprocess.run(  # nosec B603 B607
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision is not a full lowercase Git object ID")
    status = subprocess.run(  # nosec B603 B607
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout
    return {
        "candidate_manifest_sha256": manifest_digest(_candidate_manifest(TASK)),
        "admission_tool_sha256": hashlib.sha256(
            ADMISSION_TOOL.read_bytes()
        ).hexdigest(),
        "driver_sha256": hashlib.sha256(DRIVER.read_bytes()).hexdigest(),
        "pyproject_sha256": hashlib.sha256(
            (REPOSITORY_ROOT / "pyproject.toml").read_bytes()
        ).hexdigest(),
        "revision": revision,
        "state": "clean" if not status else "dirty",
        "tetrabench_manifest_sha256": manifest_digest(
            _candidate_manifest(REPOSITORY_ROOT / "src/tetrabench")
        ),
        "uv_lock_sha256": hashlib.sha256(
            (REPOSITORY_ROOT / "uv.lock").read_bytes()
        ).hexdigest(),
    }


def _validate_prepared(prepared: PreparedSubmission) -> str:
    plan = prepared.plan
    if (
        plan.storage is None
        or plan.storage.provider != "tigris"
        or plan.controller.kind != "modal"
        or plan.execution.kind != "modal"
        or prepared.controller_launch is None
        or plan.harbor.agent_name != "oracle"
        or plan.harbor.attempts != 1
        or plan.harbor.concurrency != 1
        or len(plan.trials) != 1
        or plan.trials[0].task_id != TASK_ID
        or plan.trials[0].reward_policy != "binary"
    ):
        raise ValueError("temporary project did not resolve the exact admission plan")
    endpoint = {
        "app": prepared.controller_launch.app_name,
        "environment": prepared.controller_launch.environment_name,
        "function": prepared.controller_launch.function_name,
        "storage": plan.storage.model_dump(mode="json"),
    }
    return sha256_hex(dumps_canonical_json(endpoint))


def _returned_call_id(receipt: Any) -> str:
    try:
        calls = receipt.attempts[-1].controller_calls
        call_id = calls[-1].call_id
    except (AttributeError, IndexError, TypeError) as error:
        raise ValueError(
            "submission omitted returned controller call evidence"
        ) from error
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("submission returned invalid controller call evidence")
    return call_id


def _poll_remote(
    remote: _Remote,
    run_id: str,
    *,
    deadline: float,
    interval: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> RemoteResult:
    while True:
        if monotonic() >= deadline:
            raise DetachedAdmissionTimeout("remote terminal deadline elapsed")
        result = remote.result(run_id)
        if result.state == "terminal":
            return result
        if result.state == "conflict":
            raise ValueError("authoritative remote result is conflicted")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DetachedAdmissionTimeout("remote terminal deadline elapsed")
        sleep(min(interval, remaining))


def _poll_controller(
    controller: _Controller,
    call_id: str,
    *,
    deadline: float,
    interval: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> str:
    while True:
        if monotonic() >= deadline:
            raise DetachedAdmissionTimeout("controller terminal deadline elapsed")
        state = controller.inspect(call_id).state
        if state in TERMINAL_CONTROLLER_STATES:
            return state
        if state == "inspection_failed":
            raise ValueError("controller call inspection is unknown")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DetachedAdmissionTimeout("controller terminal deadline elapsed")
        sleep(min(interval, remaining))


def _inventory_digest(result: RemoteResult) -> str:
    inventory = [
        {
            "logical_path": item.logical_path,
            "media_type": item.media_type,
            "sha256": item.sha256,
            "size": item.size,
        }
        for item in sorted(result.artifacts, key=lambda item: item.logical_path)
    ]
    return sha256_hex(dumps_canonical_json(inventory))


def _validate_remote_result(
    result: RemoteResult, expected_reward: int, *, run_id: str
) -> None:
    if (
        result.run_id != run_id
        or result.state != "terminal"
        or result.outcome != "succeeded"
        or result.admission_state != "terminal"
        or result.terminal_sha256 is None
        or result.reward != str(expected_reward)
        or result.summary_status != "available"
        or result.summary is None
    ):
        raise ValueError("remote terminal authority does not match admission contract")
    summary = result.summary
    if (
        summary.policy != "binary"
        or summary.aggregate_kind != "binary_pass_rate"
        or summary.task_count != 1
        or summary.sample_count != 1
        or summary.pass_count != expected_reward
        or summary.aggregate != str(expected_reward)
        or len(summary.tasks) != 1
        or summary.tasks[0].task_id != TASK_ID
        or summary.tasks[0].sample_count != 1
        or summary.tasks[0].pass_count != expected_reward
        or len(summary.trials) != 1
        or summary.trials[0].task_id != TASK_ID
        or summary.trials[0].value != str(expected_reward)
    ):
        raise ValueError("binary authority-fencing summary does not match expectation")
    paths = tuple(item.logical_path for item in result.artifacts)
    missing = [
        name
        for name, predicate in REQUIRED_ARTIFACTS.items()
        if not any(predicate(path) for path in paths)
    ]
    trial_root = f"/jobs/harbor-job/{summary.trials[0].trial_name}/"
    missing.extend(
        name
        for name, suffix in TRIAL_REQUIRED_FILES.items()
        if not any(path.endswith(trial_root + suffix) for path in paths)
    )
    if not any(trial_root + "artifacts/workspace/" in path for path in paths):
        missing.append("collected-workspace")
    if missing:
        raise ValueError("terminal inventory omits required native artifacts")


def _case_evidence(
    *,
    kind: str,
    expected_reward: int,
    fixture_sha256: str,
    prepared: PreparedSubmission,
    result: RemoteResult,
    controller_state: str,
    cleanup_sweeps: int,
) -> dict[str, Any]:
    summary = result.summary
    if summary is None or result.terminal_sha256 is None:
        raise ValueError("case evidence requires validated terminal authority")
    return {
        "actual_reward": int(result.reward or "-1"),
        "artifact_count": len(result.artifacts),
        "cleanup_sweep_count": cleanup_sweeps,
        "context_manifest_sha256": prepared.request.context_manifest_sha256,
        "controller_terminal_state": controller_state,
        "expected_reward": expected_reward,
        "fixture_manifest_sha256": fixture_sha256,
        "inventory_sha256": _inventory_digest(result),
        "kind": kind,
        "ok": True,
        "plan_sha256": prepared.request.plan_sha256,
        "run_id": prepared.request.run_id,
        "summary": {
            "pass_count": summary.pass_count,
            "sample_count": summary.sample_count,
            "task_count": summary.task_count,
        },
        "terminal_sha256": result.terminal_sha256,
    }


def _validate_cancellation_result(
    result: CancellationResult,
    *,
    run_id: str,
) -> None:
    if result.run_id != run_id or not result.cleanup_complete:
        raise ValueError("post-failure cancellation cleanup is incomplete")
    if result.state == "cancelled":
        return
    if result.state == "terminal" and result.terminal_proof_observed:
        return
    raise ValueError("post-failure cancellation did not reach a final state")


def _cancel_failed_case(
    services: RuntimeServices,
    run_id: str,
    *,
    sleep: Callable[[float], None],
) -> None:
    cancellation = services.cancellation.cancel(run_id)
    if cancellation.state == "failed" and cancellation.controller_terminal_observed:
        consecutive_empty = 0
        for sweep in range(5):
            result = services.children.sweep(run_id)
            consecutive_empty = (
                consecutive_empty + 1 if not result.remaining_child_ids else 0
            )
            if consecutive_empty >= 2:
                return
            if sweep < 4:
                sleep(0.2)
        raise ValueError("post-failure child cleanup is incomplete")
    _validate_cancellation_result(cancellation, run_id=run_id)
    if cancellation.state != "terminal":
        return
    recovery = services.recovery.recover(run_id)
    if (
        recovery.state != "terminal"
        or not recovery.terminal_proof_observed
        or not recovery.cleanup_complete
        or recovery.sweeps < 2
        or recovery.successor_function_call_id is not None
    ):
        raise ValueError("post-failure terminal cleanup is incomplete")


def _execute_case(
    prepared: PreparedSubmission,
    services: RuntimeServices,
    *,
    kind: str,
    expected_reward: int,
    fixture_sha256: str,
    deadline: float,
    interval: int,
    dependencies: DriverDependencies,
    progress: CaseProgress,
) -> dict[str, Any]:
    run_id = prepared.request.run_id
    try:
        progress.stage = "submit"
        if dependencies.monotonic() >= deadline:
            raise DetachedAdmissionTimeout("submission deadline elapsed")
        progress.post_submit = True
        receipt = services.submission.submit(prepared)
        call_id = _returned_call_id(receipt)
        progress.stage = "remote-result"
        result = _poll_remote(
            services.remote,
            run_id,
            deadline=deadline,
            interval=interval,
            monotonic=dependencies.monotonic,
            sleep=dependencies.sleep,
        )
        _validate_remote_result(result, expected_reward, run_id=run_id)
        progress.stage = "controller-terminal"
        controller_state = _poll_controller(
            services.controller,
            call_id,
            deadline=deadline,
            interval=interval,
            monotonic=dependencies.monotonic,
            sleep=dependencies.sleep,
        )
        progress.stage = "terminal-cleanup"
        if dependencies.monotonic() >= deadline:
            raise DetachedAdmissionTimeout("terminal cleanup deadline elapsed")
        recovery = services.recovery.recover(run_id)
        if dependencies.monotonic() >= deadline:
            raise DetachedAdmissionTimeout("terminal cleanup deadline elapsed")
        if (
            recovery.state != "terminal"
            or not recovery.terminal_proof_observed
            or not recovery.cleanup_complete
            or recovery.sweeps < 2
            or recovery.successor_function_call_id is not None
        ):
            raise ValueError("terminal-only recovery cleanup is incomplete")
        return _case_evidence(
            kind=kind,
            expected_reward=expected_reward,
            fixture_sha256=fixture_sha256,
            prepared=prepared,
            result=result,
            controller_state=controller_state,
            cleanup_sweeps=recovery.sweeps,
        )
    except BaseException:
        if progress.post_submit:
            progress.cancellation_attempted = True
            with suppress(BaseException):
                _cancel_failed_case(services, run_id, sleep=dependencies.sleep)
                progress.cancellation_completed = True
        raise


def _error_code(error: BaseException) -> str:
    if isinstance(error, DetachedAdmissionTimeout):
        return "timeout"
    if isinstance(error, (ValueError, TypeError)):
        return "validation_error"
    return "operation_error"


def _base_report(profile: str) -> dict[str, Any]:
    return {
        "admissible": False,
        "case_count": len(CASE_SPECS),
        "cases": [],
        "completed_case_count": 0,
        "error": None,
        "ok": False,
        "profile": profile,
        "schema_version": 1,
        "source": None,
        "task_id": QUALIFIED_TASK_ID,
    }


def run_admission(
    args: argparse.Namespace,
    *,
    dependencies: DriverDependencies | None = None,
) -> dict[str, Any]:
    report = _base_report(args.profile)
    if not args.yes:
        report["error"] = {"code": "confirmation_required", "stage": "confirmation"}
        return report
    if sys.platform != "linux":
        report["error"] = {"code": "unsupported_platform", "stage": "preflight"}
        return report

    dependencies = dependencies or DriverDependencies()
    services_factory = dependencies.services or _production_services
    progress = CaseProgress()
    try:
        source = _source_identity()
        report["source"] = source
        if source["state"] != "clean":
            raise ValueError("detached admission requires a clean source checkout")
        deadline = dependencies.monotonic() + args.timeout_seconds
        run_prefix = (
            args.run_prefix or f"authority-admission-{dependencies.uuid_hex()[:12]}"
        )
        endpoint_sha256: str | None = None
        run_ids: set[str] = set()
        with tempfile.TemporaryDirectory(
            prefix="authority-detached-admission-"
        ) as name:
            private_root = Path(name)
            _private_directory(private_root)
            candidate = private_root / "candidate"
            candidate_sha256 = _copy_candidate(TASK, candidate)
            if candidate_sha256 != source["candidate_manifest_sha256"]:
                raise ValueError("candidate changed after source preflight")
            for ordinal, (kind, expected_reward) in enumerate(CASE_SPECS, start=1):
                progress = CaseProgress()
                if dependencies.monotonic() >= deadline:
                    raise DetachedAdmissionTimeout("admission deadline elapsed")
                case_root = private_root / f"case-{ordinal}"
                case_root.mkdir(mode=0o700)
                project, fixture_sha256 = _write_project(
                    case_root,
                    candidate,
                    audit=kind == "reward-forgery-audit",
                )
                run_id = validate_run_id(
                    f"{run_prefix}-{ordinal}-{dependencies.uuid_hex()[:12]}"
                )
                progress.run_id = run_id
                if run_id in run_ids:
                    raise ValueError("fresh run ID generation repeated")
                run_ids.add(run_id)
                progress.stage = "prepare"
                prepared = dependencies.prepare(
                    project,
                    "systems-design",
                    args.profile,
                    run_id=run_id,
                )
                selected_endpoint = _validate_prepared(prepared)
                if endpoint_sha256 is None:
                    endpoint_sha256 = selected_endpoint
                elif selected_endpoint != endpoint_sha256:
                    raise ValueError("selected profile changed between cases")
                progress.stage = "provider-construction"
                services = services_factory(
                    prepared,
                    private_root / "receipts",
                )
                evidence = _execute_case(
                    prepared,
                    services,
                    kind=kind,
                    expected_reward=expected_reward,
                    fixture_sha256=fixture_sha256,
                    deadline=deadline,
                    interval=args.poll_interval_seconds,
                    dependencies=dependencies,
                    progress=progress,
                )
                report["cases"].append(evidence)
                report["completed_case_count"] = len(report["cases"])
        if _source_identity() != source:
            raise ValueError("source checkout changed during detached admission")
        report["profile_endpoint_sha256"] = endpoint_sha256
        report["ok"] = True
        report["admissible"] = True
    except BaseException as error:
        report["error"] = {
            "cancellation_attempted": progress.cancellation_attempted,
            "cancellation_completed": progress.cancellation_completed,
            "code": _error_code(error),
            "stage": progress.stage,
        }
        if progress.run_id is not None:
            report["error"]["run_id"] = progress.run_id
    return report


def _encode_report(report: dict[str, Any]) -> bytes:
    data = dumps_canonical_json(report) + b"\n"
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("detached admission report exceeds limit")
    return data


def _emit(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main(
    argv: list[str] | None = None,
    *,
    dependencies: DriverDependencies | None = None,
) -> int:
    args = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    output_authority: ProofOutputAuthority | None = None
    if args.output is not None:
        try:
            output_authority = open_proof_output_authority(args.output)
        except BaseException as error:
            report = _base_report(args.profile)
            report["error"] = {
                "code": _error_code(error),
                "stage": "proof-output-preflight",
            }
            _emit(_encode_report(report))
            return 1
    try:
        report = run_admission(args, dependencies=dependencies)
        data = _encode_report(report)
        if report["ok"] and output_authority is not None:
            try:
                write_exclusive_proof(output_authority, data)
            except BaseException as error:
                failed = _base_report(args.profile)
                failed["source"] = report["source"]
                failed["cases"] = report["cases"]
                failed["completed_case_count"] = report["completed_case_count"]
                failed["error"] = {
                    "code": _error_code(error),
                    "stage": "proof-output",
                }
                data = _encode_report(failed)
                report = failed
        _emit(data)
        return 0 if report["ok"] else 1
    finally:
        if output_authority is not None:
            output_authority.close()


if __name__ == "__main__":
    raise SystemExit(main())
