"""Local benchmark catalog loading and task selection."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from tetrabench.models import Catalog, CatalogSection, TaskSelection

SectionName = Literal["systems-design", "github-workflow"]


def load_catalog(
    root: Path, catalog_path: str, *, catalog_data: bytes | None = None
) -> Catalog:
    configured_path = Path(catalog_path)
    path = configured_path if configured_path.is_absolute() else root / configured_path
    try:
        if catalog_data is not None:
            return Catalog.model_validate(tomllib.loads(catalog_data.decode("utf-8")))
        with path.open("rb") as stream:
            return Catalog.model_validate(tomllib.load(stream))
    except FileNotFoundError as error:
        raise ValueError(f"catalog does not exist: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid catalog TOML in {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise ValueError(f"invalid UTF-8 in catalog {path}") from error


def get_section(catalog: Catalog, name: SectionName) -> CatalogSection:
    if name == "systems-design":
        return catalog.sections.systems_design
    return catalog.sections.github_workflow


def select_tasks(section: CatalogSection, selection: TaskSelection) -> tuple:
    tasks = section.tasks
    known = {task.id for task in tasks}
    missing = (set(selection.include) | set(selection.exclude)) - known
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ValueError(f"selected task IDs are absent from catalog: {missing_text}")
    included = set(selection.include) if selection.include else known
    return tuple(
        task
        for task in tasks
        if task.id in included and task.id not in selection.exclude
    )
