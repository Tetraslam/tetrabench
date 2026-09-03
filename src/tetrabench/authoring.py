"""Atomic local project and Harbor task authoring."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from tetrabench.catalog import SectionName, load_catalog
from tetrabench.config import load_project_config
from tetrabench.context import SealedContext, seal_context
from tetrabench.harbor_api import Harbor022Api
from tetrabench.models import ContextConfig, TaskId
from tetrabench.storage import validate_logical_path

_SECTION_ADAPTER = TypeAdapter(SectionName)
_TASK_ID_ADAPTER = TypeAdapter(TaskId)
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1

PROJECT_CONFIG = b"""\
schema_version = 1
catalog_path = "benchmarks/catalog.toml"

[controller]
kind = "local"

[execution]
kind = "docker"

[harbor]
agent_name = "oracle"
attempts = 1
concurrency = 1
"""

CATALOG = b"""\
schema_version = 1

[sections.systems-design]
description = "Build and verify systems tasks."
readme = "systems-design/README.md"

[sections.github-workflow]
description = "Build and verify GitHub workflow tasks."
readme = "github-workflow/README.md"

[[sections.systems-design.tasks]]
id = "hello-tetrabench"
harbor_task = "benchmarks/tasks/systems-design/hello-tetrabench"
reward_policy = "binary"
"""

SYSTEMS_README = b"""\
# Systems design

Locally authored systems-design tasks live in `../tasks/systems-design/`.
"""

GITHUB_README = b"""\
# GitHub workflow

Locally authored GitHub workflow tasks live in `../tasks/github-workflow/`.
"""

INSTRUCTION = b"""\
# Hello tetrabench

Create `/workspace/answer.txt` containing exactly `tetrabench` followed by a newline.
"""

TASK_CONFIG = b"""\
schema_version = "1.4"
artifacts = [
  { source = "/workspace", destination = "workspace", service = "main" },
]

[environment]
build_timeout_sec = 120
network_mode = "no-network"

[agent]
timeout_sec = 30

[verifier]
environment_mode = "separate"
timeout_sec = 30

[verifier.environment]
network_mode = "no-network"
"""

_PINNED_IMAGE = (
    b"FROM python:3.12.11-slim-bookworm@sha256:"
    b"519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7\n"
)

MAIN_DOCKERFILE = _PINNED_IMAGE + b"\nWORKDIR /workspace\n"

VERIFIER_DOCKERFILE = (
    _PINNED_IMAGE
    + b"""
COPY . /tests
RUN chmod 0700 /tests && chmod 0500 /tests/test.sh
WORKDIR /tests
"""
)

VERIFIER = b"""\
#!/bin/sh
set -eu
mkdir -p /logs/verifier
if [ -f /workspace/answer.txt ] && \
  [ "$(wc -c < /workspace/answer.txt)" -eq 11 ] && \
  [ "$(cat /workspace/answer.txt)" = "tetrabench" ]; then
  printf '{"reward":1}\n' > /logs/verifier/reward.json
else
  printf '{"reward":0}\n' > /logs/verifier/reward.json
fi
"""

SOLUTION = b"""\
#!/bin/sh
set -eu
printf 'tetrabench\n' > /workspace/answer.txt
"""

_TASK_FILES: dict[str, tuple[bytes, int]] = {
    "environment/Dockerfile": (MAIN_DOCKERFILE, 0o644),
    "instruction.md": (INSTRUCTION, 0o644),
    "solution/solve.sh": (SOLUTION, 0o755),
    "task.toml": (TASK_CONFIG, 0o644),
    "tests/Dockerfile": (VERIFIER_DOCKERFILE, 0o644),
    "tests/test.sh": (VERIFIER, 0o755),
}


@dataclass(frozen=True, slots=True)
class FixtureValidation:
    fixture: str
    file_count: int
    total_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "file_count": self.file_count,
            "fixture": self.fixture,
            "schema_version": 1,
            "status": "ok",
            "total_bytes": self.total_bytes,
        }


def _authoring_creation_checkpoint(_logical_path: str) -> None:
    """Test seam for proving staged-tree rollback."""


def _validated_section(value: str) -> SectionName:
    return _SECTION_ADAPTER.validate_python(value, strict=True)


def _validated_task_id(value: str) -> str:
    return _TASK_ID_ADAPTER.validate_python(value, strict=True)


def _project_root(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    resolved = absolute.resolve(strict=True)
    if absolute != resolved or not resolved.is_dir():
        raise ValueError(f"project directory is a symlink or not a directory: {path}")
    return resolved


def _absent_destination(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    if absolute.name in {"", ".", ".."}:
        raise ValueError("destination must name a new directory")
    parent = absolute.parent
    resolved_parent = parent.resolve(strict=True)
    if parent != resolved_parent or not resolved_parent.is_dir():
        raise ValueError("destination parent is a symlink or not a directory")
    destination = resolved_parent / absolute.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"destination already exists: {destination}")
    return destination


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-replace directory creation requires renameat2")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(f"destination already exists: {destination}")
    raise OSError(error_number, os.strerror(error_number), destination)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_staged_tree(
    destination: Path,
    files: dict[str, tuple[bytes, int]],
    *,
    empty_directories: tuple[str, ...] = (),
) -> None:
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        stage.chmod(0o755)
        directories = sorted(
            {Path(name) for name in empty_directories}
            | {
                parent
                for name in files
                for parent in Path(name).parents
                if parent != Path(".")
            },
            key=lambda item: len(item.parts),
        )
        for relative in directories:
            directory = stage / relative
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
        for logical_path, (content, mode) in files.items():
            path = stage / logical_path
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(content)
                    stream.flush()
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _authoring_creation_checkpoint(logical_path)
        for relative in sorted(
            directories, key=lambda item: len(item.parts), reverse=True
        ):
            _fsync_directory(stage / relative)
        _fsync_directory(stage)
        _rename_noreplace(stage, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def _task_files() -> dict[str, tuple[bytes, int]]:
    return dict(_TASK_FILES)


def initialize_project(directory: Path) -> Path:
    destination = _absent_destination(directory)
    files = {
        "benchmarks/catalog.toml": (CATALOG, 0o644),
        "benchmarks/github-workflow/README.md": (GITHUB_README, 0o644),
        "benchmarks/systems-design/README.md": (SYSTEMS_README, 0o644),
        "tetrabench.toml": (PROJECT_CONFIG, 0o644),
    }
    starter = "benchmarks/tasks/systems-design/hello-tetrabench"
    files.update({f"{starter}/{name}": value for name, value in _task_files().items()})
    _create_staged_tree(
        destination,
        files,
        empty_directories=("benchmarks/tasks/github-workflow",),
    )
    return destination


def create_task(project: Path, section: str, task_id: str) -> tuple[Path, str]:
    root = _project_root(project)
    section_name = _validated_section(section)
    validated_id = _validated_task_id(task_id)
    logical_path = f"benchmarks/tasks/{section_name}/{validated_id}"
    validate_logical_path(logical_path)
    config = load_project_config(root)
    catalog = load_catalog(root, config.catalog_path)
    all_tasks = [
        *catalog.sections.systems_design.tasks,
        *catalog.sections.github_workflow.tasks,
    ]
    if any(task.id == validated_id for task in all_tasks):
        raise ValueError(f"catalog task ID already exists: {validated_id}")
    if any(task.harbor_task == logical_path for task in all_tasks):
        raise ValueError(f"catalog fixture is already referenced: {logical_path}")
    destination = _absent_destination(root / logical_path)
    _create_staged_tree(destination, _task_files())
    return destination, logical_path


def validate_fixture(root: Path, fixture: str) -> FixtureValidation:
    root = _project_root(root)
    logical_path = validate_logical_path(fixture)
    sealed = _validate_fixture_snapshot(root, logical_path)
    return FixtureValidation(
        fixture=logical_path,
        file_count=len(sealed.manifest.files),
        total_bytes=sum(item.content.size for item in sealed.manifest.files),
    )


def _materialize_sealed_context(sealed: SealedContext, destination: Path) -> None:
    destination.chmod(0o700)
    for manifest_file, sealed_file in zip(
        sealed.manifest.files, sealed.files, strict=True
    ):
        path = destination / manifest_file.destination
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = path.parent
        while current != destination:
            current.chmod(0o700)
            current = current.parent
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(sealed_file.content)
                stream.flush()
            os.fchmod(descriptor, manifest_file.mode)
        finally:
            os.close(descriptor)


def _validate_fixture_snapshot(root: Path, logical_path: str) -> SealedContext:
    sealed = seal_context(
        root,
        ContextConfig(),
        fixture_roots=(logical_path,),
    )
    with tempfile.TemporaryDirectory(prefix="tetrabench-validate-") as temporary:
        materialized_root = Path(temporary)
        _materialize_sealed_context(sealed, materialized_root)
        Harbor022Api().validate_task(path=materialized_root / logical_path)
    resealed = seal_context(
        root,
        ContextConfig(),
        fixture_roots=(logical_path,),
    )
    if resealed != sealed:
        raise RuntimeError("fixture changed while validating the sealed snapshot")
    return sealed


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _catalog_block(section: str, task_id: str, fixture: str) -> bytes:
    return (
        f"[[sections.{section}.tasks]]\n"
        f"id = {_toml_string(task_id)}\n"
        f"harbor_task = {_toml_string(fixture)}\n"
        'reward_policy = "binary"\n'
    ).encode()


def _catalog_path(root: Path, configured_path: str) -> Path:
    logical_path = validate_logical_path(configured_path)
    current = root
    for component in logical_path.split("/"):
        current = current / component
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(
                f"configured catalog path contains a symlink: {logical_path}"
            )
    if not stat.S_ISREG(current.stat().st_mode):
        raise ValueError(f"configured catalog is not a regular file: {logical_path}")
    return current


def _atomic_append_catalog(
    root: Path,
    catalog_path: Path,
    section: str,
    task_id: str,
    fixture: str,
    validated_snapshot: SealedContext,
) -> None:
    parent_descriptor = os.open(catalog_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    lock_descriptor: int | None = None
    temporary: Path | None = None
    try:
        lock_descriptor = os.open(
            f".{catalog_path.name}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        lock_stat = os.fstat(lock_descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise ValueError("catalog lock is not a single-link regular file")
        os.fchmod(lock_descriptor, 0o600)
        # All tetrabench catalog writers serialize on this sibling advisory lock.
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        source_descriptor = os.open(
            catalog_path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        try:
            source_stat = os.fstat(source_descriptor)
            with os.fdopen(source_descriptor, "rb", closefd=False) as stream:
                original = stream.read()
        finally:
            os.close(source_descriptor)
        catalog = load_catalog(root, catalog_path.as_posix(), catalog_data=original)
        all_tasks = [
            *catalog.sections.systems_design.tasks,
            *catalog.sections.github_workflow.tasks,
        ]
        if any(task.id == task_id for task in all_tasks):
            raise ValueError(f"catalog task ID already exists: {task_id}")
        if any(task.harbor_task == fixture for task in all_tasks):
            raise ValueError(f"catalog fixture is already referenced: {fixture}")

        separator = b"\n" if original.endswith(b"\n") else b"\n\n"
        updated = original + separator + _catalog_block(section, task_id, fixture)
        load_catalog(root, catalog_path.as_posix(), catalog_data=updated)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{catalog_path.name}.", dir=catalog_path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(updated)
                stream.flush()
            os.fchmod(descriptor, stat.S_IMODE(source_stat.st_mode))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        revalidated = _validate_fixture_snapshot(root, fixture)
        if revalidated != validated_snapshot:
            raise RuntimeError("fixture changed after its initial validation")

        check_descriptor = os.open(
            catalog_path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        try:
            check_stat = os.fstat(check_descriptor)
            with os.fdopen(check_descriptor, "rb", closefd=False) as stream:
                current = stream.read()
        finally:
            os.close(check_descriptor)
        if (check_stat.st_dev, check_stat.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ) or current != original:
            raise RuntimeError("catalog changed while preparing the atomic append")
        os.replace(temporary, catalog_path)
        temporary = None
        os.fsync(parent_descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(parent_descriptor)


def add_task(root: Path, section: str, task_id: str, fixture: str) -> FixtureValidation:
    root = _project_root(root)
    section_name = _validated_section(section)
    validated_id = _validated_task_id(task_id)
    logical_path = validate_logical_path(fixture)
    validated_snapshot = _validate_fixture_snapshot(root, logical_path)
    validation = FixtureValidation(
        fixture=logical_path,
        file_count=len(validated_snapshot.manifest.files),
        total_bytes=sum(
            item.content.size for item in validated_snapshot.manifest.files
        ),
    )
    config = load_project_config(root)
    catalog_path = _catalog_path(root, config.catalog_path)
    _atomic_append_catalog(
        root,
        catalog_path,
        section_name,
        validated_id,
        validation.fixture,
        validated_snapshot,
    )
    return validation
