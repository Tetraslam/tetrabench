from __future__ import annotations

import os
import shutil
import stat
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import tetrabench.context as context_module
import tetrabench.submission as submission_module
from tetrabench.canonical_json import sha256_hex
from tetrabench.cli import app
from tetrabench.context import (
    FixtureLimitError,
    FixtureMutationError,
    FixtureTrustError,
    open_project_root,
    seal_context,
)
from tetrabench.controller import ControllerInvocation
from tetrabench.controller_runtime import attempt_paths
from tetrabench.harbor import ATTEMPT_LABEL, PLAN_LABEL, RUN_LABEL
from tetrabench.harbor_runner import compile_harbor_job
from tetrabench.models import ContextConfig
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.storage import request_key
from tetrabench.submission import prepare_submission

runner = CliRunner()


def _project(
    root: Path,
    tasks: tuple[tuple[str, str], ...] = (("one", "tasks/one"),),
    *,
    context: str = "",
) -> Path:
    root.mkdir()
    task_values = ", ".join(
        f'{{id = "{task_id}", harbor_task = "{task_root}"}}'
        for task_id, task_root in tasks
    )
    (root / "tetrabench.toml").write_text(
        f"""\
schema_version = 1
catalog_path = "catalog.toml"
[storage]
provider = "tigris"
bucket = "fixture-bucket"
{context}
""",
        encoding="utf-8",
    )
    (root / "catalog.toml").write_text(
        f"""\
schema_version = 1
[sections.systems-design]
readme = "systems.md"
tasks = [{task_values}]
[sections.github-workflow]
readme = "github.md"
tasks = []
""",
        encoding="utf-8",
    )
    (root / "systems.md").write_text("systems", encoding="utf-8")
    (root / "github.md").write_text("github", encoding="utf-8")
    return root


def _fixture(root: Path, relative: str = "tasks/one") -> Path:
    fixture = root / relative
    (fixture / "nested").mkdir(parents=True)
    (fixture / "task.toml").write_bytes(b"task\n")
    script = fixture / "nested/run.sh"
    script.write_bytes(b"#!/bin/sh\n")
    script.chmod(0o755)
    return fixture


def _identity(root: Path) -> tuple[str, str, str]:
    prepared = prepare_submission(root, "systems-design", run_id="fixture-run")
    return (
        plan_digest(prepared.plan),
        prepared.request.context_manifest_sha256,
        sha256_hex(canonical_model_bytes(prepared.request)),
    )


def _manifest_tree(prepared) -> dict[str, tuple[bytes, int]]:
    by_digest = {
        item.descriptor.sha256: item.content for item in prepared.sealed_context.files
    }
    return {
        item.destination: (by_digest[item.content.sha256], item.mode)
        for item in prepared.sealed_context.manifest.files
    }


def test_submit_uses_one_prepared_config_catalog_and_provider_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path / "project")
    _fixture(root)
    loads = {"catalog": 0, "config": 0}
    spawned: list[tuple[str, str, str | None]] = []
    stores = []
    prepared_results = []

    original_config_loader = submission_module.load_project_config
    original_catalog_loader = submission_module.load_catalog
    original_prepare = submission_module.prepare_submission

    def load_config(*args, **kwargs):
        loads["config"] += 1
        return original_config_loader(*args, **kwargs)

    def load_selected_catalog(*args, **kwargs):
        loads["catalog"] += 1
        return original_catalog_loader(*args, **kwargs)

    def prepare_then_replace(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        prepared_results.append(prepared)
        (root / "tetrabench.toml").write_text(
            """\
schema_version = 1
catalog_path = "replacement.toml"
[controller]
kind = "modal"
app_name = "replacement-app"
function_name = "replacement-function"
secret_name = "replacement-secret"
[execution]
kind = "modal"
[storage]
provider = "aws"
bucket = "replacement-bucket"
region = "us-east-1"
""",
            encoding="utf-8",
        )
        (root / "catalog.toml").write_text(
            "invalid after preparation", encoding="utf-8"
        )
        return prepared

    class Function:
        @staticmethod
        def from_name(app_name, function_name, *, environment_name=None):
            spawned.append((app_name, function_name, environment_name))
            return SimpleNamespace(
                spawn=lambda *_args: SimpleNamespace(object_id="fc-prepared")
            )

    class Service:
        def __init__(self, store, controller, _receipts):
            self.store = store
            self.controller = controller

        def submit(self, prepared):
            storage = prepared.plan.storage
            assert storage is not None
            request_sha256 = sha256_hex(canonical_model_bytes(prepared.request))
            invocation = ControllerInvocation(
                schema_version=1,
                run_id=prepared.request.run_id,
                request_sha256=request_sha256,
                plan_sha256=prepared.request.plan_sha256,
                request_key=request_key(
                    prepared.request.run_id,
                    request_sha256,
                    prefix=storage.prefix,
                ),
                storage=storage,
            )
            call_id = self.controller.spawn(invocation)
            call = SimpleNamespace(call_id=call_id)
            attempt = SimpleNamespace(controller_calls=(call,))
            return SimpleNamespace(
                run_id=prepared.request.run_id,
                request_sha256=request_sha256,
                attempts=(attempt,),
            )

    def create_store(storage):
        stores.append(storage)
        return object()

    monkeypatch.setattr(submission_module, "load_project_config", load_config)
    monkeypatch.setattr(submission_module, "load_catalog", load_selected_catalog)
    monkeypatch.setattr("tetrabench.cli.prepare_submission", prepare_then_replace)
    monkeypatch.setattr(
        "tetrabench.cli.load_project_config",
        lambda *_args, **_kwargs: pytest.fail("submit reread project configuration"),
    )
    monkeypatch.setattr("tetrabench.cli.create_s3_store", create_store)
    monkeypatch.setattr("tetrabench.cli.SubmissionService", Service)
    monkeypatch.setattr("tetrabench.controller.modal.Function", Function)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app, ["submit", "systems-design", "--run-id", "authority-run"]
    )

    assert result.exit_code == 0, result.stderr
    assert loads == {"catalog": 1, "config": 1}
    assert len(stores) == 1
    assert stores[0].provider == "tigris"
    assert stores[0].bucket == "fixture-bucket"
    assert spawned == [("tetrabench", "controller", "tetrabench-default")]
    durable_request = canonical_model_bytes(prepared_results[0].request)
    assert b"tetrabench-default" not in durable_request


def test_fixture_manifest_is_deterministic_under_catalog_and_listing_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _project(
        tmp_path / "first",
        (("one", "tasks/one"), ("two", "tasks/two")),
    )
    _fixture(first, "tasks/one")
    _fixture(first, "tasks/two")
    second = tmp_path / "second"
    shutil.copytree(first, second)
    (second / "catalog.toml").write_text(
        """\
schema_version = 1
[sections.systems-design]
readme = "systems.md"
tasks = [
  {id = "two", harbor_task = "tasks/two"},
  {id = "one", harbor_task = "tasks/one"},
]
[sections.github-workflow]
readme = "github.md"
tasks = []
""",
        encoding="utf-8",
    )
    original_listdir = context_module.os.listdir

    def reversed_listdir(path):
        return list(reversed(original_listdir(path)))

    first_prepared = prepare_submission(first, "systems-design", run_id="fixture-run")
    monkeypatch.setattr(context_module.os, "listdir", reversed_listdir)
    second_prepared = prepare_submission(second, "systems-design", run_id="fixture-run")

    assert first_prepared.request.context_manifest == (
        second_prepared.request.context_manifest
    )
    assert first_prepared.request.context_manifest_sha256 == (
        second_prepared.request.context_manifest_sha256
    )


@pytest.mark.parametrize("mutation", ["bytes", "path", "mode", "add", "remove"])
def test_every_fixture_mutation_changes_plan_context_and_request_identity(
    tmp_path: Path, mutation: str
) -> None:
    root = _project(tmp_path / "project")
    fixture = _fixture(root)
    before = _identity(root)
    if mutation == "bytes":
        (fixture / "task.toml").write_bytes(b"changed\n")
    elif mutation == "path":
        (fixture / "task.toml").rename(fixture / "renamed.toml")
    elif mutation == "mode":
        (fixture / "task.toml").chmod(0o755)
    elif mutation == "add":
        (fixture / "added.txt").write_bytes(b"added")
    else:
        (fixture / "task.toml").unlink()

    after = _identity(root)
    assert all(old != new for old, new in zip(before, after, strict=True))


def test_multi_task_union_and_explicit_context_compose_when_disjoint(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path / "project",
        (("one", "tasks/one"), ("two", "tasks/two")),
        context="""
[context]
files = [{source = "input.txt", destination = "inputs/data.txt"}]
""",
    )
    _fixture(root, "tasks/one")
    _fixture(root, "tasks/two")
    (root / "input.txt").write_bytes(b"explicit")

    prepared = prepare_submission(root, "systems-design", run_id="fixture-run")
    destinations = {item.destination for item in prepared.plan.context}

    assert destinations == {
        "inputs/data.txt",
        "tasks/one/task.toml",
        "tasks/one/nested/run.sh",
        "tasks/two/task.toml",
        "tasks/two/nested/run.sh",
    }


@pytest.mark.parametrize(
    "destination",
    [
        "tasks/one/task.toml",
        "TASKS/ONE/TASK.TOML",
        "tasks/one",
        unicodedata.normalize("NFD", "tasks/one/café.txt"),
    ],
)
def test_explicit_fixture_collisions_and_ambiguity_are_rejected(
    tmp_path: Path, destination: str
) -> None:
    root = _project(
        tmp_path / "project",
        context=f'''
[context]
files = [{{source = "input.txt", destination = "{destination}"}}]
''',
    )
    _fixture(root)
    (root / "input.txt").write_bytes(b"explicit")
    with pytest.raises(ValueError, match=r"destination|ambiguous|collision|duplicate"):
        prepare_submission(root, "systems-design", run_id="fixture-run")


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "parent-symlink"])
def test_invalid_task_roots_are_rejected(kind: str, tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    if kind == "file":
        (root / "tasks").mkdir()
        (root / "tasks/one").write_bytes(b"file")
    elif kind == "symlink":
        target = root / "target"
        target.mkdir()
        (root / "tasks").mkdir()
        (root / "tasks/one").symlink_to(target, target_is_directory=True)
    elif kind == "parent-symlink":
        target = root / "target"
        (target / "one").mkdir(parents=True)
        (root / "tasks").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match=r"missing|directory|unsafe|safely"):
        prepare_submission(root, "systems-design", run_id="fixture-run")


@pytest.mark.parametrize(
    "tasks",
    [
        (("one", "tasks/one"), ("two", "tasks/one")),
        (("one", "tasks/one"), ("two", "TASKS/ONE")),
        (("one", "../outside"),),
    ],
)
def test_duplicate_ambiguous_or_escaping_selected_task_roots_are_rejected(
    tmp_path: Path, tasks: tuple[tuple[str, str], ...]
) -> None:
    root = _project(tmp_path / "project", tasks)
    _fixture(root)
    with pytest.raises(ValueError, match=r"unique|ambiguous|destination"):
        prepare_submission(root, "systems-design", run_id="fixture-run")


def test_fixture_rejection_constructs_no_cloud_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path / "project")
    target = root / "target"
    target.mkdir()
    (root / "tasks").mkdir()
    (root / "tasks/one").symlink_to(target, target_is_directory=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda _config: pytest.fail("fixture rejection constructed S3"),
    )

    class ForbiddenModal:
        def __init__(self, *_args, **_kwargs) -> None:
            pytest.fail("fixture rejection constructed Modal")

    monkeypatch.setattr("tetrabench.cli.ModalControllerClient", ForbiddenModal)
    result = runner.invoke(app, ["submit", "systems-design"])
    assert result.exit_code == 2
    assert "unsafe" in result.stderr


def test_fixture_rejects_symlink_and_special_entries(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    fixture = _fixture(root)
    (fixture / "link").symlink_to(fixture / "task.toml")
    with pytest.raises(ValueError, match="symlink"):
        prepare_submission(root, "systems-design", run_id="fixture-run")
    (fixture / "link").unlink()
    os.mkfifo(fixture / "fifo")
    with pytest.raises(ValueError, match="regular file or directory"):
        prepare_submission(root, "systems-design", run_id="fixture-run")


@pytest.mark.parametrize(
    "mutation", ["replace", "add", "remove", "rename", "directory", "mode"]
)
def test_fixture_mutation_during_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root = _project(tmp_path / "project")
    fixture = _fixture(root)
    target = fixture / "task.toml"

    def mutate_once(phase: str) -> None:
        assert phase == "before-verification"
        if mutation == "replace":
            replacement = fixture / "replacement"
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
        elif mutation == "add":
            (fixture / "added").write_bytes(b"added")
        elif mutation == "remove":
            (fixture / "nested/run.sh").unlink()
        elif mutation == "rename":
            (fixture / "nested/run.sh").rename(fixture / "nested/moved.sh")
        elif mutation == "directory":
            old = root / "tasks/old-one"
            fixture.rename(old)
            shutil.copytree(old, fixture)
        else:
            target.chmod(0o755)

    monkeypatch.setattr(context_module, "_fixture_sealing_checkpoint", mutate_once)
    with pytest.raises(
        ValueError, match=r"changed while sealing|changed while reading"
    ):
        prepare_submission(root, "systems-design", run_id="fixture-run")


@pytest.mark.parametrize(
    "config",
    [
        ContextConfig(max_files=1),
        ContextConfig(max_file_bytes=4),
        ContextConfig(max_total_bytes=5),
    ],
)
def test_fixture_limits_fail_before_a_provider_can_be_constructed(
    tmp_path: Path, config: ContextConfig
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _fixture(root)
    with pytest.raises(ValueError, match=r"max_files|max_file_bytes|max_total_bytes"):
        seal_context(root, config, fixture_roots=("tasks/one",))


def test_discovery_bounds_are_explicit_and_incremental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fixture = root / "tasks/one"
    for name in ("one", "two", "three"):
        (fixture / name).mkdir(parents=True)
    monkeypatch.setattr(
        context_module.os,
        "listdir",
        lambda *_args, **_kwargs: pytest.fail("fixture discovery used listdir"),
    )

    with pytest.raises(FixtureLimitError, match="max_entries"):
        seal_context(
            root,
            ContextConfig(max_files=2, max_entries=2),
            fixture_roots=("tasks/one",),
        )
    with pytest.raises(FixtureLimitError, match="max_directories"):
        seal_context(
            root,
            ContextConfig(max_directories=1),
            fixture_roots=("tasks/one",),
        )
    with pytest.raises(FixtureLimitError, match="max_depth"):
        seal_context(
            root,
            ContextConfig(max_depth=0),
            fixture_roots=("tasks/one",),
        )
    with pytest.raises(ValidationError, match="max_entries cannot be lower"):
        ContextConfig(max_files=2, max_entries=1)


def test_fixture_rejects_hardlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fixture = _fixture(root)
    os.link(fixture / "task.toml", root / "external-alias")

    with pytest.raises(FixtureTrustError, match="hard links"):
        seal_context(root, ContextConfig(), fixture_roots=("tasks/one",))


def test_fixture_rejects_fake_cross_device_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fixture = _fixture(root)
    target_ino = (fixture / "task.toml").stat().st_ino
    original = context_module.os.fstat

    def fake_cross_device(descriptor: int):
        metadata = original(descriptor)
        if metadata.st_ino == target_ino:
            values = list(metadata)
            values[2] = metadata.st_dev + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(context_module.os, "fstat", fake_cross_device)
    with pytest.raises(FixtureTrustError, match="device"):
        seal_context(root, ContextConfig(), fixture_roots=("tasks/one",))


@pytest.mark.parametrize("failure", ["mismatch", "unavailable"])
def test_fixture_rejects_mount_id_mismatch_or_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    descriptors_before = len(tuple(Path("/proc/self/fd").iterdir()))
    root = tmp_path / "root"
    root.mkdir()
    fixture = _fixture(root)
    target_ino = (fixture / "task.toml").stat().st_ino
    authority = open_project_root(root)
    original = context_module._read_mount_id

    def mount_id(descriptor: int) -> int:
        if os.fstat(descriptor).st_ino == target_ino:
            if failure == "unavailable":
                raise FixtureTrustError("mount ID evidence is unavailable")
            return authority.mount_id + 1
        return original(descriptor)

    monkeypatch.setattr(context_module, "_read_mount_id", mount_id)
    try:
        with pytest.raises(FixtureTrustError, match=r"mount|unavailable"):
            seal_context(
                root,
                ContextConfig(),
                fixture_roots=("tasks/one",),
                authority=authority,
            )
    finally:
        authority.close()
    assert len(tuple(Path("/proc/self/fd").iterdir())) == descriptors_before


def test_second_enumeration_digest_catches_coarse_stat_content_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fixture = _fixture(root)
    target = fixture / "task.toml"
    timestamps = target.stat()

    def mutate(_phase: str) -> None:
        target.write_bytes(b"evil\n")
        os.utime(target, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))

    monkeypatch.setattr(context_module, "_fixture_sealing_checkpoint", mutate)
    with pytest.raises(FixtureMutationError, match="content changed"):
        seal_context(root, ContextConfig(), fixture_roots=("tasks/one",))


def test_project_root_replacement_cannot_switch_fixture_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _fixture(root)
    moved = tmp_path / "moved-root"

    def replace(_phase: str) -> None:
        root.rename(moved)
        shutil.copytree(moved, root)

    monkeypatch.setattr(context_module, "_fixture_sealing_checkpoint", replace)
    with pytest.raises(FixtureMutationError, match="project root was replaced"):
        seal_context(root, ContextConfig(), fixture_roots=("tasks/one",))


def test_root_replacement_after_catalog_parse_cannot_switch_fixture_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path / "project")
    _fixture(root)
    moved = tmp_path / "catalog-root"
    original = submission_module.load_catalog

    def replace_after_parse(*args, **kwargs):
        catalog = original(*args, **kwargs)
        root.rename(moved)
        shutil.copytree(moved, root)
        (root / "tasks/one/task.toml").write_bytes(b"switched\n")
        return catalog

    monkeypatch.setattr(submission_module, "load_catalog", replace_after_parse)
    with pytest.raises(FixtureMutationError, match="project root was replaced"):
        prepare_submission(root, "systems-design", run_id="fixture-run")


def test_sealed_materialized_fixture_matches_stable_local_tree(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    fixture = _fixture(root)
    prepared = prepare_submission(root, "systems-design", run_id="fixture-run")
    materialized = tmp_path / "materialized"
    for destination, (data, mode) in _manifest_tree(prepared).items():
        output = materialized / destination
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        output.chmod(mode)

    local = {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            0o755 if path.stat().st_mode & 0o111 else 0o644,
        )
        for path in fixture.rglob("*")
        if path.is_file()
    }
    detached = {
        path.relative_to(materialized).as_posix(): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in materialized.rglob("*")
        if path.is_file()
    }
    assert detached == local


def test_detached_harbor_plan_resolves_only_materialized_task_path(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "project")
    _fixture(root)
    prepared = prepare_submission(root, "systems-design", run_id="fixture-run")
    paths = attempt_paths(tmp_path / "controller", "fixture-run", "attempt-one")
    paths.context.mkdir(parents=True)
    paths.jobs.mkdir()
    for destination, (data, mode) in _manifest_tree(prepared).items():
        output = paths.context / destination
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        output.chmod(mode)
    shutil.rmtree(root / "tasks")

    class Api:
        task_path: Path | None = None

        def task_config(self, *, path: Path):
            self.task_path = path
            return path

        @staticmethod
        def agent_config(**kwargs):
            return kwargs

        @staticmethod
        def import_path_environment(**kwargs):
            return kwargs

        @staticmethod
        def docker_environment():
            return {"type": "docker"}

        @staticmethod
        def validate_task(*, path: Path) -> None:
            del path

        @staticmethod
        def execute(config):
            return config

        @staticmethod
        def validate_native_artifacts(job_directory, result, config):
            return job_directory, result, config

        @staticmethod
        def job_config(**kwargs):
            return kwargs

    api = Api()
    labels = {
        RUN_LABEL: prepared.request.run_id,
        ATTEMPT_LABEL: paths.root.name,
        PLAN_LABEL: prepared.request.plan_sha256,
    }
    compile_harbor_job(
        prepared.request,
        paths,
        environment_import_path="fixture:Environment",
        labels=labels,
        api=api,
    )
    assert api.task_path == paths.context / "tasks/one"
    assert api.task_path.is_dir()
