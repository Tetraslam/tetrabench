from __future__ import annotations

from pathlib import Path

import pytest

from tetrabench.catalog import get_section, load_catalog, select_tasks
from tetrabench.models import TaskSelection

ROOT = Path(__file__).parents[1]


def test_local_catalog_has_two_empty_sections() -> None:
    catalog = load_catalog(ROOT, "benchmarks/catalog.toml")

    assert len(get_section(catalog, "systems-design").tasks) == 0
    assert len(get_section(catalog, "github-workflow").tasks) == 0
    assert (
        select_tasks(
            get_section(catalog, "systems-design"),
            TaskSelection(),
        )
        == ()
    )


def test_unknown_excluded_task_fails() -> None:
    catalog = load_catalog(ROOT, "benchmarks/catalog.toml")

    with pytest.raises(ValueError, match="absent from catalog: missing"):
        select_tasks(
            get_section(catalog, "systems-design"),
            TaskSelection(exclude=["missing"]),
        )
