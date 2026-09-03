from __future__ import annotations

import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from tetrabench import authoring
from tetrabench.canonical_json import dumps_canonical_json, loads_canonical_json
from tetrabench.catalog import get_section, load_catalog, select_tasks
from tetrabench.cli import app
from tetrabench.config import load_project_config
from tetrabench.models import TaskSelection

runner = CliRunner()


def _init(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    result = runner.invoke(app, ["init", str(project), "--json"])
    assert result.exit_code == 0, result.stderr
    assert _payload(result.stdout) == {
        "directory": str(project),
        "schema_version": 1,
        "status": "created",
    }
    return project


def _payload(output: str) -> dict[str, object]:
    raw = output.removesuffix("\n").encode()
    value = loads_canonical_json(raw)
    assert isinstance(value, dict)
    assert dumps_canonical_json(value) == raw
    return cast(dict[str, object], value)


def _tree_bytes(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_init_creates_canonical_runnable_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    monkeypatch.chdir(project)

    doctor = runner.invoke(app, ["doctor", "--json"])
    plan = runner.invoke(app, ["plan", "systems-design", "--json"])
    validation = runner.invoke(
        app,
        [
            "task",
            "validate",
            "benchmarks/tasks/systems-design/hello-tetrabench",
            "--json",
        ],
    )

    assert doctor.exit_code == 0, doctor.stderr
    assert plan.exit_code == 0, plan.stderr
    assert validation.exit_code == 0, validation.stderr
    assert _payload(plan.stdout)["runnable"] is True
    report = _payload(validation.stdout)
    assert report["status"] == "ok"
    assert report["file_count"] == 6
    assert isinstance(report["total_bytes"], int)
    assert report["total_bytes"] > 0
    assert (project / "benchmarks/systems-design/README.md").is_file()
    assert (project / "benchmarks/github-workflow/README.md").is_file()
    assert (project / "benchmarks/tasks/github-workflow").is_dir()
    for relative in (
        "solution/solve.sh",
        "tests/test.sh",
    ):
        mode = project / "benchmarks/tasks/systems-design/hello-tetrabench" / relative
        assert stat.S_IMODE(mode.stat().st_mode) == 0o755


def test_task_new_stays_unlisted_then_add_preserves_catalog_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    catalog_path = project / "benchmarks/catalog.toml"
    before = catalog_path.read_bytes() + b"\n# user comment\n"
    catalog_path.write_bytes(before)

    created = runner.invoke(
        app,
        [
            "task",
            "new",
            "github-workflow",
            "review-pr",
            "--project",
            str(project),
            "--json",
        ],
    )
    assert created.exit_code == 0, created.stderr
    assert _payload(created.stdout)["fixture"] == (
        "benchmarks/tasks/github-workflow/review-pr"
    )
    config = load_project_config(project)
    catalog = load_catalog(project, config.catalog_path)
    assert get_section(catalog, "github-workflow").tasks == []

    monkeypatch.chdir(project)
    added = runner.invoke(
        app,
        [
            "task",
            "add",
            "github-workflow",
            "review-pr",
            "benchmarks/tasks/github-workflow/review-pr",
            "--json",
        ],
    )
    assert added.exit_code == 0, added.stderr
    assert _payload(added.stdout) == {
        "fixture": "benchmarks/tasks/github-workflow/review-pr",
        "schema_version": 1,
        "section": "github-workflow",
        "status": "added",
        "task_id": "review-pr",
    }
    after = catalog_path.read_bytes()
    assert after.startswith(before)
    assert b"# user comment\n" in after
    catalog = load_catalog(project, config.catalog_path)
    selected = select_tasks(get_section(catalog, "github-workflow"), TaskSelection())
    assert [(task.id, task.harbor_task, task.reward_policy) for task in selected] == [
        (
            "review-pr",
            "benchmarks/tasks/github-workflow/review-pr",
            "binary",
        )
    ]


@pytest.mark.parametrize(
    "fixture",
    ["../outside", "./benchmarks/task", "/absolute/task", "benchmarks//task"],
)
def test_validate_rejects_non_normalized_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
) -> None:
    project = _init(tmp_path)
    monkeypatch.chdir(project)
    before = _tree_bytes(project)

    result = runner.invoke(app, ["task", "validate", fixture, "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert _tree_bytes(project) == before


def test_validate_rejects_symlink_and_is_read_only_without_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    fixture = project / "benchmarks/tasks/systems-design/hello-tetrabench"
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda *_args: pytest.fail("validation constructed S3"),
    )
    monkeypatch.setattr(
        "tetrabench.cli.ModalControllerClient",
        lambda *_args: pytest.fail("validation constructed Modal"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("validation called Docker"),
    )
    before = _tree_bytes(project)

    valid = runner.invoke(
        app,
        ["task", "validate", fixture.relative_to(project).as_posix(), "--json"],
    )
    assert valid.exit_code == 0, valid.stderr
    assert _tree_bytes(project) == before

    (fixture / "link").symlink_to("instruction.md")
    invalid = runner.invoke(
        app,
        ["task", "validate", fixture.relative_to(project).as_posix(), "--json"],
    )
    assert invalid.exit_code == 2
    assert "symlink" in str(_payload(invalid.stderr)["error"])


@pytest.mark.parametrize(
    ("relative_path", "replacement", "swap"),
    [
        ("instruction.md", b"# changed but valid\n", False),
        ("task.toml", b"not valid Harbor TOML\n", True),
    ],
)
def test_validate_rejects_source_mutation_during_private_harbor_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    replacement: bytes,
    swap: bool,
) -> None:
    project = _init(tmp_path)
    fixture = project / "benchmarks/tasks/systems-design/hello-tetrabench"
    source = fixture / relative_path
    native_validate = authoring.Harbor022Api.validate_task

    def mutate_source_then_validate(*, path: Path) -> None:
        assert path != fixture
        assert _tree_bytes(path) == _tree_bytes(fixture)
        if swap:
            competing = source.with_name(f".{source.name}.replacement")
            competing.write_bytes(replacement)
            competing.chmod(stat.S_IMODE(source.stat().st_mode))
            competing.replace(source)
        else:
            source.write_bytes(replacement)
        native_validate(path=path)

    monkeypatch.setattr(
        authoring.Harbor022Api,
        "validate_task",
        staticmethod(mutate_source_then_validate),
    )

    with pytest.raises(RuntimeError, match="fixture changed"):
        authoring.validate_fixture(project, fixture.relative_to(project).as_posix())


def test_validate_rejects_invalid_sealed_bytes_even_if_source_is_swapped_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    fixture = project / "benchmarks/tasks/systems-design/hello-tetrabench"
    source = fixture / "task.toml"
    valid = source.read_bytes()
    source.write_bytes(b"not valid Harbor TOML\n")
    native_validate = authoring.Harbor022Api.validate_task

    def restore_source_then_validate(*, path: Path) -> None:
        assert path != fixture
        assert (path / "task.toml").read_bytes() == b"not valid Harbor TOML\n"
        source.write_bytes(valid)
        native_validate(path=path)

    monkeypatch.setattr(
        authoring.Harbor022Api,
        "validate_task",
        staticmethod(restore_source_then_validate),
    )

    with pytest.raises(ValueError):
        authoring.validate_fixture(project, fixture.relative_to(project).as_posix())


def test_existing_and_symlink_destinations_are_never_overwritten(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    init_result = runner.invoke(app, ["init", str(existing), "--json"])
    assert init_result.exit_code == 2
    assert sentinel.read_text(encoding="utf-8") == "keep"

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    link_result = runner.invoke(app, ["init", str(link), "--json"])
    assert link_result.exit_code == 2
    assert link.is_symlink()
    assert list(target.iterdir()) == []


def test_task_new_rejects_existing_and_symlink_destinations(
    tmp_path: Path,
) -> None:
    project = _init(tmp_path)
    parent = project / "benchmarks/tasks/github-workflow"
    existing = parent / "existing"
    existing.mkdir()
    sentinel = existing / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "task",
            "new",
            "github-workflow",
            "existing",
            "--project",
            str(project),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert sentinel.read_text(encoding="utf-8") == "keep"

    link = parent / "linked"
    link.symlink_to(existing, target_is_directory=True)
    linked = runner.invoke(
        app,
        [
            "task",
            "new",
            "github-workflow",
            "linked",
            "--project",
            str(project),
            "--json",
        ],
    )
    assert linked.exit_code == 2
    assert link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("section", "task_id"),
    [("unknown", "valid"), ("systems-design", "../escape"), ("systems-design", "")],
)
def test_task_new_rejects_invalid_inputs_without_output(
    tmp_path: Path,
    section: str,
    task_id: str,
) -> None:
    project = _init(tmp_path)
    before = _tree_bytes(project)
    result = runner.invoke(
        app,
        ["task", "new", section, task_id, "--project", str(project), "--json"],
    )
    assert result.exit_code == 2
    assert _tree_bytes(project) == before


def test_injected_task_creation_failure_leaves_no_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    destination = project / "benchmarks/tasks/systems-design/injected"

    def fail_after_first(_logical_path: str) -> None:
        raise OSError("injected creation failure")

    monkeypatch.setattr(authoring, "_authoring_creation_checkpoint", fail_after_first)
    result = runner.invoke(
        app,
        [
            "task",
            "new",
            "systems-design",
            "injected",
            "--project",
            str(project),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert not destination.exists()
    assert not list(destination.parent.glob(".injected.*"))


def test_init_creation_failure_and_destination_race_leave_no_partial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "failed-project"

    def fail(_logical_path: str) -> None:
        raise OSError("injected creation failure")

    monkeypatch.setattr(authoring, "_authoring_creation_checkpoint", fail)
    failed = runner.invoke(app, ["init", str(destination), "--json"])
    assert failed.exit_code == 2
    assert not destination.exists()
    assert not list(tmp_path.glob(".failed-project.*"))

    raced = tmp_path / "raced-project"
    injected = False

    def create_competing_destination(_logical_path: str) -> None:
        nonlocal injected
        if not injected:
            raced.mkdir()
            (raced / "sentinel").write_text("keep", encoding="utf-8")
            injected = True

    monkeypatch.setattr(
        authoring, "_authoring_creation_checkpoint", create_competing_destination
    )
    race = runner.invoke(app, ["init", str(raced), "--json"])
    assert race.exit_code == 2
    assert (raced / "sentinel").read_text(encoding="utf-8") == "keep"
    assert list(raced.iterdir()) == [raced / "sentinel"]
    assert not list(tmp_path.glob(".raced-project.*"))


def test_add_refuses_duplicates_and_invalid_fixture_without_catalog_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    monkeypatch.chdir(project)
    catalog = project / "benchmarks/catalog.toml"
    before = catalog.read_bytes()

    duplicate_id = runner.invoke(
        app,
        [
            "task",
            "add",
            "github-workflow",
            "hello-tetrabench",
            "benchmarks/tasks/systems-design/hello-tetrabench",
            "--json",
        ],
    )
    assert duplicate_id.exit_code == 2
    assert catalog.read_bytes() == before

    duplicate_fixture = runner.invoke(
        app,
        [
            "task",
            "add",
            "github-workflow",
            "different-id",
            "benchmarks/tasks/systems-design/hello-tetrabench",
            "--json",
        ],
    )
    assert duplicate_fixture.exit_code == 2
    assert catalog.read_bytes() == before

    missing = runner.invoke(
        app,
        [
            "task",
            "add",
            "github-workflow",
            "missing",
            "benchmarks/tasks/github-workflow/missing",
            "--json",
        ],
    )
    assert missing.exit_code == 2
    assert catalog.read_bytes() == before


def test_add_replace_failure_keeps_original_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    created = runner.invoke(
        app,
        [
            "task",
            "new",
            "github-workflow",
            "atomic-add",
            "--project",
            str(project),
        ],
    )
    assert created.exit_code == 0, created.stderr
    monkeypatch.chdir(project)
    catalog = project / "benchmarks/catalog.toml"
    before = catalog.read_bytes()
    monkeypatch.setattr(
        authoring.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
    )

    result = runner.invoke(
        app,
        [
            "task",
            "add",
            "github-workflow",
            "atomic-add",
            "benchmarks/tasks/github-workflow/atomic-add",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert catalog.read_bytes() == before
    assert list(catalog.parent.glob(".catalog.toml.*")) == [
        catalog.parent / ".catalog.toml.lock"
    ]


def test_add_rejects_catalog_inside_fixture_before_lock_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    fixture = "benchmarks/tasks/systems-design/hello-tetrabench"
    fixture_path = project / fixture
    nested_catalog = fixture_path / "catalog.toml"
    nested_catalog.write_bytes((project / "benchmarks/catalog.toml").read_bytes())
    (project / "tetrabench.toml").write_text(
        f'''schema_version = 1
catalog_path = "{fixture}/catalog.toml"

[controller]
kind = "local"

[execution]
kind = "docker"
''',
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        ["task", "add", "systems-design", "nested", fixture, "--json"],
    )

    assert result.exit_code == 2
    assert "outside the task fixture" in str(_payload(result.stderr)["error"])
    assert not (fixture_path / ".catalog.toml.lock").exists()
    catalog = load_catalog(project, f"{fixture}/catalog.toml")
    assert [task.id for task in catalog.sections.systems_design.tasks] == [
        "hello-tetrabench"
    ]


@pytest.mark.parametrize(
    ("relative_path", "replacement", "swap"),
    [
        ("instruction.md", b"# changed but valid\n", False),
        ("task.toml", b"not valid Harbor TOML\n", True),
    ],
)
def test_add_revalidates_exact_initial_fixture_before_catalog_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    replacement: bytes,
    swap: bool,
) -> None:
    project = _init(tmp_path)
    fixture = "benchmarks/tasks/github-workflow/raced"
    authoring.create_task(project, "github-workflow", "raced")
    source = project / fixture / relative_path
    catalog = project / "benchmarks/catalog.toml"
    before = catalog.read_bytes()
    validate_snapshot = authoring._validate_fixture_snapshot
    calls = 0

    def mutate_after_initial(root: Path, logical_path: str) -> authoring.SealedContext:
        nonlocal calls
        snapshot = validate_snapshot(root, logical_path)
        calls += 1
        if calls == 1:
            if swap:
                competing = source.with_name(f".{source.name}.replacement")
                competing.write_bytes(replacement)
                competing.chmod(stat.S_IMODE(source.stat().st_mode))
                competing.replace(source)
            else:
                source.write_bytes(replacement)
        return snapshot

    monkeypatch.setattr(authoring, "_validate_fixture_snapshot", mutate_after_initial)

    with pytest.raises((RuntimeError, ValueError)):
        authoring.add_task(project, "github-workflow", "raced", fixture)

    assert catalog.read_bytes() == before
    assert b'id = "raced"' not in catalog.read_bytes()


def test_cooperative_concurrent_adds_preserve_both_catalog_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    fixtures = {
        task_id: authoring.create_task(project, "github-workflow", task_id)[1]
        for task_id in ("concurrent-a", "concurrent-b")
    }
    barrier = threading.Barrier(2)
    flock = authoring.fcntl.flock

    def synchronized_flock(descriptor: int, operation: int) -> None:
        if operation == authoring.fcntl.LOCK_EX:
            barrier.wait(timeout=10)
        flock(descriptor, operation)

    monkeypatch.setattr(authoring.fcntl, "flock", synchronized_flock)

    def add(task_id: str) -> None:
        authoring.add_task(
            project,
            "github-workflow",
            task_id,
            fixtures[task_id],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(add, task_id) for task_id in fixtures]
        for future in futures:
            future.result(timeout=30)

    config = load_project_config(project)
    catalog = load_catalog(project, config.catalog_path)
    tasks = get_section(catalog, "github-workflow").tasks
    assert sorted(task.id for task in tasks) == ["concurrent-a", "concurrent-b"]
    catalog_bytes = (project / "benchmarks/catalog.toml").read_bytes()
    assert catalog_bytes.count(b'id = "concurrent-a"') == 1
    assert catalog_bytes.count(b'id = "concurrent-b"') == 1
    lock_path = project / "benchmarks/.catalog.toml.lock"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_uncooperative_catalog_change_is_detected_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _init(tmp_path)
    fixture = authoring.create_task(project, "github-workflow", "uncooperative")[1]
    catalog = project / "benchmarks/catalog.toml"
    original = catalog.read_bytes()
    competing = original + b"\n# uncooperative writer\n"
    validate_snapshot = authoring._validate_fixture_snapshot
    calls = 0

    def change_catalog_after_final_validation(
        root: Path, logical_path: str
    ) -> authoring.SealedContext:
        nonlocal calls
        snapshot = validate_snapshot(root, logical_path)
        calls += 1
        if calls == 2:
            catalog.write_bytes(competing)
        return snapshot

    monkeypatch.setattr(
        authoring,
        "_validate_fixture_snapshot",
        change_catalog_after_final_validation,
    )

    with pytest.raises(RuntimeError, match="catalog changed"):
        authoring.add_task(
            project,
            "github-workflow",
            "uncooperative",
            fixture,
        )

    assert catalog.read_bytes() == competing
    assert b'id = "uncooperative"' not in catalog.read_bytes()


@pytest.mark.docker
def test_initialized_starter_runs_through_public_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert docker.returncode == 0, "Docker daemon is required for the test suite"
    project = _init(tmp_path)
    output = tmp_path / "output"
    monkeypatch.chdir(project)

    result = runner.invoke(
        app,
        ["run", "systems-design", "--output", str(output), "--json"],
    )

    assert result.exit_code == 0, result.stderr
    report = _payload(result.stdout)
    assert report["outcome"] == "succeeded"
    assert report["reward"] == "1"
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["policy"] == "binary"
    assert summary["pass_count"] == 1
    assert (output / "harbor-job/result.json").is_file()
