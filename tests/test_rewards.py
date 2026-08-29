from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock
from harbor.models.job.result import JobResult
from harbor.models.trial.config import TaskConfig, TrialConfig
from harbor.models.trial.result import TrialResult
from pydantic import ValidationError

from tetrabench.harbor_api import NativeJobArtifacts, NativeTrialArtifacts
from tetrabench.models import ResolvedPlan
from tetrabench.rewards import (
    SectionRewardSummary,
    TrialReward,
    summarize_rewards,
)


def _plan(
    tasks: tuple[tuple[str, str, str], ...], *, attempts: int = 1
) -> ResolvedPlan:
    return ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "systems-design",
            "controller": {"kind": "local"},
            "execution": {"kind": "docker"},
            "storage": None,
            "selection": {},
            "harbor": {"attempts": attempts},
            "context": (),
            "trials": tuple(
                {
                    "task_id": task_id,
                    "harbor_task": path,
                    "reward_policy": policy,
                }
                for task_id, path, policy in tasks
            ),
            "runnable": True,
            "not_runnable_reasons": (),
        }
    )


def _artifacts(
    context: Path,
    samples: tuple[tuple[str, str, tuple[tuple[str, object], ...] | None], ...],
) -> NativeJobArtifacts:
    trials = []
    for trial_name, task_path, rewards in samples:
        directory = context.parent / trial_name
        trials.append(
            NativeTrialArtifacts(
                trial_name=trial_name,
                directory=directory,
                config_path=directory / "config.json",
                lock_path=directory / "lock.json",
                result_path=directory / "result.json",
                config=TrialConfig(task=TaskConfig(path=context / task_path)),
                result=cast(TrialResult, SimpleNamespace()),
                rewards=cast(tuple[tuple[str, int | float], ...] | None, rewards),
                step_rewards=(),
                atif_paths=(),
            )
        )
    return NativeJobArtifacts(
        job_directory=context.parent,
        config_path=context.parent / "config.json",
        lock_path=context.parent / "lock.json",
        result_path=context.parent / "result.json",
        config=JobConfig(),
        lock=cast(JobLock, SimpleNamespace()),
        result=cast(JobResult, SimpleNamespace()),
        trials=tuple(trials),
    )


def _context(tmp_path: Path, *tasks: str) -> Path:
    context = tmp_path / "context"
    for task in tasks:
        (context / task).mkdir(parents=True)
    return context


def _binary_summary_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy": "binary",
        "aggregate_kind": "binary_pass_rate",
        "task_count": 1,
        "sample_count": 1,
        "pass_count": 1,
        "aggregate": "1",
        "trials": (
            {
                "task_id": "task",
                "trial_name": "trial",
                "policy": "binary",
                "value": "1",
            },
        ),
        "tasks": (
            {
                "task_id": "task",
                "policy": "binary",
                "sample_count": 1,
                "pass_count": 1,
                "aggregate": "1",
            },
        ),
    }


def _numeric_summary_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy": "numeric",
        "aggregate_kind": "numeric_mean",
        "task_count": 1,
        "sample_count": 1,
        "pass_count": None,
        "aggregate": "1",
        "trials": (
            {
                "task_id": "task",
                "trial_name": "trial",
                "policy": "numeric",
                "value": "1",
            },
        ),
        "tasks": (
            {
                "task_id": "task",
                "policy": "numeric",
                "sample_count": 1,
                "pass_count": None,
                "aggregate": "1",
            },
        ),
    }


@pytest.mark.parametrize("value", [0, 1])
def test_binary_accepts_exact_integer_rewards(tmp_path: Path, value: int) -> None:
    context = _context(tmp_path, "task")
    summary = summarize_rewards(
        _plan((("task", "task", "binary"),)),
        context,
        _artifacts(context, (("trial", "task", (("reward", value),)),)),
    )

    assert summary.aggregate_kind == "binary_pass_rate"
    assert summary.pass_count == value
    assert summary.aggregate == str(value)
    assert summary.trials[0].value == str(value)


@pytest.mark.parametrize(
    "value", [0.0, 1.0, True, None, "1", 0.5, float("nan"), float("inf")]
)
def test_binary_rejects_every_non_exact_primary(tmp_path: Path, value: object) -> None:
    context = _context(tmp_path, "task")
    rewards = () if value is None else (("reward", value),)
    with pytest.raises(ValueError, match=r"binary primary|exact finite"):
        summarize_rewards(
            _plan((("task", "task", "binary"),)),
            context,
            _artifacts(context, (("trial", "task", rewards),)),
        )


@pytest.mark.parametrize(
    "value",
    ["2", "-0", "0.0", "-0.0", "00", "0e0", "0E+0", "1.0", "01", "1e0", "1E+0"],
)
def test_section_model_rejects_every_alternate_binary_trial_value(value: str) -> None:
    data = _binary_summary_data()
    trial = dict(cast(tuple[dict[str, object], ...], data["trials"])[0])
    trial["value"] = value
    data["trials"] = (trial,)

    with pytest.raises(ValidationError):
        SectionRewardSummary.model_validate(data)


def test_section_model_independently_rejects_constructed_binary_trial_value() -> None:
    data = _binary_summary_data()
    data["trials"] = (
        TrialReward.model_construct(
            task_id="task",
            trial_name="trial",
            policy="binary",
            value="2",
        ),
    )

    with pytest.raises(ValidationError, match="exactly string"):
        SectionRewardSummary.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "task_counts_and_rate",
        "section_counts_and_rate",
        "task_sample_count_and_rate",
    ],
)
def test_section_model_rejects_coherent_binary_arithmetic_not_derived_from_trials(
    mutation: str,
) -> None:
    data = _binary_summary_data()
    task = dict(cast(tuple[dict[str, object], ...], data["tasks"])[0])
    if mutation == "task_counts_and_rate":
        task.update(pass_count=0, aggregate="0")
    elif mutation == "section_counts_and_rate":
        data.update(pass_count=0, aggregate="0")
    else:
        task.update(sample_count=2, aggregate="0.5")
        data.update(sample_count=2, aggregate="0.5")
    data["tasks"] = (task,)

    with pytest.raises(ValidationError):
        SectionRewardSummary.model_validate(data)


@pytest.mark.parametrize("value", ["-0", "0.0", "-0.0", "1.0", "01", "1e0", "1E+0"])
@pytest.mark.parametrize("target", ["trial", "task", "section"])
def test_numeric_summary_rejects_noncanonical_decimal_strings(
    value: str, target: str
) -> None:
    data = _numeric_summary_data()
    if target == "trial":
        trial = dict(cast(tuple[dict[str, object], ...], data["trials"])[0])
        trial["value"] = value
        data["trials"] = (trial,)
    elif target == "task":
        task = dict(cast(tuple[dict[str, object], ...], data["tasks"])[0])
        task["aggregate"] = value
        data["tasks"] = (task,)
    else:
        data["aggregate"] = value

    with pytest.raises(ValidationError):
        SectionRewardSummary.model_validate(data)


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf")])
def test_every_diagnostic_must_be_exact_finite_numeric(
    tmp_path: Path, value: object
) -> None:
    context = _context(tmp_path, "task")
    with pytest.raises(ValueError, match="exact finite"):
        summarize_rewards(
            _plan((("task", "task", "numeric"),)),
            context,
            _artifacts(
                context,
                (("trial", "task", (("reward", 1), ("diagnostic", value))),),
            ),
        )


def test_step_verifier_diagnostics_are_also_validated(tmp_path: Path) -> None:
    context = _context(tmp_path, "task")
    artifacts = _artifacts(context, (("trial", "task", (("reward", 1),)),))
    artifacts = replace(
        artifacts,
        trials=(
            replace(
                artifacts.trials[0],
                step_rewards=((("diagnostic", float("nan")),),),
            ),
        ),
    )
    with pytest.raises(ValueError, match="exact finite"):
        summarize_rewards(
            _plan((("task", "task", "numeric"),)),
            context,
            artifacts,
        )


def test_numeric_rejects_mixed_missing_attempt_rewards(tmp_path: Path) -> None:
    context = _context(tmp_path, "task")
    with pytest.raises(ValueError, match="mixed missing"):
        summarize_rewards(
            _plan((("task", "task", "numeric"),), attempts=2),
            context,
            _artifacts(
                context,
                (
                    ("trial-a", "task", (("reward", 1),)),
                    ("trial-b", "task", None),
                ),
            ),
        )


def test_numeric_preserves_unavailable_when_no_primary_exists(tmp_path: Path) -> None:
    context = _context(tmp_path, "task")
    summary = summarize_rewards(
        _plan((("task", "task", "numeric"),)),
        context,
        _artifacts(context, (("trial", "task", (("diagnostic", 2.5),)),)),
    )
    assert summary.aggregate is None
    assert summary.trials == ()


def test_multi_task_attempt_summary_is_order_independent(tmp_path: Path) -> None:
    context = _context(tmp_path, "a", "b")
    plan = _plan((("a", "a", "binary"), ("b", "b", "binary")), attempts=2)
    samples = (
        ("z", "b", (("reward", 0),)),
        ("c", "a", (("reward", 1),)),
        ("a", "a", (("reward", 1),)),
        ("y", "b", (("reward", 1),)),
    )
    first = summarize_rewards(plan, context, _artifacts(context, samples))
    second = summarize_rewards(plan, context, _artifacts(context, samples[::-1]))

    assert first == second
    assert first.sample_count == 4
    assert first.task_count == 2
    assert first.pass_count == 3
    assert first.aggregate == "0.75"
    assert [(task.task_id, task.pass_count) for task in first.tasks] == [
        ("a", 2),
        ("b", 1),
    ]


def test_five_binary_tasks_aggregate_over_all_task_samples(tmp_path: Path) -> None:
    task_ids = ("a", "b", "c", "d", "e")
    context = _context(tmp_path, *task_ids)
    plan = _plan(tuple((task, task, "binary") for task in task_ids))
    artifacts = _artifacts(
        context,
        tuple(
            (f"trial-{task}", task, (("reward", value),))
            for task, value in zip(task_ids, (1, 0, 1, 1, 0), strict=True)
        ),
    )

    summary = summarize_rewards(plan, context, artifacts)
    assert summary.task_count == 5
    assert summary.sample_count == 5
    assert summary.pass_count == 3
    assert summary.aggregate == "0.6"
    assert tuple(task.task_id for task in summary.tasks) == task_ids


def test_path_mapping_rejects_ambiguous_unknown_and_escaped_relationships(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, "task")
    ambiguous = _plan((("a", "task", "numeric"), ("b", "task", "numeric")))
    with pytest.raises(ValueError, match="multiple resolved"):
        summarize_rewards(
            ambiguous,
            context,
            _artifacts(context, (("trial", "task", (("reward", 1),)),)),
        )

    plan = _plan((("task", "task", "numeric"),))
    _context(tmp_path, "other")
    with pytest.raises(ValueError, match="unknown"):
        summarize_rewards(
            plan,
            context,
            _artifacts(context, (("trial", "other", (("reward", 1),)),)),
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = _artifacts(context, (("trial", "task", (("reward", 1),)),))
    escaped_trial = escaped.trials[0]
    escaped = replace(
        escaped,
        trials=(
            replace(
                escaped_trial,
                config=TrialConfig(task=TaskConfig(path=outside)),
            ),
        ),
    )
    with pytest.raises(ValueError, match="escaped"):
        summarize_rewards(plan, context, escaped)


def test_plan_rejects_mixed_reward_policies() -> None:
    with pytest.raises(ValidationError, match="cannot mix reward policies"):
        _plan((("a", "a", "binary"), ("b", "b", "numeric")))
