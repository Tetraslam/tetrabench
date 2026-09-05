from __future__ import annotations

from pathlib import Path

import pytest

from tetrabench.catalog import get_section, load_catalog, select_tasks
from tetrabench.models import TaskSelection

ROOT = Path(__file__).parents[1]


def test_local_catalog_has_admitted_authority_fencing_task() -> None:
    catalog = load_catalog(ROOT, "benchmarks/catalog.toml")

    systems = get_section(catalog, "systems-design")
    github = get_section(catalog, "github-workflow")
    assert systems.description == (
        "Implement compact systems, then verify lifecycle, consistency, migration, and "
        "authorization invariants from clean state under fixed fault schedules."
    )
    assert github.description == (
        "Complete single-pull-request workflows in real local Git against a "
        "deterministic task-local forge, then reconstruct and verify them from clean "
        "snapshots."
    )
    assert [task.id for task in systems.tasks] == ["authority-fencing"]
    assert systems.tasks[0].harbor_task == (
        "benchmarks/tasks/systems-design/authority-fencing"
    )
    assert systems.tasks[0].reward_policy == "binary"
    assert len(github.tasks) == 0
    assert [task.id for task in select_tasks(systems, TaskSelection())] == [
        "authority-fencing"
    ]


def test_unknown_excluded_task_fails() -> None:
    catalog = load_catalog(ROOT, "benchmarks/catalog.toml")

    with pytest.raises(ValueError, match="absent from catalog: missing"):
        select_tasks(
            get_section(catalog, "systems-design"),
            TaskSelection(exclude=["missing"]),
        )
