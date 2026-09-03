from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from tetrabench.controller import ControllerCallState
from tetrabench.remote import RemoteArtifact, RemoteResult
from tetrabench.rewards import (
    SectionRewardSummary,
    TaskRewardSummary,
    TrialReward,
)

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools/run_authority_fencing_detached_admission.py"


def _load_driver(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _artifact_paths() -> list[str]:
    prefix = "attempts/attempt-1"
    trial = f"{prefix}/jobs/harbor-job/trials/authority-fencing__sample-0"
    return [
        f"{prefix}/controller-plan.json",
        f"{prefix}/controller-result.json",
        f"{prefix}/jobs/harbor-job/config.json",
        f"{prefix}/jobs/harbor-job/lock.json",
        f"{prefix}/jobs/harbor-job/result.json",
        f"{trial}/config.json",
        f"{trial}/lock.json",
        f"{trial}/result.json",
        f"{trial}/verifier/reward.json",
        f"{trial}/verifier/diagnostics.json",
        f"{trial}/artifacts/manifest.json",
        f"{trial}/artifacts/workspace/authority.py",
    ]


def _remote_result(reward: int, *, paths: list[str] | None = None) -> RemoteResult:
    trial = TrialReward(
        task_id="authority-fencing",
        trial_name="authority-fencing__sample-0",
        sample_index=0,
        policy="binary",
        value=str(reward),
    )
    task = TaskRewardSummary(
        task_id="authority-fencing",
        policy="binary",
        sample_count=1,
        pass_count=reward,
        aggregate=str(reward),
    )
    summary = SectionRewardSummary(
        policy="binary",
        aggregate_kind="binary_pass_rate",
        task_count=1,
        sample_count=1,
        pass_count=reward,
        aggregate=str(reward),
        trials=(trial,),
        tasks=(task,),
    )
    artifacts = tuple(
        RemoteArtifact(
            logical_path=path,
            sha256=f"{index + 1:064x}",
            size=index,
            media_type="application/octet-stream",
        )
        for index, path in enumerate(paths or _artifact_paths())
    )
    return RemoteResult(
        run_id="run-1",
        state="terminal",
        admission_state="terminal",
        outcome="succeeded",
        reward=str(reward),
        summary_status="available",
        summary=summary,
        terminal_sha256="f" * 64,
        artifacts=artifacts,
    )


class _Storage:
    provider = "tigris"

    def model_dump(self, *, mode: str) -> dict[str, str]:
        assert mode == "json"
        return {
            "provider": "tigris",
            "bucket": "private",
            "region": "auto",
            "prefix": "proof",
        }


def _prepared(run_id: str) -> Any:
    request = SimpleNamespace(
        run_id=run_id,
        context_manifest_sha256="a" * 64,
        plan_sha256="b" * 64,
    )
    plan = SimpleNamespace(
        storage=_Storage(),
        controller=SimpleNamespace(kind="modal"),
        execution=SimpleNamespace(kind="modal"),
        harbor=SimpleNamespace(agent_name="oracle", attempts=1, concurrency=1),
        trials=(SimpleNamespace(task_id="authority-fencing", reward_policy="binary"),),
    )
    launch = SimpleNamespace(
        app_name="app",
        function_name="controller",
        environment_name="environment",
    )
    return SimpleNamespace(request=request, plan=plan, controller_launch=launch)


class _Submission:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def submit(self, _prepared: Any) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        call = SimpleNamespace(call_id="fc-private")
        attempt = SimpleNamespace(controller_calls=(call,))
        return SimpleNamespace(attempts=(attempt,))


class _Remote:
    def __init__(
        self, result: RemoteResult | None = None, error: BaseException | None = None
    ) -> None:
        self.value = result or _remote_result(1)
        self.error = error

    def result(self, _run_id: str) -> RemoteResult:
        if self.error is not None:
            raise self.error
        return self.value


class _Controller:
    def __init__(
        self,
        state: Literal[
            "running", "succeeded", "failed", "expired", "inspection_failed"
        ] = "succeeded",
    ) -> None:
        self.state = state

    def inspect(self, call_id: str) -> ControllerCallState:
        return ControllerCallState(call_id=call_id, state=self.state)


class _Cancellation:
    def __init__(self, result: Any | None = None) -> None:
        self.run_ids: list[str] = []
        self.result = result

    def cancel(self, run_id: str) -> Any:
        self.run_ids.append(run_id)
        return self.result or SimpleNamespace(
            run_id=run_id,
            state="cancelled",
            terminal_proof_observed=False,
            cleanup_complete=True,
        )


class _Recovery:
    def __init__(self, *, cleanup: bool = True, sweeps: int = 2) -> None:
        self.cleanup = cleanup
        self.sweeps = sweeps

    def recover(self, _run_id: str) -> Any:
        return SimpleNamespace(
            state="terminal",
            terminal_proof_observed=True,
            cleanup_complete=self.cleanup,
            sweeps=self.sweeps,
            successor_function_call_id=None,
        )


class _Children:
    def __init__(self, sweeps: tuple[tuple[str, ...], ...] = ((), ())) -> None:
        self.sweeps = list(sweeps)
        self.calls = 0

    def sweep(self, run_id: str) -> Any:
        self.calls += 1
        remaining = self.sweeps.pop(0) if self.sweeps else ()
        return SimpleNamespace(run_id=run_id, remaining_child_ids=remaining)


def _services(
    driver: Any,
    *,
    reward: int = 1,
    run_id: str = "run-1",
    remote_error: BaseException | None = None,
    controller_state: Literal[
        "running", "succeeded", "failed", "expired", "inspection_failed"
    ] = "succeeded",
    recovery: _Recovery | None = None,
    cancellation_result: Any | None = None,
    children: _Children | None = None,
) -> tuple[Any, _Cancellation]:
    cancellation = _Cancellation(cancellation_result)
    result = _remote_result(reward).model_copy(update={"run_id": run_id})
    services = driver.RuntimeServices(
        submission=_Submission(),
        remote=_Remote(result, remote_error),
        controller=_Controller(controller_state),
        cancellation=cancellation,
        recovery=recovery or _Recovery(),
        children=children or _Children(),
    )
    return services, cancellation


def test_yes_is_required_before_source_or_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver("detached_admission_confirmation")
    monkeypatch.setattr(
        driver,
        "_source_identity",
        lambda: pytest.fail("source preflight must not run before confirmation"),
    )
    dependencies = driver.DriverDependencies(
        services=lambda _prepared, _root: pytest.fail(
            "provider construction must not run before confirmation"
        )
    )
    args = driver.parse_arguments(["--profile", "baseline"])
    report = driver.run_admission(args, dependencies=dependencies)
    assert report["error"] == {
        "code": "confirmation_required",
        "stage": "confirmation",
    }
    assert report["admissible"] is False
    assert report["ok"] is False


def test_temporary_project_is_binary_and_audit_changes_only_copied_solution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver("detached_admission_project")
    original = driver.tree_manifest(driver.TASK)
    gold, gold_digest = driver._write_project(
        tmp_path / "gold", driver.TASK, audit=False
    )
    audit, audit_digest = driver._write_project(
        tmp_path / "audit", driver.TASK, audit=True
    )

    catalog = tomllib.loads((gold / "benchmarks/catalog.toml").read_text())
    tasks = catalog["sections"]["systems-design"]["tasks"]
    assert tasks == [
        {
            "id": "authority-fencing",
            "harbor_task": "tasks/authority-fencing",
            "reward_policy": "binary",
        }
    ]
    gold_task = gold / "tasks/authority-fencing"
    audit_task = audit / "tasks/authority-fencing"
    assert driver._candidate_manifest(gold_task) == driver._candidate_manifest(
        driver.TASK
    )
    changed = {
        entry["path"]
        for entry in driver._candidate_manifest(gold_task)
        if entry not in driver._candidate_manifest(audit_task)
    } | {
        entry["path"]
        for entry in driver._candidate_manifest(audit_task)
        if entry not in driver._candidate_manifest(gold_task)
    }
    assert changed == {"solution/solve.sh"}
    solution = audit_task / "solution/solve.sh"
    assert solution.read_bytes() == driver.AUDIT_SOLUTION
    assert stat.S_IMODE(solution.stat().st_mode) == 0o755
    assert driver.tree_manifest(driver.TASK) == original
    assert gold_digest != audit_digest

    config = tmp_path / "config/tetrabench/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """schema_version = 1
[profiles.proof.controller]
kind = "modal"
app_name = "deployed-app"
function_name = "controller"
[profiles.proof.execution]
kind = "modal"
[profiles.proof.storage]
provider = "tigris"
bucket = "private-bucket"
prefix = "proof"
"""
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    prepared = driver.prepare_submission(
        gold,
        "systems-design",
        "proof",
        run_id="detached-test",
    )
    assert driver._validate_prepared(prepared)
    assert [trial.task_id for trial in prepared.plan.trials] == ["authority-fencing"]
    assert prepared.plan.trials[0].reward_policy == "binary"


@pytest.mark.parametrize(
    ("reward", "expected", "paths", "message"),
    [
        (0, 1, None, "remote terminal"),
        (1, 0, None, "remote terminal"),
        (1, 1, _artifact_paths()[:-1], "inventory"),
    ],
)
def test_reward_summary_and_artifact_validation_fail_closed(
    reward: int,
    expected: int,
    paths: list[str] | None,
    message: str,
) -> None:
    driver = _load_driver(
        f"detached_admission_validation_{reward}_{expected}_{message}"
    )
    result = _remote_result(reward, paths=paths)
    with pytest.raises(ValueError, match=message):
        driver._validate_remote_result(result, expected, run_id="run-1")


def test_summary_and_run_identity_validation_fail_closed() -> None:
    driver = _load_driver("detached_admission_summary_identity")
    result = _remote_result(1)
    assert result.summary is not None
    tampered_summary = result.summary.model_copy(update={"sample_count": 2})
    tampered = result.model_copy(update={"summary": tampered_summary})
    with pytest.raises(ValueError, match="summary"):
        driver._validate_remote_result(tampered, 1, run_id="run-1")
    with pytest.raises(ValueError, match="remote terminal"):
        driver._validate_remote_result(result, 1, run_id="another-run")


def test_controller_and_terminal_cleanup_are_required() -> None:
    driver = _load_driver("detached_admission_controller_cleanup")
    dependencies = driver.DriverDependencies(
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )
    progress = driver.CaseProgress()
    services, cancellation = _services(
        driver,
        recovery=_Recovery(cleanup=False),
    )
    with pytest.raises(ValueError, match="cleanup is incomplete"):
        driver._execute_case(
            _prepared("run-1"),
            services,
            kind="gold-1",
            expected_reward=1,
            fixture_sha256="c" * 64,
            deadline=2.0,
            interval=1,
            dependencies=dependencies,
            progress=progress,
        )
    assert cancellation.run_ids == ["run-1"]

    with pytest.raises(driver.DetachedAdmissionTimeout):
        driver._poll_controller(
            _Controller("running"),
            "fc-private",
            deadline=0.0,
            interval=1,
            monotonic=lambda: 1.0,
            sleep=lambda _seconds: None,
        )


def test_post_submit_error_invokes_cancellation_and_preserves_original() -> None:
    driver = _load_driver("detached_admission_cancellation")
    original = RuntimeError("private provider detail /must/not/escape")
    services, cancellation = _services(driver, remote_error=original)
    progress = driver.CaseProgress()
    dependencies = driver.DriverDependencies(
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(RuntimeError) as caught:
        driver._execute_case(
            _prepared("run-1"),
            services,
            kind="gold-1",
            expected_reward=1,
            fixture_sha256="c" * 64,
            deadline=2.0,
            interval=1,
            dependencies=dependencies,
            progress=progress,
        )
    assert caught.value is original
    assert cancellation.run_ids == ["run-1"]
    assert progress.cancellation_attempted is True
    assert progress.cancellation_completed is True


def test_post_submit_error_does_not_claim_incomplete_cancellation() -> None:
    driver = _load_driver("detached_admission_incomplete_cancellation")
    services, cancellation = _services(driver, remote_error=RuntimeError("failed"))
    cancellation.result = SimpleNamespace(
        run_id="run-1",
        state="failed",
        terminal_proof_observed=False,
        cleanup_complete=False,
    )
    progress = driver.CaseProgress()
    dependencies = driver.DriverDependencies(
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(RuntimeError, match="failed"):
        driver._execute_case(
            _prepared("run-1"),
            services,
            kind="gold-1",
            expected_reward=1,
            fixture_sha256="c" * 64,
            deadline=2.0,
            interval=1,
            dependencies=dependencies,
            progress=progress,
        )
    assert cancellation.run_ids == ["run-1"]
    assert progress.cancellation_attempted is True
    assert progress.cancellation_completed is False


def test_failed_admission_gets_direct_cleanup_without_recovery() -> None:
    driver = _load_driver("detached_admission_failed_cleanup")
    children = _Children((("child-1",), (), ()))
    cancellation_result = SimpleNamespace(
        run_id="run-1",
        state="failed",
        controller_terminal_observed=True,
        terminal_proof_observed=False,
        cleanup_complete=False,
    )
    services, cancellation = _services(
        driver,
        remote_error=RuntimeError("failed"),
        cancellation_result=cancellation_result,
        children=children,
    )
    recovery_calls = 0

    class RefusingRecovery:
        def recover(self, _run_id: str) -> Any:
            nonlocal recovery_calls
            recovery_calls += 1
            pytest.fail("failed admission cleanup must not spawn through recovery")

    services = driver.RuntimeServices(
        submission=services.submission,
        remote=services.remote,
        controller=services.controller,
        cancellation=services.cancellation,
        recovery=RefusingRecovery(),
        children=children,
    )
    progress = driver.CaseProgress()
    dependencies = driver.DriverDependencies(
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(RuntimeError, match="failed"):
        driver._execute_case(
            _prepared("run-1"),
            services,
            kind="gold-1",
            expected_reward=1,
            fixture_sha256="c" * 64,
            deadline=2.0,
            interval=1,
            dependencies=dependencies,
            progress=progress,
        )
    assert cancellation.run_ids == ["run-1"]
    assert children.calls == 3
    assert recovery_calls == 0
    assert progress.cancellation_completed is True


def test_post_submit_timeout_invokes_cancellation() -> None:
    driver = _load_driver("detached_admission_timeout_cancellation")
    cancellation = _Cancellation()

    class PendingRemote:
        def result(self, run_id: str) -> RemoteResult:
            return RemoteResult(run_id=run_id, state="unknown")

    services = driver.RuntimeServices(
        submission=_Submission(),
        remote=PendingRemote(),
        controller=_Controller(),
        cancellation=cancellation,
        recovery=_Recovery(),
        children=_Children(),
    )
    progress = driver.CaseProgress()
    clock = iter((0.0, 2.0))
    dependencies = driver.DriverDependencies(
        monotonic=lambda: next(clock),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(driver.DetachedAdmissionTimeout):
        driver._execute_case(
            _prepared("run-1"),
            services,
            kind="gold-1",
            expected_reward=1,
            fixture_sha256="c" * 64,
            deadline=1.0,
            interval=1,
            dependencies=dependencies,
            progress=progress,
        )
    assert cancellation.run_ids == ["run-1"]
    assert progress.cancellation_completed is True


def test_failed_report_is_bounded_and_omits_exception_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver("detached_admission_redaction")
    monkeypatch.setattr(
        driver,
        "_source_identity",
        lambda: {
            "admission_tool_sha256": "0" * 64,
            "candidate_manifest_sha256": "a" * 64,
            "driver_sha256": "b" * 64,
            "revision": "c" * 40,
            "state": "clean",
        },
    )
    monkeypatch.setattr(
        driver,
        "_write_project",
        lambda root, source, audit: (root, "d" * 64),
    )
    monkeypatch.setattr(driver, "_copy_candidate", lambda _source, _target: "a" * 64)
    original = RuntimeError("credential=secret-value /private/path")

    def factory(_prepared_value: Any, _root: Path) -> Any:
        services, _cancellation = _services(driver, remote_error=original)
        return services

    dependencies = driver.DriverDependencies(
        prepare=lambda _root, _section, _profile, run_id: _prepared(run_id),
        services=factory,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
        uuid_hex=lambda: "1" * 32,
    )
    args = driver.parse_arguments(["--profile", "baseline", "--yes"])
    report = driver.run_admission(args, dependencies=dependencies)
    encoded = driver._encode_report(report)
    assert len(encoded) <= driver.MAX_REPORT_BYTES
    assert b"secret-value" not in encoded
    assert b"private/path" not in encoded
    assert report["error"] == {
        "cancellation_attempted": True,
        "cancellation_completed": True,
        "code": "operation_error",
        "run_id": "authority-admission-111111111111-1-111111111111",
        "stage": "remote-result",
    }


def test_proof_output_is_created_only_after_all_three_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    driver = _load_driver("detached_admission_output")
    output = tmp_path / "proof.json"
    monkeypatch.setattr(
        driver,
        "_source_identity",
        lambda: {"candidate_manifest_sha256": "f" * 64, "state": "clean"},
    )
    monkeypatch.setattr(driver, "_copy_candidate", lambda _source, _target: "f" * 64)
    monkeypatch.setattr(
        driver,
        "_write_project",
        lambda root, source, audit: (root, ("e" if audit else "d") * 64),
    )
    rewards = iter((1, 1, 0))

    def factory(_prepared_value: Any, _root: Path) -> Any:
        services, _cancellation = _services(
            driver,
            reward=next(rewards),
            run_id=_prepared_value.request.run_id,
        )
        return services

    dependencies = driver.DriverDependencies(
        prepare=lambda _root, _section, _profile, run_id: _prepared(run_id),
        services=factory,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
        uuid_hex=lambda: os.urandom(16).hex(),
    )
    exit_code = driver.main(
        ["--profile", "baseline", "--yes", "--output", str(output)],
        dependencies=dependencies,
    )
    stdout = capfd.readouterr().out.encode()
    assert exit_code == 0
    assert output.read_bytes() == stdout
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    report = json.loads(stdout)
    assert [case["kind"] for case in report["cases"]] == [
        "gold-1",
        "gold-2",
        "reward-forgery-audit",
    ]
    assert report["admissible"] is True


def test_sequence_stops_on_first_failure_and_creates_no_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    driver = _load_driver("detached_admission_stop")
    output = tmp_path / "proof.json"
    monkeypatch.setattr(
        driver,
        "_source_identity",
        lambda: {"candidate_manifest_sha256": "f" * 64, "state": "clean"},
    )
    monkeypatch.setattr(driver, "_copy_candidate", lambda _source, _target: "f" * 64)
    monkeypatch.setattr(
        driver,
        "_write_project",
        lambda root, source, audit: (root, ("e" if audit else "d") * 64),
    )
    calls: list[int] = []

    def factory(_prepared_value: Any, _root: Path) -> Any:
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            services, _cancellation = _services(
                driver,
                run_id=_prepared_value.request.run_id,
                remote_error=RuntimeError("second failed"),
            )
            return services
        services, _cancellation = _services(
            driver,
            reward=1,
            run_id=_prepared_value.request.run_id,
        )
        return services

    dependencies = driver.DriverDependencies(
        prepare=lambda _root, _section, _profile, run_id: _prepared(run_id),
        services=factory,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
        uuid_hex=lambda: os.urandom(16).hex(),
    )
    exit_code = driver.main(
        ["--profile", "baseline", "--yes", "--output", str(output)],
        dependencies=dependencies,
    )
    report = json.loads(capfd.readouterr().out)
    assert exit_code == 1
    assert calls == [1, 2]
    assert report["completed_case_count"] == 1
    assert report["admissible"] is False
    assert not output.exists()


def test_dirty_source_refuses_before_fixture_or_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver("detached_admission_dirty_source")
    monkeypatch.setattr(driver, "_source_identity", lambda: {"state": "dirty"})
    monkeypatch.setattr(
        driver,
        "_copy_candidate",
        lambda _source, _target: pytest.fail("fixture copy must not run"),
    )
    dependencies = driver.DriverDependencies(
        services=lambda _prepared, _root: pytest.fail(
            "provider construction must not run"
        )
    )
    report = driver.run_admission(
        driver.parse_arguments(["--profile", "baseline", "--yes"]),
        dependencies=dependencies,
    )
    assert report["error"] == {
        "cancellation_attempted": False,
        "cancellation_completed": False,
        "code": "validation_error",
        "stage": "project",
    }
    assert report["admissible"] is False
