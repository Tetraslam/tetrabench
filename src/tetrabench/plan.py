"""Resolve canonical, secret-free execution plans."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from tetrabench.canonical_json import (
    dumps_canonical_json,
    loads_canonical_json,
    sha256_hex,
)
from tetrabench.catalog import SectionName, get_section, load_catalog, select_tasks
from tetrabench.config import load_project_config
from tetrabench.context import resolve_context
from tetrabench.models import (
    CatalogTask,
    ConfigOverrides,
    ProjectConfig,
    ResolvedContextFile,
    ResolvedPlan,
    ResolvedTaskSelection,
    ResolvedTrial,
    is_legacy_reward_plan,
)


def canonical_model_bytes(model: BaseModel) -> bytes:
    value = model.model_dump(mode="json", by_alias=True)
    plan = model if isinstance(model, ResolvedPlan) else getattr(model, "plan", None)
    if isinstance(plan, ResolvedPlan) and "harness" not in plan.model_fields_set:
        plan_value = value if isinstance(model, ResolvedPlan) else value["plan"]
        plan_value.pop("harness", None)
    if isinstance(plan, ResolvedPlan) and is_legacy_reward_plan(plan):
        trials = (
            value["trials"]
            if isinstance(model, ResolvedPlan)
            else value["plan"]["trials"]
        )
        for trial in trials:
            trial.pop("reward_policy", None)
    return dumps_canonical_json(value)


def parse_canonical_model[ModelT: BaseModel](
    data: bytes,
    model_type: type[ModelT],
) -> ModelT:
    """Validate canonical bytes and then the strict record schema."""
    loads_canonical_json(data)
    return model_type.model_validate_json(data, strict=True)


def resolve_plan(
    root: Path,
    section_name: SectionName,
    profile: str | None = None,
    *,
    context: tuple[ResolvedContextFile, ...] | None = None,
    overrides: ConfigOverrides | None = None,
    allow_online_auth: bool = False,
) -> ResolvedPlan:
    config = load_project_config(root, profile=profile, overrides=overrides)
    catalog = load_catalog(root, config.catalog_path)
    tasks = select_tasks(get_section(catalog, section_name), config.selection)
    return resolved_plan_from_selection(
        config,
        section_name,
        tasks,
        context=(resolve_context(root, config.context) if context is None else context),
        allow_online_auth=allow_online_auth,
    )


def resolved_plan_from_selection(
    config: ProjectConfig,
    section_name: SectionName,
    tasks: tuple[CatalogTask, ...],
    *,
    context: tuple[ResolvedContextFile, ...],
    allow_online_auth: bool = False,
) -> ResolvedPlan:
    """Build one plan from an already selected catalog and sealed context snapshot."""
    trials = tuple(
        ResolvedTrial(
            task_id=task.id,
            harbor_task=task.harbor_task,
            reward_policy=task.reward_policy,
        )
        for task in tasks
    )
    empty_reason = f"section {section_name!r} contains no selected tasks"
    reasons = () if trials else (empty_reason,)
    from tetrabench.harnesses import seal_harness

    harness = {}
    if config.harness is not None:
        if config.harness.native_config and config.harness.native_config.path:
            raise ValueError(
                "native harness paths must be sealed before plan resolution"
            )
        from tetrabench.auth_selection import resolve_harness_auth

        selected = resolve_harness_auth(config.harness, allow_online=allow_online_auth)
        harness["harness"] = seal_harness(selected, Path.cwd())
    return ResolvedPlan(
        schema_version=1,
        section=section_name,
        controller=config.controller.model_dump(mode="python"),
        execution=config.execution.model_dump(mode="python"),
        storage=(
            config.storage.model_dump(mode="python")
            if config.storage is not None
            else None
        ),
        selection=ResolvedTaskSelection(
            include=tuple(config.selection.include),
            exclude=tuple(config.selection.exclude),
        ),
        harbor=config.harbor.model_dump(mode="python"),
        context=context,
        trials=trials,
        runnable=bool(trials),
        not_runnable_reasons=reasons,
        **harness,
    )


def plan_digest(plan: ResolvedPlan) -> str:
    return sha256_hex(canonical_model_bytes(plan))
