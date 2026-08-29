"""Pinned Harbor 0.22 import and lifecycle boundary."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from harbor import Task
from harbor.job import Job
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock, TrialLock
from harbor.models.job.result import JobResult
from harbor.models.trajectories import Trajectory
from harbor.models.trial.config import (
    AgentConfig,
    EnvironmentConfig,
    TaskConfig,
    TrialConfig,
)
from harbor.models.trial.result import TrialResult

HARBOR_API_VERSION = "0.22.0"


@dataclass(frozen=True, slots=True)
class NativeTrialArtifacts:
    trial_name: str
    directory: Path
    config_path: Path
    lock_path: Path
    result_path: Path
    result: TrialResult
    atif_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class NativeJobArtifacts:
    job_directory: Path
    config_path: Path
    lock_path: Path
    result_path: Path
    config: JobConfig
    lock: JobLock
    result: JobResult
    trials: tuple[NativeTrialArtifacts, ...]


def _exact_model_value(model: Any) -> Any:
    return model.model_dump(mode="json", exclude_none=False)


_TRANSPORT_TIME_FIELDS = frozenset(
    {
        "started_at",
        "updated_at",
        "finished_at",
        "environment_setup",
        "agent_setup",
        "agent_execution",
        "verifier",
    }
)


def _semantic_model_value(model: Any) -> Any:
    value = _exact_model_value(model)

    def strip_times(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: strip_times(child)
                for key, child in item.items()
                if key not in _TRANSPORT_TIME_FIELDS
            }
        if isinstance(item, list):
            return [strip_times(child) for child in item]
        return item

    return strip_times(value)


def _terminal_stat_counts(stats: Any) -> tuple[int, int, int, int, int, int]:
    return (
        stats.n_completed_trials,
        stats.n_errored_trials,
        stats.n_running_trials,
        stats.n_pending_trials,
        stats.n_cancelled_trials,
        stats.n_retries,
    )


def _retry_semantics(retry: Any) -> Any:
    value = _exact_model_value(retry)
    for field in ("include_exceptions", "exclude_exceptions"):
        if value[field] is not None:
            value[field] = sorted(value[field])
    return value


def _safe_component(value: str, *, field: str) -> str:
    if Path(value).name != value or value in {"", ".", ".."}:
        raise ValueError(f"Harbor persisted an unsafe {field}")
    return value


class Harbor022Api:
    """Only Harbor interfaces used by tetrabench's runner."""

    def __init__(self) -> None:
        if version("harbor") != HARBOR_API_VERSION:
            raise RuntimeError(f"Harbor {HARBOR_API_VERSION} is required")

    @staticmethod
    def job_config(**kwargs: Any) -> JobConfig:
        return JobConfig(**kwargs)

    @staticmethod
    def task_config(*, path: Path) -> TaskConfig:
        return TaskConfig(path=path)

    @staticmethod
    def validate_task(*, path: Path) -> None:
        Task(task_dir=path)

    @staticmethod
    def agent_config(*, name: str, model_name: str | None) -> AgentConfig:
        return AgentConfig(name=name, model_name=model_name)

    @staticmethod
    def docker_environment() -> EnvironmentConfig:
        return EnvironmentConfig(type=EnvironmentType.DOCKER)

    @staticmethod
    def import_path_environment(
        *, import_path: str, kwargs: dict[str, object]
    ) -> EnvironmentConfig:
        return EnvironmentConfig(import_path=import_path, kwargs=kwargs)

    @staticmethod
    async def _execute(config: JobConfig) -> JobResult:
        job = await Job.create(config)
        return await job.run()

    def execute(self, config: JobConfig) -> JobResult:
        return asyncio.run(self._execute(config))

    @staticmethod
    def validate_native_artifacts(
        job_directory: Path,
        result: JobResult,
        config: JobConfig,
    ) -> NativeJobArtifacts:
        config_path = job_directory / "config.json"
        lock_path = job_directory / "lock.json"
        result_path = job_directory / "result.json"
        persisted_config = JobConfig.model_validate_json(config_path.read_text())
        if _exact_model_value(persisted_config) != _exact_model_value(config):
            raise ValueError("Harbor job config changed after compilation")
        persisted_lock = JobLock.model_validate_json(lock_path.read_text())
        if persisted_lock.n_concurrent_trials != persisted_config.n_concurrent_trials:
            raise ValueError("Harbor job lock concurrency disagrees with config")
        if _retry_semantics(persisted_lock.retry) != _retry_semantics(
            persisted_config.retry
        ):
            raise ValueError("Harbor job lock retry policy disagrees with config")
        persisted_result = JobResult.model_validate_json(result_path.read_text())
        if (
            persisted_result.id != result.id
            or persisted_result.n_total_trials != result.n_total_trials
            or _terminal_stat_counts(persisted_result.stats)
            != _terminal_stat_counts(result.stats)
        ):
            raise ValueError(
                "Harbor in-memory job result disagrees with persisted result"
            )
        if persisted_result.finished_at is None:
            raise ValueError("Harbor job result is incomplete or changed")
        if (
            persisted_result.stats.n_completed_trials != persisted_result.n_total_trials
            or persisted_result.stats.n_running_trials
            or persisted_result.stats.n_pending_trials
        ):
            raise ValueError("Harbor returned before every trial reached a result")
        if len(persisted_lock.trials) != persisted_result.n_total_trials:
            raise ValueError("Harbor job lock trial count disagrees with result")

        returned_trials = {trial.trial_name: trial for trial in result.trial_results}
        if len(returned_trials) != persisted_result.n_total_trials:
            raise ValueError("Harbor transport result trial count is incomplete")
        candidate_directories: list[Path] = []
        with os.scandir(job_directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError("Harbor job directory contains a symlink")
                if entry.is_dir(follow_symlinks=False):
                    candidate = Path(entry.path)
                    if all(
                        (candidate / name).is_file()
                        for name in ("config.json", "lock.json", "result.json")
                    ):
                        candidate_directories.append(candidate)
        if len(candidate_directories) != persisted_result.n_total_trials:
            raise ValueError("Harbor persisted trial directory count is incomplete")

        trials: list[NativeTrialArtifacts] = []
        trial_names: set[str] = set()
        trial_lock_values = sorted(
            (_exact_model_value(item) for item in persisted_lock.trials), key=repr
        )
        persisted_trial_locks: list[Any] = []
        for trial_directory in sorted(candidate_directories):
            trial_name = _safe_component(
                trial_directory.name, field="trial directory name"
            )
            if trial_name in trial_names:
                raise ValueError("Harbor returned duplicate trial directory names")
            trial_names.add(trial_name)
            trial_config = trial_directory / "config.json"
            trial_lock = trial_directory / "lock.json"
            trial_result_path = trial_directory / "result.json"
            persisted_trial_config = TrialConfig.model_validate_json(
                trial_config.read_text()
            )
            persisted_trial_lock = TrialLock.model_validate_json(trial_lock.read_text())
            persisted_trial_locks.append(_exact_model_value(persisted_trial_lock))
            persisted_trial = TrialResult.model_validate_json(
                trial_result_path.read_text()
            )
            if persisted_trial.trial_name != trial_name:
                raise ValueError("Harbor trial directory and result name disagree")
            returned_trial = returned_trials.get(trial_name)
            if returned_trial is None or _semantic_model_value(
                persisted_trial
            ) != _semantic_model_value(returned_trial):
                raise ValueError("Harbor trial result changed after execution")
            if _exact_model_value(persisted_trial.config) != _exact_model_value(
                persisted_trial_config
            ):
                raise ValueError("Harbor trial config disagrees with trial result")
            atif_paths: list[Path] = []
            if persisted_trial.step_results is None:
                atif_paths.append(trial_directory / "agent" / "trajectory.json")
            else:
                for step in persisted_trial.step_results:
                    step_name = _safe_component(step.step_name, field="step name")
                    atif_paths.append(
                        trial_directory
                        / "steps"
                        / step_name
                        / "agent"
                        / "trajectory.json"
                    )
            for trajectory in atif_paths:
                if trajectory.is_file():
                    Trajectory.model_validate_json(trajectory.read_text())
            trials.append(
                NativeTrialArtifacts(
                    trial_name=trial_name,
                    directory=trial_directory,
                    config_path=trial_config,
                    lock_path=trial_lock,
                    result_path=trial_result_path,
                    result=persisted_trial,
                    atif_paths=tuple(atif_paths),
                )
            )
        if sorted(persisted_trial_locks, key=repr) != trial_lock_values:
            raise ValueError("Harbor job and per-trial locks disagree")
        return NativeJobArtifacts(
            job_directory=job_directory,
            config_path=config_path,
            lock_path=lock_path,
            result_path=result_path,
            config=persisted_config,
            lock=persisted_lock,
            result=persisted_result,
            trials=tuple(trials),
        )
