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
    ProjectConfig,
    ResolvedContextFile,
    ResolvedPlan,
    ResolvedTaskSelection,
    ResolvedTrial,
)


def canonical_model_bytes(model: BaseModel) -> bytes:
    return dumps_canonical_json(model.model_dump(mode="json", by_alias=True))


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
) -> ResolvedPlan:
    config = load_project_config(root, profile=profile)
    catalog = load_catalog(root, config.catalog_path)
    tasks = select_tasks(get_section(catalog, section_name), config.selection)
    return resolved_plan_from_selection(
        config,
        section_name,
        tasks,
        context=(resolve_context(root, config.context) if context is None else context),
    )


def resolved_plan_from_selection(
    config: ProjectConfig,
    section_name: SectionName,
    tasks: tuple[CatalogTask, ...],
    *,
    context: tuple[ResolvedContextFile, ...],
) -> ResolvedPlan:
    """Build one plan from an already selected catalog and sealed context snapshot."""
    trials = tuple(
        ResolvedTrial(task_id=task.id, harbor_task=task.harbor_task) for task in tasks
    )
    empty_reason = f"section {section_name!r} contains no selected tasks"
    reasons = () if trials else (empty_reason,)
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
    )


def plan_digest(plan: ResolvedPlan) -> str:
    return sha256_hex(canonical_model_bytes(plan))
