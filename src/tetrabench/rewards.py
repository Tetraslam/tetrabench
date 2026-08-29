"""Canonical reward validation, summaries, and controller result records."""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from tetrabench.harbor_api import NativeJobArtifacts, NativeTrialArtifacts
from tetrabench.models import (
    FrozenRecord,
    NonEmptyString,
    RecordIdentifier,
    ResolvedPlan,
    ResolvedTrial,
    RewardPolicy,
    Sha256,
    TaskId,
)


def canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("reward value must be finite")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _validate_canonical_decimal(value: str) -> str:
    if canonical_decimal(Decimal(value)) != value:
        raise ValueError("decimal string is not canonically normalized")
    return value


DecimalString = Annotated[
    str,
    Field(pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"),
    AfterValidator(_validate_canonical_decimal),
]


def _mean(values: list[Decimal]) -> str | None:
    if not values:
        return None
    precision = max(50, max(len(value.as_tuple().digits) for value in values) + 50)
    with localcontext(Context(prec=precision)):
        return canonical_decimal(sum(values, Decimal()) / len(values))


def _ratio(numerator: int, denominator: int) -> str:
    # Preserve the pre-summary binary mean representation: binary inputs have
    # one significant digit, and _mean therefore evaluates them at precision 51.
    with localcontext(Context(prec=51)):
        return canonical_decimal(Decimal(numerator) / Decimal(denominator))


class TrialReward(FrozenRecord):
    task_id: TaskId
    trial_name: NonEmptyString
    sample_index: int | None = Field(default=None, ge=0)
    policy: RewardPolicy
    value: DecimalString

    @model_validator(mode="after")
    def validate_policy_value(self) -> TrialReward:
        if self.policy == "binary" and self.value not in {"0", "1"}:
            raise ValueError("binary trial reward must be exactly string 0 or 1")
        return self


class TaskRewardSummary(FrozenRecord):
    task_id: TaskId
    policy: RewardPolicy
    sample_count: int = Field(ge=1)
    pass_count: int | None = Field(default=None, ge=0)
    aggregate: DecimalString | None

    @model_validator(mode="after")
    def validate_policy_fields(self) -> TaskRewardSummary:
        if self.policy == "binary":
            if self.pass_count is None or self.pass_count > self.sample_count:
                raise ValueError("binary task summary requires a bounded pass count")
            if self.aggregate != _ratio(self.pass_count, self.sample_count):
                raise ValueError("binary task aggregate disagrees with its counts")
        elif self.pass_count is not None:
            raise ValueError("numeric task summary cannot contain a pass count")
        return self


class SectionRewardSummary(FrozenRecord):
    schema_version: Literal[1] = 1
    policy: RewardPolicy
    aggregate_kind: Literal["numeric_mean", "binary_pass_rate"]
    task_count: int = Field(ge=1)
    sample_count: int = Field(ge=1)
    pass_count: int | None = Field(default=None, ge=0)
    aggregate: DecimalString | None
    trials: tuple[TrialReward, ...]
    tasks: tuple[TaskRewardSummary, ...]

    @model_validator(mode="after")
    def validate_summary(self) -> SectionRewardSummary:
        expected_kind = (
            "binary_pass_rate" if self.policy == "binary" else "numeric_mean"
        )
        if self.aggregate_kind != expected_kind:
            raise ValueError("section aggregate kind disagrees with reward policy")
        if self.task_count != len(self.tasks):
            raise ValueError("section task count disagrees with task summaries")
        if self.sample_count != sum(task.sample_count for task in self.tasks):
            raise ValueError("section sample count disagrees with task summaries")
        ordered_trials = tuple(
            sorted(self.trials, key=lambda item: (item.task_id, item.trial_name))
        )
        if ordered_trials != self.trials:
            raise ValueError("trial rewards are not canonically ordered")
        if tuple(sorted(self.tasks, key=lambda item: item.task_id)) != self.tasks:
            raise ValueError("task summaries are not canonically ordered")
        if any(task.policy != self.policy for task in self.tasks) or any(
            trial.policy != self.policy for trial in self.trials
        ):
            raise ValueError("section summary mixes reward policies")
        if len({(trial.task_id, trial.trial_name) for trial in self.trials}) != len(
            self.trials
        ):
            raise ValueError("trial reward identities must be unique")
        trials_by_task: dict[str, list[TrialReward]] = defaultdict(list)
        for trial in self.trials:
            if self.policy == "binary" and trial.value not in {"0", "1"}:
                raise ValueError("binary trial reward must be exactly string 0 or 1")
            trials_by_task[trial.task_id].append(trial)
        all_values: list[Decimal] = []
        for task in self.tasks:
            trials = trials_by_task.pop(task.task_id, [])
            if self.policy == "binary" and len(trials) != task.sample_count:
                raise ValueError(
                    "binary task sample count disagrees with trial rewards"
                )
            if len(trials) > task.sample_count:
                raise ValueError("task reward count exceeds its sample count")
            values = [Decimal(trial.value) for trial in trials]
            if self.policy == "binary":
                pass_count = sum(trial.value == "1" for trial in trials)
                if task.pass_count != pass_count:
                    raise ValueError("task pass count disagrees with trial rewards")
                if task.aggregate != _ratio(pass_count, len(trials)):
                    raise ValueError("task aggregate disagrees with trial rewards")
            elif task.aggregate != _mean(values):
                raise ValueError("task aggregate disagrees with trial rewards")
            all_values.extend(values)
        if trials_by_task:
            raise ValueError("trial reward names an unknown summarized task")
        if self.policy == "binary":
            if self.pass_count is None or self.pass_count > self.sample_count:
                raise ValueError("binary section summary requires a bounded pass count")
            if len(self.trials) != self.sample_count:
                raise ValueError("binary summary requires one reward per sample")
            pass_count = sum(trial.value == "1" for trial in self.trials)
            if self.pass_count != pass_count:
                raise ValueError("section pass count disagrees with trial rewards")
            if self.pass_count != sum(task.pass_count or 0 for task in self.tasks):
                raise ValueError("section pass count disagrees with task summaries")
            if self.aggregate != _ratio(pass_count, len(self.trials)):
                raise ValueError("section aggregate disagrees with trial rewards")
        elif self.pass_count is not None:
            raise ValueError("numeric section summary cannot contain a pass count")
        elif self.aggregate != _mean(all_values):
            raise ValueError("section aggregate disagrees with trial rewards")
        return self


def validate_summary_for_plan(
    summary: SectionRewardSummary, plan: ResolvedPlan
) -> SectionRewardSummary:
    if not plan.trials:
        raise ValueError("reward summary cannot bind an empty plan")
    policies = {trial.reward_policy for trial in plan.trials}
    if policies != {summary.policy}:
        raise ValueError("reward summary policy disagrees with the resolved plan")
    expected_ids = tuple(sorted(trial.task_id for trial in plan.trials))
    if tuple(task.task_id for task in summary.tasks) != expected_ids:
        raise ValueError("reward summary tasks disagree with the resolved plan")
    if any(task.sample_count != plan.harbor.attempts for task in summary.tasks):
        raise ValueError("reward summary attempts disagree with the resolved plan")
    return summary


class ControllerResultV1(FrozenRecord):
    schema_version: Literal[1]
    run_id: RecordIdentifier
    attempt_id: RecordIdentifier
    outcome: Literal["succeeded", "failed", "cancelled"]
    harbor_version: Literal["0.22.0"]
    modal_version: Literal["1.5.4"]
    tetrabench_version: NonEmptyString


class ControllerResultV2(FrozenRecord):
    schema_version: Literal[2]
    run_id: RecordIdentifier
    attempt_id: RecordIdentifier
    outcome: Literal["succeeded", "failed", "cancelled"]
    request_sha256: Sha256
    plan_sha256: Sha256
    harbor_version: Literal["0.22.0"]
    modal_version: Literal["1.5.4"]
    tetrabench_version: NonEmptyString
    summary: SectionRewardSummary


def _resolved_task_path(context_root: Path, harbor_task: str) -> Path:
    root = context_root.resolve(strict=True)
    candidate = root / harbor_task
    if candidate.is_symlink():
        raise ValueError("resolved task path is a symlink")
    resolved = candidate.resolve(strict=True)
    if candidate.absolute() != resolved:
        raise ValueError("resolved task path contains an alias or symlink")
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(
            "resolved task path escaped the materialized context"
        ) from error
    if not resolved.is_dir():
        raise ValueError("resolved task path is not a directory")
    return resolved


def _map_trials(
    plan: ResolvedPlan,
    context_root: Path,
    artifacts: NativeJobArtifacts,
) -> tuple[tuple[NativeTrialArtifacts, ResolvedTrial], ...]:
    expected: dict[Path, ResolvedTrial] = {}
    for trial in plan.trials:
        path = _resolved_task_path(context_root, trial.harbor_task)
        if path in expected:
            raise ValueError("multiple resolved trials map to one Harbor task path")
        expected[path] = trial

    mapped: list[tuple[NativeTrialArtifacts, ResolvedTrial]] = []
    counts: dict[str, int] = defaultdict(int)
    for native in artifacts.trials:
        task_path = native.config.task.path
        if task_path is None or task_path.is_symlink():
            raise ValueError("Harbor trial task path is missing or unsafe")
        resolved = task_path.resolve(strict=True)
        if task_path.absolute() != resolved:
            raise ValueError("Harbor trial task path contains an alias or symlink")
        try:
            resolved.relative_to(context_root.resolve(strict=True))
        except ValueError as error:
            raise ValueError(
                "Harbor trial task path escaped the materialized context"
            ) from error
        trial = expected.get(resolved)
        if trial is None:
            raise ValueError("Harbor trial task path is unknown to the resolved plan")
        counts[trial.task_id] += 1
        mapped.append((native, trial))
    expected_attempts = plan.harbor.attempts
    if any(counts[trial.task_id] != expected_attempts for trial in plan.trials):
        raise ValueError("Harbor trial attempts do not match the resolved plan")
    return tuple(mapped)


def _validated_reward_dictionary(
    raw: tuple[tuple[str, int | float], ...] | None,
) -> dict[str, int | float]:
    if raw is None:
        return {}
    rewards = dict(raw)
    for name, value in rewards.items():
        if not name:
            raise ValueError("Harbor reward names must be non-empty")
        if type(value) not in {int, float} or (
            type(value) is float and not math.isfinite(value)
        ):
            raise ValueError("every Harbor reward must be an exact finite number")
    return rewards


def _validated_rewards(native: NativeTrialArtifacts) -> dict[str, int | float]:
    rewards = _validated_reward_dictionary(native.rewards)
    for step_rewards in native.step_rewards:
        _validated_reward_dictionary(step_rewards)
    return rewards


def summarize_rewards(
    plan: ResolvedPlan,
    context_root: Path,
    artifacts: NativeJobArtifacts,
) -> SectionRewardSummary:
    mapped = _map_trials(plan, context_root, artifacts)
    policies = {trial.reward_policy for _native, trial in mapped}
    if len(policies) != 1:
        raise ValueError("a section cannot mix reward policies")
    policy = policies.pop()
    grouped: dict[str, list[tuple[NativeTrialArtifacts, Decimal | None]]] = defaultdict(
        list
    )
    trial_rewards: list[TrialReward] = []
    for native, trial in mapped:
        rewards = _validated_rewards(native)
        primary = rewards.get("reward")
        if policy == "binary":
            if type(primary) is not int or primary not in {0, 1}:
                raise ValueError("binary primary reward must be exact integer 0 or 1")
        value = Decimal(str(primary)) if primary is not None else None
        grouped[trial.task_id].append((native, value))
        if value is not None:
            trial_rewards.append(
                TrialReward(
                    task_id=trial.task_id,
                    trial_name=native.trial_name,
                    policy=policy,
                    value=canonical_decimal(value),
                )
            )

    task_summaries: list[TaskRewardSummary] = []
    all_values: list[Decimal] = []
    for task_id in sorted(grouped):
        samples = grouped[task_id]
        values = [value for _native, value in samples if value is not None]
        if policy == "numeric" and values and len(values) != len(samples):
            raise ValueError("numeric primary reward is mixed missing and present")
        all_values.extend(values)
        pass_count = sum(value == 1 for value in values) if policy == "binary" else None
        aggregate = (
            _ratio(pass_count, len(samples))
            if pass_count is not None
            else _mean(values)
        )
        task_summaries.append(
            TaskRewardSummary(
                task_id=task_id,
                policy=policy,
                sample_count=len(samples),
                pass_count=pass_count,
                aggregate=aggregate,
            )
        )
    section_pass_count = (
        sum(value == 1 for value in all_values) if policy == "binary" else None
    )
    aggregate = (
        _ratio(section_pass_count, len(mapped))
        if section_pass_count is not None
        else _mean(all_values)
    )
    return SectionRewardSummary(
        policy=policy,
        aggregate_kind=("binary_pass_rate" if policy == "binary" else "numeric_mean"),
        task_count=len(task_summaries),
        sample_count=len(mapped),
        pass_count=section_pass_count,
        aggregate=aggregate,
        trials=tuple(
            sorted(trial_rewards, key=lambda item: (item.task_id, item.trial_name))
        ),
        tasks=tuple(task_summaries),
    )
