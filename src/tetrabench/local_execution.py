"""Production attached local Docker execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tetrabench.canonical_json import sha256_hex
from tetrabench.catalog import SectionName
from tetrabench.config import load_project_config
from tetrabench.context import seal_context
from tetrabench.controller_runtime import (
    AttemptPaths,
    credential_free_harbor_environment,
)
from tetrabench.harbor import (
    ATTEMPT_LABEL,
    ENVIRONMENT_IMPORT_PATH,
    PLAN_LABEL,
    RUN_LABEL,
)
from tetrabench.harbor_runner import HarborRunner
from tetrabench.plan import canonical_model_bytes, plan_digest, resolve_plan
from tetrabench.records import RequestRecord
from tetrabench.rewards import SectionRewardSummary

LOCAL_RUN_ID = "local-run"


@dataclass(frozen=True, slots=True)
class LocalExecutionResult:
    outcome: Literal["succeeded", "failed", "cancelled"]
    reward: str | None
    summary: SectionRewardSummary
    job_directory: Path


class LocalOutputExistsError(FileExistsError):
    """The requested local output path was already reserved."""


def _validate_task_paths(root: Path, request: RequestRecord) -> None:
    for trial in request.plan.trials:
        try:
            task = (root / trial.harbor_task).resolve(strict=True)
            task.relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            raise ValueError(
                f"catalog task is not a checked-in directory: {trial.harbor_task}"
            ) from error
        if not task.is_dir():
            raise ValueError(
                f"catalog task is not a checked-in directory: {trial.harbor_task}"
            )


def run_local(
    root: Path,
    section: SectionName,
    profile: str,
    output_directory: Path,
) -> LocalExecutionResult:
    """Resolve one local profile and run its selected checked-in tasks attached."""
    root = root.resolve(strict=True)
    config = load_project_config(root, profile=profile)
    if config.controller.kind != "local" or config.execution.kind != "docker":
        raise ValueError(
            "run requires controller.kind='local' and execution.kind='docker'"
        )

    plan = resolve_plan(root, section, profile)
    if not plan.runnable or not plan.trials:
        reasons = "; ".join(plan.not_runnable_reasons)
        raise ValueError(f"plan is not runnable: {reasons}")

    key_prefix = config.storage.prefix if config.storage is not None else ""
    sealed = seal_context(root, config.context, key_prefix=key_prefix)
    expected_context = tuple(
        (item.destination, item.mode, item.content.size, item.content.sha256)
        for item in sealed.manifest.files
    )
    planned_context = tuple(
        (item.destination, item.mode, item.size, item.sha256) for item in plan.context
    )
    if expected_context != planned_context:
        raise ValueError("selected context changed while preparing local execution")

    manifest_bytes = canonical_model_bytes(sealed.manifest)
    request = RequestRecord(
        schema_version=1,
        run_id=LOCAL_RUN_ID,
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(manifest_bytes),
        context_manifest=sealed.manifest,
    )
    _validate_task_paths(root, request)

    output_directory = output_directory.expanduser().absolute()
    if not output_directory.parent.is_dir():
        raise ValueError(
            f"output parent directory does not exist: {output_directory.parent}"
        )
    if output_directory.exists() or output_directory.is_symlink():
        raise LocalOutputExistsError(
            f"output directory already exists: {output_directory}"
        )

    runner = HarborRunner()
    with credential_free_harbor_environment():
        runner.validate_tasks(request, root)
    try:
        output_directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise LocalOutputExistsError(
            f"output directory already exists: {output_directory}"
        ) from error
    output_directory.chmod(0o700)
    paths = AttemptPaths(
        root=output_directory,
        context=root,
        jobs=output_directory,
        request=output_directory / "request.json",
        child_events=output_directory / "child-events.jsonl",
        controller_plan=output_directory / "controller-plan.json",
        controller_result=output_directory / "controller-result.json",
        failure=output_directory / "failure.json",
    )
    labels = {
        RUN_LABEL: request.run_id,
        ATTEMPT_LABEL: paths.root.name,
        PLAN_LABEL: request.plan_sha256,
    }
    with credential_free_harbor_environment():
        result = runner.run(
            request,
            paths,
            environment_import_path=ENVIRONMENT_IMPORT_PATH,
            labels=labels,
        )
    if result.summary is None:
        raise ValueError("Harbor runner did not return a canonical reward summary")
    return LocalExecutionResult(
        outcome=result.outcome,
        reward=result.reward,
        summary=result.summary,
        job_directory=result.job_directory,
    )
