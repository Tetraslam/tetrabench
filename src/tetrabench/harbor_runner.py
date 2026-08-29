"""Compile and execute resolved plans with the pinned Harbor adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from tetrabench.controller_runtime import AttemptPaths, HarborRunResult
from tetrabench.harbor import ATTEMPT_LABEL, PLAN_LABEL, RUN_LABEL
from tetrabench.harbor_api import Harbor022Api, NativeJobArtifacts
from tetrabench.records import RequestRecord


class HarborApi(Protocol):
    def job_config(self, **kwargs: Any) -> Any: ...
    def task_config(self, *, path: Path) -> Any: ...
    def agent_config(self, *, name: str, model_name: str | None) -> Any: ...
    def docker_environment(self) -> Any: ...
    def import_path_environment(
        self, *, import_path: str, kwargs: dict[str, object]
    ) -> Any: ...
    def execute(self, config: Any) -> Any: ...
    def validate_native_artifacts(
        self, job_directory: Path, result: Any, config: Any
    ) -> NativeJobArtifacts: ...


def _task_path(context_root: Path, logical_path: str) -> Path:
    root = context_root.resolve(strict=True)
    path = (root / logical_path).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Harbor task escaped the materialized context") from error
    if not path.is_dir():
        raise ValueError(f"Harbor task is not a directory: {logical_path}")
    return path


def compile_harbor_job(
    request: RequestRecord,
    paths: AttemptPaths,
    *,
    environment_import_path: str,
    labels: dict[str, str],
    api: HarborApi,
    event_sink_key: str = "",
) -> Any:
    """Compile one resolved request without interpreting model/provider names."""
    expected_labels = {
        RUN_LABEL: request.run_id,
        ATTEMPT_LABEL: paths.root.name,
        PLAN_LABEL: request.plan_sha256,
    }
    if labels != expected_labels:
        raise ValueError("Harbor labels do not match the run, attempt, and plan")

    plan = request.plan
    tasks = [
        api.task_config(path=_task_path(paths.context, trial.harbor_task))
        for trial in plan.trials
    ]
    if plan.execution.kind == "docker":
        environment = api.docker_environment()
    elif plan.execution.kind == "modal":
        environment = api.import_path_environment(
            import_path=environment_import_path,
            kwargs={
                "run_id": request.run_id,
                "attempt_id": paths.root.name,
                "plan_sha256": request.plan_sha256,
                "event_sink_key": event_sink_key,
                "observation_path": str(paths.child_events),
                "labels": labels,
            },
        )
    else:
        raise ValueError(f"unsupported Harbor execution kind: {plan.execution.kind}")

    return api.job_config(
        job_name="harbor-job",
        jobs_dir=paths.jobs,
        n_attempts=plan.harbor.attempts,
        n_concurrent_trials=plan.harbor.concurrency,
        quiet=True,
        tasks=tasks,
        agents=[
            api.agent_config(
                name=plan.harbor.agent_name,
                model_name=plan.harbor.model_name,
            )
        ],
        environment=environment,
    )


def _outcome(result: Any) -> Literal["succeeded", "failed", "cancelled"]:
    errors = result.stats.n_errored_trials
    cancelled = result.stats.n_cancelled_trials
    if cancelled and errors == cancelled:
        return "cancelled"
    if errors:
        return "failed"
    return "succeeded"


def _native_evidence(artifacts: NativeJobArtifacts) -> tuple[str, ...]:
    result = artifacts.result
    rewards = sum(
        1
        for trial in artifacts.trials
        if trial.result.verifier_result is not None
        and trial.result.verifier_result.rewards is not None
    )
    exceptions = sum(
        trial.result.exception_info is not None
        or any(
            step.exception_info is not None
            for step in (trial.result.step_results or [])
        )
        for trial in artifacts.trials
    )
    return (
        f"Harbor completed {result.stats.n_completed_trials} native trial result(s)",
        f"Harbor recorded verifier rewards for {rewards} trial(s)",
        f"Harbor persisted exception evidence for {exceptions} trial(s)",
    )


class HarborRunner:
    """Synchronous controller adapter for Harbor's asynchronous public API."""

    def __init__(self, api: HarborApi | None = None) -> None:
        self._api = api or Harbor022Api()

    def run(
        self,
        request: RequestRecord,
        paths: AttemptPaths,
        *,
        environment_import_path: str,
        labels: dict[str, str],
        event_sink_key: str = "",
    ) -> HarborRunResult:
        config = compile_harbor_job(
            request,
            paths,
            environment_import_path=environment_import_path,
            labels=labels,
            event_sink_key=event_sink_key,
            api=self._api,
        )
        result = self._api.execute(config)
        job_directory = paths.jobs / config.job_name
        artifacts = self._api.validate_native_artifacts(job_directory, result, config)
        return HarborRunResult(
            outcome=_outcome(artifacts.result),
            job_directory=artifacts.job_directory,
            config_path=artifacts.config_path,
            lock_path=artifacts.lock_path,
            result_path=artifacts.result_path,
            evidence=_native_evidence(artifacts),
            atif_paths=tuple(
                path for trial in artifacts.trials for path in trial.atif_paths
            ),
        )
