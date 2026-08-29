"""Pinned Harbor 0.22 import and lifecycle boundary."""

from __future__ import annotations

import asyncio
import json
import os
import stat
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
from harbor.models.task.artifacts import (
    convention_source_for_os,
    effective_artifact_service,
    is_convention_entry,
    source_relative_path,
    with_convention_entry,
)
from harbor.models.trajectories import Trajectory
from harbor.models.trial.artifact_manifest import ArtifactManifest
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
    config: TrialConfig
    result: TrialResult
    rewards: tuple[tuple[str, int | float], ...] | None
    step_rewards: tuple[tuple[tuple[str, int | float], ...], ...]
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


def _artifact_destination(source: str, destination: str | None) -> str:
    relative = (
        Path(destination) if destination else Path(*source_relative_path(source).parts)
    )
    return "artifacts" if relative == Path(".") else f"artifacts/{relative.as_posix()}"


def _reject_manifest_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate Harbor artifact manifest key: {key!r}")
        value[key] = item
    return value


def _reject_manifest_nonfinite(value: str) -> object:
    raise ValueError(f"nonfinite Harbor artifact manifest value: {value}")


def _load_strict_json(path: Path) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_manifest_duplicate_keys,
            parse_constant=_reject_manifest_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Harbor trial result is not strict JSON") from error


def _raw_verifier_rewards(
    verifier: object,
) -> tuple[tuple[str, int | float], ...] | None:
    if verifier is None:
        return None
    if not isinstance(verifier, dict):
        raise ValueError("Harbor verifier result must be an object")
    rewards = verifier.get("rewards")
    if rewards is None:
        return None
    if not isinstance(rewards, dict):
        raise ValueError("Harbor verifier rewards must be an object")
    return tuple(rewards.items())


def _raw_rewards(
    value: object,
) -> tuple[
    tuple[tuple[str, int | float], ...] | None,
    tuple[tuple[tuple[str, int | float], ...], ...],
]:
    if not isinstance(value, dict):
        raise ValueError("Harbor trial result root must be an object")
    primary = _raw_verifier_rewards(value.get("verifier_result"))
    step_rewards = []
    steps = value.get("step_results")
    if steps is not None:
        if not isinstance(steps, list) or any(
            not isinstance(step, dict) for step in steps
        ):
            raise ValueError("Harbor trial step results must be an array of objects")
        for step in steps:
            rewards = _raw_verifier_rewards(step.get("verifier_result"))
            if rewards is not None:
                step_rewards.append(rewards)
    return primary, tuple(step_rewards)


def _load_trial_artifact_manifest(manifest_path: Path) -> ArtifactManifest:
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest_data = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_reject_manifest_duplicate_keys,
            parse_constant=_reject_manifest_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Harbor trial artifact manifest is not valid JSON") from error
    if not isinstance(manifest_data, list) or any(
        not isinstance(entry, dict)
        or set(entry) != {"destination", "service", "source", "status", "type"}
        or not isinstance(entry["source"], str)
        or not isinstance(entry["destination"], str)
        or not isinstance(entry["type"], str)
        or not isinstance(entry["status"], str)
        or (entry["service"] is not None and not isinstance(entry["service"], str))
        for entry in manifest_data
    ):
        raise ValueError("Harbor trial artifact manifest schema changed")
    manifest = ArtifactManifest(entries=manifest_data)
    expected_bytes = json.dumps(manifest.to_json_data(), indent=2).encode("utf-8")
    if manifest_bytes != expected_bytes:
        raise ValueError("Harbor trial artifact manifest representation changed")
    return manifest


def _validate_trial_artifact_manifest(
    trial_directory: Path, trial_config: TrialConfig
) -> None:
    artifacts_directory = trial_directory / "artifacts"
    manifest_path = artifacts_directory / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Harbor trial artifact manifest is missing or unsafe")
    manifest = _load_trial_artifact_manifest(manifest_path)
    task_path = trial_config.task.path
    if task_path is None or task_path.is_symlink() or not task_path.is_dir():
        raise ValueError("Harbor persisted task path is missing or not a directory")
    try:
        task = Task(task_dir=task_path)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("Harbor persisted task path is not a valid task") from error
    task_artifacts = list(task.config.artifacts)
    task_os = task.config.environment.os
    convention_source = convention_source_for_os(task_os)
    expected = with_convention_entry(
        [*task_artifacts, *trial_config.artifacts],
        convention_source=convention_source,
    )
    if len(manifest.entries) != len(expected):
        raise ValueError("Harbor trial artifact manifest entry count changed")

    claimed: list[Path] = []
    for entry, artifact in zip(manifest.entries, expected, strict=True):
        destination = _artifact_destination(artifact.source, artifact.destination)
        is_convention = is_convention_entry(artifact, convention_source)
        expected_service = artifact.service
        if (
            entry.source != artifact.source
            or entry.destination != destination
            or entry.service != expected_service
            or effective_artifact_service(artifact) != (entry.service or "main")
        ):
            raise ValueError("Harbor trial artifact manifest binding changed")
        if is_convention:
            if entry.status not in {"ok", "empty"}:
                raise ValueError("Harbor convention artifact collection failed")
        elif entry.status != "ok":
            raise ValueError("Harbor declared artifact collection failed")

        relative = Path(entry.destination).relative_to("artifacts")
        target = artifacts_directory / relative
        target_stat = target.lstat() if target.exists() else None
        if entry.status == "empty":
            if not is_convention or (
                target_stat is not None
                and (not stat.S_ISDIR(target_stat.st_mode) or any(target.iterdir()))
            ):
                raise ValueError("Harbor convention artifact is not empty")
            continue
        if target_stat is None or stat.S_ISLNK(target_stat.st_mode):
            raise ValueError("Harbor artifact path is missing or unsafe")
        actual_type = (
            "directory"
            if stat.S_ISDIR(target_stat.st_mode)
            else "file"
            if stat.S_ISREG(target_stat.st_mode)
            else "special"
        )
        if entry.type != actual_type:
            raise ValueError("Harbor artifact manifest type disagrees with path")
        for current, directories, files in os.walk(target, followlinks=False):
            for name in [*directories, *files]:
                if stat.S_ISLNK((Path(current) / name).lstat().st_mode):
                    raise ValueError("Harbor artifact contains a symlink")
        if any(
            path == target or path in target.parents or target in path.parents
            for path in claimed
        ):
            raise ValueError("Harbor artifact destinations collide")
        claimed.append(target)


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
            _validate_trial_artifact_manifest(trial_directory, persisted_trial_config)
            persisted_trial_lock = TrialLock.model_validate_json(trial_lock.read_text())
            persisted_trial_locks.append(_exact_model_value(persisted_trial_lock))
            persisted_trial = TrialResult.model_validate_json(
                trial_result_path.read_text()
            )
            raw_rewards, raw_step_rewards = _raw_rewards(
                _load_strict_json(trial_result_path)
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
                    config=persisted_trial_config,
                    result=persisted_trial,
                    rewards=raw_rewards,
                    step_rewards=raw_step_rewards,
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
