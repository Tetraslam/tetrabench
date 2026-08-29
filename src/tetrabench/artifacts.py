"""Descriptor-rooted materialization of remote terminal inventories."""

from __future__ import annotations

import os
import stat
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from tetrabench.artifact_policy import ArtifactLimits
from tetrabench.lifecycle import BindingStore, terminal_admission_conflicts
from tetrabench.models import FrozenRecord, Sha256
from tetrabench.records import (
    ArtifactInventoryEntry,
    ConflictRunState,
    ContentObject,
    RunId,
    RunReadState,
    TerminalRunState,
    validate_run_id,
)
from tetrabench.remote import RemoteArtifact
from tetrabench.s3 import AdmissionRead, S3IntegrityError
from tetrabench.storage import validate_logical_path

_MKDIR_MODE_LOCK = threading.Lock()


class ArtifactPullStore(BindingStore, Protocol):
    def read_run_state(self, run_id: str) -> RunReadState: ...
    def read_admission(self, run_id: str) -> AdmissionRead | None: ...
    def stream_content_to_fd(self, descriptor: ContentObject, fd: int) -> None: ...


class ArtifactDestinationExistsError(RuntimeError):
    """The requested artifact destination already exists."""


class ArtifactPullRefusedError(RuntimeError):
    """The run does not have one successful, bound terminal inventory."""


class ArtifactPullResult(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    output_directory: str
    terminal_sha256: Sha256
    artifacts: tuple[RemoteArtifact, ...]


@dataclass
class _Directory:
    fd: int
    parent_fd: int
    name: str
    identity: tuple[int, int]


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _verify_fd(
    fd: int,
    *,
    identity: tuple[int, int] | None,
    mode: int,
    directory: bool,
) -> os.stat_result:
    value = os.fstat(fd)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_type(value.st_mode)
        or (identity is not None and _identity(value) != identity)
        or stat.S_IMODE(value.st_mode) != mode
    ):
        kind = "directory" if directory else "file"
        raise OSError(f"artifact {kind} mode, type, or identity changed")
    return value


def _directory_flags() -> int:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise OSError("artifact pull requires POSIX no-follow directory operations")
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _mkdir_private(name: str, *, dir_fd: int) -> None:
    # mkdirat applies the process umask. Hold the change across only this syscall
    # so the reservation itself, not a later path chmod, starts at exact 0700.
    with _MKDIR_MODE_LOCK:
        previous = os.umask(0)
        try:
            os.mkdir(name, mode=0o700, dir_fd=dir_fd)
        finally:
            os.umask(previous)


def _open_parent(
    path: Path,
) -> tuple[int, str, tuple[int, ...], tuple[_Directory, ...]]:
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    if len(parts) < 2 or parts[0] != os.sep:
        raise ValueError("artifact destination must resolve beneath a POSIX root")
    fd = os.open(os.sep, _directory_flags())
    fds = [fd]
    anchors: list[_Directory] = []
    try:
        for component in parts[1:-1]:
            next_fd = os.open(component, _directory_flags(), dir_fd=fd)
            fds.append(next_fd)
            value = os.fstat(next_fd)
            anchors.append(
                _Directory(
                    fd=next_fd,
                    parent_fd=fd,
                    name=component,
                    identity=_identity(value),
                )
            )
            fd = next_fd
        return fd, parts[-1], tuple(fds), tuple(anchors)
    except BaseException:
        for opened in reversed(fds):
            with suppress(OSError):
                os.close(opened)
        raise


def _verify_anchors(anchors: tuple[_Directory, ...]) -> None:
    for anchor in anchors:
        held = os.fstat(anchor.fd)
        visible = os.stat(
            anchor.name,
            dir_fd=anchor.parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or _identity(held) != anchor.identity
            or _identity(visible) != anchor.identity
        ):
            raise OSError("artifact destination parent identity changed")


def _validate_inventory(
    artifacts: tuple[ArtifactInventoryEntry, ...],
    limits: ArtifactLimits | None = None,
) -> tuple[ArtifactInventoryEntry, ...]:
    limits = limits or ArtifactLimits()
    ordered = tuple(sorted(artifacts, key=lambda item: item.logical_path))
    if len(ordered) > limits.max_files:
        raise ArtifactPullRefusedError("terminal inventory exceeds max_files")
    total_bytes = 0
    paths: list[tuple[str, ...]] = []
    for artifact in ordered:
        path = validate_logical_path(artifact.logical_path)
        size = artifact.content.size
        if size > limits.max_file_bytes:
            raise ArtifactPullRefusedError(
                "terminal inventory file exceeds max_file_bytes"
            )
        if size > limits.max_total_bytes - total_bytes:
            raise ArtifactPullRefusedError("terminal inventory exceeds max_total_bytes")
        total_bytes += size
        parts = tuple(path.split("/"))
        if parts in paths:
            raise ArtifactPullRefusedError(
                "terminal inventory contains a duplicate path"
            )
        paths.append(parts)
    path_set = set(paths)
    for parts in paths:
        if any(parts[:index] in path_set for index in range(1, len(parts))):
            raise ArtifactPullRefusedError(
                "terminal inventory contains a file/directory prefix conflict"
            )
    return ordered


class _Materializer:
    def __init__(self, output: Path, artifacts: tuple[ArtifactInventoryEntry, ...]):
        self.output = Path(os.path.abspath(output.expanduser()))
        self.artifacts = artifacts
        self.directories: dict[tuple[str, ...], _Directory] = {}
        self._directory_fds: list[int] = []
        self.files: dict[tuple[str, ...], tuple[int, int]] = {}
        self.expected: dict[tuple[str, ...], set[str]] = {(): set()}
        for artifact in artifacts:
            parts = tuple(artifact.logical_path.split("/"))
            for index, component in enumerate(parts):
                parent = parts[:index]
                self.expected.setdefault(parent, set()).add(component)
                if index + 1 < len(parts):
                    self.expected.setdefault(parts[: index + 1], set())

    def materialize(self, store: ArtifactPullStore) -> None:
        parent_fd, name, parent_fds, anchors = _open_parent(self.output)
        try:
            _verify_anchors(anchors)
            try:
                _mkdir_private(name, dir_fd=parent_fd)
            except FileExistsError as error:
                raise ArtifactDestinationExistsError(
                    f"artifact destination already exists: {self.output}"
                ) from error
            os.fsync(parent_fd)
            root_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
            self._directory_fds.append(root_fd)
            opened = os.fstat(root_fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise OSError("reserved artifact destination is not a directory")
            root_identity = _identity(opened)
            os.fchmod(root_fd, 0o700)
            root_stat = _verify_fd(
                root_fd,
                identity=root_identity,
                mode=0o700,
                directory=True,
            )
            os.fsync(root_fd)
            self.directories[()] = _Directory(
                fd=root_fd,
                parent_fd=parent_fd,
                name=name,
                identity=_identity(root_stat),
            )
            for artifact in self.artifacts:
                _verify_anchors(anchors)
                self._write(store, artifact)
            _verify_anchors(anchors)
            self._verify_complete_tree()
            self._finalize_tree()
        except BaseException:
            self._sync_partial(parent_fd)
            raise
        finally:
            for fd in reversed(self._directory_fds):
                with suppress(OSError):
                    os.close(fd)
            for opened in reversed(parent_fds):
                with suppress(OSError):
                    os.close(opened)
            # A successful mkdir is permanent evidence, including when opening,
            # downloading, or materialization later fails.

    def _verify_directory(self, path: tuple[str, ...]) -> _Directory:
        directory = self.directories[path]
        held = os.fstat(directory.fd)
        visible = os.stat(
            directory.name,
            dir_fd=directory.parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or _identity(held) != directory.identity
            or _identity(visible) != directory.identity
        ):
            raise OSError("artifact destination directory identity changed")
        return directory

    def _ensure_directory(self, path: tuple[str, ...]) -> _Directory:
        if path in self.directories:
            return self._verify_directory(path)
        parent_path = path[:-1]
        parent = self._ensure_directory(parent_path)
        name = path[-1]
        try:
            _mkdir_private(name, dir_fd=parent.fd)
        except FileExistsError as error:
            raise OSError("artifact directory was injected concurrently") from error
        os.fsync(parent.fd)
        fd = os.open(name, _directory_flags(), dir_fd=parent.fd)
        self._directory_fds.append(fd)
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("artifact directory is not a directory")
        identity = _identity(opened)
        os.fchmod(fd, 0o700)
        value = _verify_fd(
            fd,
            identity=identity,
            mode=0o700,
            directory=True,
        )
        os.fsync(fd)
        directory = _Directory(
            fd=fd,
            parent_fd=parent.fd,
            name=name,
            identity=_identity(value),
        )
        self.directories[path] = directory
        return self._verify_directory(path)

    def _write(
        self, store: ArtifactPullStore, artifact: ArtifactInventoryEntry
    ) -> None:
        parts = tuple(artifact.logical_path.split("/"))
        parent = self._ensure_directory(parts[:-1])
        self._verify_directory(())
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(parts[-1], flags, 0o600, dir_fd=parent.fd)
        except FileExistsError as error:
            raise OSError("artifact file was injected concurrently") from error
        failed = True
        file_identity: tuple[int, int] | None = None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("created artifact is not a regular file")
            file_identity = _identity(opened)
            os.fsync(parent.fd)
            os.fchmod(fd, 0o600)
            _verify_fd(
                fd,
                identity=file_identity,
                mode=0o600,
                directory=False,
            )
            store.stream_content_to_fd(artifact.content, fd)
            held = os.fstat(fd)
            visible = os.stat(parts[-1], dir_fd=parent.fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(held.st_mode)
                or not stat.S_ISREG(visible.st_mode)
                or _identity(held) != _identity(visible)
                or held.st_size != artifact.content.size
            ):
                raise OSError("artifact file identity changed during materialization")
            os.fsync(fd)
            self.files[parts] = _identity(held)
            failed = False
        finally:
            if failed:
                with suppress(OSError):
                    os.fchmod(fd, 0o600)
                    _verify_fd(
                        fd,
                        identity=file_identity,
                        mode=0o600,
                        directory=False,
                    )
                with suppress(OSError):
                    os.fsync(fd)
                with suppress(OSError):
                    os.fsync(parent.fd)
            try:
                os.close(fd)
            except OSError:
                if not failed:
                    raise

    def _sync_partial(self, parent_fd: int) -> None:
        """Best-effort durability for retained evidence during exception unwind."""
        for path in sorted(self.files):
            with suppress(OSError):
                self._finalize_file(path)
        known_directories = {
            directory.fd: directory for directory in self.directories.values()
        }
        for fd in reversed(self._directory_fds):
            with suppress(OSError):
                directory = known_directories.get(fd)
                opened = os.fstat(fd)
                if not stat.S_ISDIR(opened.st_mode):
                    raise OSError("artifact directory type changed")
                identity = (
                    directory.identity if directory is not None else _identity(opened)
                )
                os.fchmod(fd, 0o700)
                _verify_fd(
                    fd,
                    identity=identity,
                    mode=0o700,
                    directory=True,
                )
                os.fsync(fd)
        with suppress(OSError):
            os.fsync(parent_fd)

    def _finalize_file(self, path: tuple[str, ...]) -> None:
        parent = self._verify_directory(path[:-1])
        identity = self.files[path]
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(path[-1], flags, dir_fd=parent.fd)
        failed = True
        try:
            held = os.fstat(fd)
            visible = os.stat(path[-1], dir_fd=parent.fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(held.st_mode)
                or not stat.S_ISREG(visible.st_mode)
                or _identity(held) != identity
                or _identity(visible) != identity
            ):
                raise OSError("artifact file identity changed before finalization")
            os.fchmod(fd, 0o600)
            _verify_fd(
                fd,
                identity=identity,
                mode=0o600,
                directory=False,
            )
            visible = os.stat(path[-1], dir_fd=parent.fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(visible.st_mode)
                or _identity(visible) != identity
                or stat.S_IMODE(visible.st_mode) != 0o600
            ):
                raise OSError("artifact file changed during finalization")
            os.fsync(fd)
            failed = False
        finally:
            try:
                os.close(fd)
            except OSError:
                if not failed:
                    raise

    def _finalize_directory(self, path: tuple[str, ...]) -> None:
        directory = self._verify_directory(path)
        os.fchmod(directory.fd, 0o700)
        _verify_fd(
            directory.fd,
            identity=directory.identity,
            mode=0o700,
            directory=True,
        )
        visible = os.stat(
            directory.name,
            dir_fd=directory.parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(visible.st_mode)
            or _identity(visible) != directory.identity
            or stat.S_IMODE(visible.st_mode) != 0o700
        ):
            raise OSError("artifact directory changed during finalization")
        os.fsync(directory.fd)

    def _finalize_tree(self) -> None:
        for path in sorted(self.files):
            self._finalize_file(path)
        for path in sorted(self.directories, key=len, reverse=True):
            self._finalize_directory(path)

    def _verify_complete_tree(self) -> None:
        for path, directory in self.directories.items():
            self._verify_directory(path)
            names = set(os.listdir(directory.fd))
            if names != self.expected[path]:
                raise OSError("artifact destination contains unexpected content")
            for name in names:
                child = (*path, name)
                value = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                if child in self.directories:
                    if not stat.S_ISDIR(value.st_mode):
                        raise OSError("artifact directory was replaced")
                elif child in self.files:
                    if (
                        not stat.S_ISREG(value.st_mode)
                        or _identity(value) != self.files[child]
                    ):
                        raise OSError("artifact file was replaced")
                else:
                    raise OSError("artifact destination contains injected content")


class ArtifactPullService:
    def __init__(
        self,
        store: ArtifactPullStore,
        *,
        limits: ArtifactLimits | None = None,
    ) -> None:
        self._store = store
        self._limits = limits or ArtifactLimits()

    def pull(self, run_id: str, output: Path) -> ArtifactPullResult:
        run_id = validate_run_id(run_id)
        durable = self._store.read_run_state(run_id)
        if isinstance(durable, ConflictRunState):
            raise ArtifactPullRefusedError("; ".join(durable.reasons))
        if not isinstance(durable, TerminalRunState):
            raise ArtifactPullRefusedError(
                "run has no authoritative terminal inventory"
            )
        try:
            admission = self._store.read_admission(run_id)
        except (OSError, S3IntegrityError, TypeError, ValueError) as error:
            raise ArtifactPullRefusedError(
                f"invalid admission record: {error}"
            ) from error
        conflicts = terminal_admission_conflicts(
            self._store,
            durable,
            admission.record if admission is not None else None,
        )
        if conflicts:
            raise ArtifactPullRefusedError("; ".join(conflicts))
        if durable.terminal.outcome != "succeeded":
            raise ArtifactPullRefusedError(
                "artifact pull requires a successful terminal outcome"
            )
        artifacts = _validate_inventory(durable.terminal.artifacts, self._limits)
        _Materializer(output, artifacts).materialize(self._store)
        return ArtifactPullResult(
            run_id=run_id,
            output_directory=str(Path(os.path.abspath(output.expanduser()))),
            terminal_sha256=durable.terminal_sha256,
            artifacts=tuple(
                RemoteArtifact(
                    logical_path=item.logical_path,
                    sha256=item.content.sha256,
                    size=item.content.size,
                    media_type=item.content.media_type,
                )
                for item in artifacts
            ),
        )
