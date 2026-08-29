from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from harbor.models.job.lock import JobLock
from harbor.models.job.result import JobResult

import tetrabench.harbor_api as harbor_api_module
from tetrabench.controller_runtime import attempt_paths
from tetrabench.harbor import (
    ATTEMPT_LABEL,
    ENVIRONMENT_IMPORT_PATH,
    PLAN_LABEL,
    RUN_LABEL,
)
from tetrabench.harbor_api import (
    HARBOR_API_VERSION,
    Harbor022Api,
    JobConfig,
    NativeJobArtifacts,
    NativeTrialArtifacts,
    _exact_model_value,
)
from tetrabench.harbor_runner import HarborRunner, compile_harbor_job
from tetrabench.integration import prepare_fixture_submission, run_local_composition
from tetrabench.models import ProjectConfig

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/harbor_task"


class _FakeApi:
    def __init__(self, *, errors: int = 0, cancelled: int = 0, fail=False):
        self.errors = errors
        self.cancelled = cancelled
        self.fail = fail
        self.config = None

    @staticmethod
    def task_config(*, path):
        return {"path": path}

    @staticmethod
    def validate_task(*, path):
        del path

    @staticmethod
    def agent_config(*, name, model_name):
        return {"name": name, "model_name": model_name}

    @staticmethod
    def docker_environment():
        return {"type": "docker"}

    @staticmethod
    def import_path_environment(*, import_path, kwargs):
        return {"import_path": import_path, "kwargs": kwargs}

    def job_config(self, **kwargs):
        self.config = SimpleNamespace(**kwargs)
        return self.config

    def execute(self, config):
        if self.fail:
            raise RuntimeError("Harbor failed")
        trial = SimpleNamespace(
            trial_name="trial-one",
            verifier_result=SimpleNamespace(rewards={"reward": 0}),
            exception_info=None,
            step_results=None,
        )
        return SimpleNamespace(
            stats=SimpleNamespace(
                n_completed_trials=1,
                n_errored_trials=self.errors,
                n_cancelled_trials=self.cancelled,
            ),
            trial_results=[trial],
        )

    @staticmethod
    def validate_native_artifacts(job_directory, result, config):
        trial = job_directory / "trial-one"
        return NativeJobArtifacts(
            job_directory=job_directory,
            config_path=job_directory / "config.json",
            lock_path=job_directory / "lock.json",
            result_path=job_directory / "result.json",
            trials=(
                NativeTrialArtifacts(
                    trial_name="trial-one",
                    directory=trial,
                    config_path=trial / "config.json",
                    lock_path=trial / "lock.json",
                    result_path=trial / "result.json",
                    result=result.trial_results[0],
                    atif_paths=(trial / "agent/trajectory.json",),
                ),
            ),
            config=config,
            lock=cast(JobLock, SimpleNamespace()),
            result=result,
        )


def _setup(tmp_path: Path, *, modal_execution: bool = False):
    config = ProjectConfig.model_validate(
        {
            "schema_version": 1,
            "controller": {"kind": "modal" if modal_execution else "local"},
            "execution": {"kind": "modal" if modal_execution else "docker"},
            "storage": (
                {"provider": "aws", "bucket": "bucket", "region": "us-west-2"}
                if modal_execution
                else None
            ),
            "harbor": {
                "agent_name": "opaque-agent",
                "model_name": "opaque/provider-model",
                "attempts": 2,
                "concurrency": 3,
            },
        }
    )
    prepared = prepare_fixture_submission(FIXTURE, config, run_id="fixture-run")
    paths = attempt_paths(tmp_path, "fixture-run", "attempt-one")
    paths.context.mkdir(parents=True)
    paths.jobs.mkdir()
    shutil.copytree(FIXTURE, paths.context / "fixture-task")
    labels = {
        RUN_LABEL: prepared.request.run_id,
        ATTEMPT_LABEL: paths.root.name,
        PLAN_LABEL: prepared.request.plan_sha256,
    }
    return prepared.request, paths, labels


def test_docker_config_compiles_opaque_agent_model_attempts_and_concurrency(
    tmp_path: Path,
) -> None:
    request, paths, labels = _setup(tmp_path)
    api = _FakeApi()
    config = compile_harbor_job(
        request,
        paths,
        environment_import_path=ENVIRONMENT_IMPORT_PATH,
        labels=labels,
        api=api,
    )
    assert config.environment == {"type": "docker"}
    assert config.agents == [
        {"name": "opaque-agent", "model_name": "opaque/provider-model"}
    ]
    assert config.n_attempts == 2
    assert config.n_concurrent_trials == 3
    assert config.tasks[0]["path"] == paths.context / "fixture-task"


def test_compilation_constructs_the_pinned_real_harbor_022_models(
    tmp_path: Path,
) -> None:
    request, paths, labels = _setup(tmp_path, modal_execution=True)
    config = compile_harbor_job(
        request,
        paths,
        environment_import_path=ENVIRONMENT_IMPORT_PATH,
        labels=labels,
        api=Harbor022Api(),
    )
    assert HARBOR_API_VERSION == "0.22.0"
    assert isinstance(config, JobConfig)
    assert config.environment.import_path == ENVIRONMENT_IMPORT_PATH
    assert config.environment.kwargs["labels"] == labels
    assert config.agents[0].name == "opaque-agent"
    assert config.agents[0].model_name == "opaque/provider-model"


def test_exact_job_config_comparison_does_not_use_harbor_relaxed_equality() -> None:
    first = JobConfig(job_name="first")
    second = JobConfig(job_name="second")
    assert first == second
    assert _exact_model_value(first) != _exact_model_value(second)


def test_modal_config_uses_public_import_path_and_observation_identity(
    tmp_path: Path,
) -> None:
    request, paths, labels = _setup(tmp_path, modal_execution=True)
    config = compile_harbor_job(
        request,
        paths,
        environment_import_path=ENVIRONMENT_IMPORT_PATH,
        labels=labels,
        api=_FakeApi(),
    )
    assert config.environment["import_path"] == ENVIRONMENT_IMPORT_PATH
    assert config.environment["kwargs"] == {
        "run_id": "fixture-run",
        "attempt_id": "attempt-one",
        "plan_sha256": request.plan_sha256,
        "event_sink_key": "",
        "observation_path": str(paths.child_events),
        "labels": labels,
    }


def test_zero_verifier_reward_is_a_successful_runner_outcome(tmp_path: Path) -> None:
    request, paths, labels = _setup(tmp_path)
    result = HarborRunner(_FakeApi()).run(
        request,
        paths,
        environment_import_path=ENVIRONMENT_IMPORT_PATH,
        labels=labels,
    )
    assert result.outcome == "succeeded"
    assert result.reward == "0"
    assert result.config_path == paths.jobs / "harbor-job/config.json"
    assert result.atif_paths == (
        paths.jobs / "harbor-job/trial-one/agent/trajectory.json",
    )


@pytest.mark.parametrize(
    ("errors", "cancelled", "outcome"),
    [(1, 0, "failed"), (1, 1, "cancelled"), (2, 1, "failed")],
)
def test_runner_uses_harbor_error_and_cancellation_semantics(
    tmp_path: Path,
    errors: int,
    cancelled: int,
    outcome: str,
) -> None:
    request, paths, labels = _setup(tmp_path)
    result = HarborRunner(_FakeApi(errors=errors, cancelled=cancelled)).run(
        request,
        paths,
        environment_import_path=ENVIRONMENT_IMPORT_PATH,
        labels=labels,
    )
    assert result.outcome == outcome


def test_runner_propagates_harbor_failure_with_partial_directory_intact(
    tmp_path: Path,
) -> None:
    request, paths, labels = _setup(tmp_path)
    with pytest.raises(RuntimeError, match="Harbor failed"):
        HarborRunner(_FakeApi(fail=True)).run(
            request,
            paths,
            environment_import_path=ENVIRONMENT_IMPORT_PATH,
            labels=labels,
        )
    assert paths.jobs.is_dir()


def test_native_artifact_discovery_requires_real_job_and_trial_files(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job"
    trial = job / "trial-one"
    (trial / "agent").mkdir(parents=True)
    for path in (
        job / "config.json",
        job / "lock.json",
        job / "result.json",
        trial / "config.json",
        trial / "lock.json",
        trial / "result.json",
        trial / "agent/trajectory.json",
    ):
        path.write_text("{}")

    result = cast(JobResult, object())
    config = JobConfig()
    with pytest.raises(ValueError):
        harbor_api_module.Harbor022Api.validate_native_artifacts(job, result, config)


def _docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
def test_real_harbor_local_docker_fixture_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _docker_available(), "Docker daemon is required for the test suite"
    credential_names = (
        "AWS_ACCESS_KEY_ID",
        "AWS_ACCOUNT_ID",
        "aws_alternate_credential",
        "AwS_Mixed_Credential",
        "TIGRIS_SECRET_ACCESS_KEY",
        "tigris_alternate_credential",
        "TiGrIs_Mixed_Credential",
        "bOtO_cOnFiG",
        "bOtOcOrE_TcP_KeEpAlIvE",
    )
    for name in credential_names:
        monkeypatch.setenv(name, f"controller-secret-{name.lower()}")
    fixture_result = run_local_composition(FIXTURE, tmp_path / "run")
    terminal = fixture_result.terminal
    assert (
        fixture_result.runtime == "ControllerRuntime with attached Harbor 0.22.0 Docker"
    )
    assert fixture_result.controller.state == "terminal"
    assert terminal.outcome == "succeeded"
    assert terminal.harbor_result is not None
    assert terminal.harbor_config is not None
    assert terminal.harbor_lock is not None
    assert all(name in __import__("os").environ for name in credential_names)
    run = fixture_result.invocation_root / "jobs/harbor-job"
    job_result = json.loads((run / "result.json").read_text())
    assert job_result["stats"]["n_errored_trials"] == 0
    trial_directories = [path for path in run.iterdir() if path.is_dir()]
    assert len(trial_directories) == 1
    trial_result = json.loads((trial_directories[0] / "result.json").read_text())
    assert trial_result["verifier_result"]["rewards"] == {"reward": 1.0}
    assert (trial_directories[0] / "agent/oracle.txt").is_file()
    assert not (trial_directories[0] / "agent/trajectory.json").exists()
    assert any("ATIF missing" in warning for warning in terminal.warnings)
