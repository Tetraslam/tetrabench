"""Seal explicit files and selected task trees into immutable local bytes."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tetrabench.models import (
    ContextConfig,
    ResolvedContextFile,
    validate_context_destinations,
)
from tetrabench.records import ContentObject, ContextManifest, ContextManifestFile
from tetrabench.storage import (
    content_object_key,
    validate_logical_path,
    verify_content_object,
)

_FileKind = Literal["directory", "file"]
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ENTRY_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _fixture_sealing_checkpoint(_phase: str) -> None:
    pass


class FixtureSealingError(ValueError):
    """Base for stable fixture-sealing rejection types."""


class FixtureLimitError(FixtureSealingError):
    """A configured fixture discovery or content bound was exceeded."""


class FixtureTrustError(FixtureSealingError):
    """Required descriptor, mount, or tree trust evidence was unavailable."""


class FixtureMutationError(FixtureSealingError):
    """The selected fixture changed during sealing."""


@dataclass(frozen=True, slots=True)
class _Node:
    kind: _FileKind
    dev: int
    ino: int
    mode: int
    size: int
    mount_id: int
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class _FixtureSnapshot:
    root: str
    entries: dict[str, _Node]


@dataclass(frozen=True, slots=True)
class _StagedFixtureFile:
    destination: str
    content: bytes
    mode: int


@dataclass(slots=True)
class _DiscoveryBudget:
    config: ContextConfig
    files: int
    bytes: int
    entries: int = 0
    directories: int = 0

    def take_entry(self) -> None:
        if self.entries >= self.config.max_entries:
            raise FixtureLimitError("fixture contains more than max_entries")
        self.entries += 1

    def take_directory(self) -> None:
        if self.directories >= self.config.max_directories:
            raise FixtureLimitError("fixture contains more than max_directories")
        self.directories += 1

    def take_file(self) -> None:
        if self.files >= self.config.max_files:
            raise FixtureLimitError("context contains more than max_files")
        self.files += 1

    def take_bytes(self, size: int) -> None:
        if size > self.config.max_total_bytes - self.bytes:
            raise FixtureLimitError("context exceeds max_total_bytes")
        self.bytes += size


@dataclass(slots=True)
class ProjectRootAuthority:
    """One retained no-follow project-root descriptor and its mount authority."""

    path: Path
    descriptor: int
    dev: int
    ino: int
    mode: int
    mount_id: int
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            os.close(self.descriptor)
            self._closed = True

    def open_directory(self, logical_path: str) -> int:
        validate_logical_path(logical_path)
        current = self.descriptor
        owned = False
        try:
            for component in logical_path.split("/"):
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
                try:
                    _require_same_tree(child, self)
                except BaseException:
                    os.close(child)
                    raise
                if owned:
                    os.close(current)
                current = child
                owned = True
            return current
        except BaseException:
            if owned:
                os.close(current)
            raise

    def open_regular(self, logical_path: str) -> int:
        validate_logical_path(logical_path)
        components = logical_path.split("/")
        current = self.descriptor
        owned = False
        try:
            for component in components[:-1]:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
                try:
                    _require_same_tree(child, self)
                except BaseException:
                    os.close(child)
                    raise
                if owned:
                    os.close(current)
                current = child
                owned = True
            descriptor = os.open(components[-1], _ENTRY_FLAGS, dir_fd=current)
            try:
                _require_same_tree(descriptor, self)
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor
        finally:
            if owned:
                os.close(current)

    def verify_path(self) -> None:
        descriptor = _open_absolute_directory(self.path)
        try:
            metadata = os.fstat(descriptor)
            observed = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                _read_mount_id(descriptor),
            )
            expected = (self.dev, self.ino, self.mode, self.mount_id)
            if observed != expected:
                raise FixtureMutationError(
                    "trusted project root was replaced while sealing fixtures"
                )
        finally:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SealedContextFile:
    """One retained immutable content object, never a source-path reference."""

    descriptor: ContentObject
    content: bytes

    def __post_init__(self) -> None:
        verify_content_object(
            self.content,
            sha256=self.descriptor.sha256,
            size=self.descriptor.size,
        )


@dataclass(frozen=True, slots=True)
class SealedContext:
    manifest: ContextManifest
    files: tuple[SealedContextFile, ...]

    def __post_init__(self) -> None:
        if tuple(item.content for item in self.manifest.files) != tuple(
            item.descriptor for item in self.files
        ):
            raise ValueError("sealed files do not match context manifest")


def _require_safe_open_support() -> None:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if (
        os.name != "posix"
        or any(not hasattr(os, name) for name in required_flags)
        or os.open not in getattr(os, "supports_dir_fd", ())
        or not Path("/proc/self/fdinfo").is_dir()
    ):
        raise RuntimeError(
            "context sealing requires POSIX dir_fd and Linux /proc fd mount "
            "evidence, "
            "O_DIRECTORY, O_NOFOLLOW, and O_NONBLOCK support"
        )


def _read_mount_id(descriptor: int) -> int:
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as stream:
            values = [
                line.split(":", 1)[1].strip()
                for line in stream
                if line.startswith("mnt_id:")
            ]
    except (OSError, UnicodeError) as error:
        raise FixtureTrustError("mount ID evidence is unavailable") from error
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise FixtureTrustError("mount ID evidence is missing or malformed")
    mount_id = int(values[0])
    if mount_id <= 0:
        raise FixtureTrustError("mount ID evidence is missing or malformed")
    return mount_id


def _open_absolute_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    try:
        current = os.open("/", _DIRECTORY_FLAGS)
    except OSError as error:
        raise FixtureTrustError("cannot establish trusted project root") from error
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except OSError as error:
        os.close(current)
        raise FixtureTrustError(
            f"cannot traverse trusted project root safely: {path}"
        ) from error
    except BaseException:
        os.close(current)
        raise


def open_project_root(path: Path) -> ProjectRootAuthority:
    """Anchor a Linux project root before reading project authority beneath it."""
    _require_safe_open_support()
    absolute = Path(os.path.abspath(path))
    descriptor = _open_absolute_directory(absolute)
    try:
        metadata = os.fstat(descriptor)
        return ProjectRootAuthority(
            path=absolute,
            descriptor=descriptor,
            dev=metadata.st_dev,
            ino=metadata.st_ino,
            mode=metadata.st_mode,
            mount_id=_read_mount_id(descriptor),
        )
    except BaseException:
        os.close(descriptor)
        raise


def _require_same_tree(
    descriptor: int, authority: ProjectRootAuthority
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if metadata.st_dev != authority.dev:
        raise FixtureTrustError("fixture entry crosses the trusted root device")
    if _read_mount_id(descriptor) != authority.mount_id:
        raise FixtureTrustError("fixture entry crosses the trusted root mount")
    return metadata


def _stable_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_ctime_ns,
        value.st_mtime_ns,
    )


def _read_descriptor(
    descriptor: int,
    display_path: str,
    max_file_bytes: int,
    remaining_total_bytes: int,
    authority: ProjectRootAuthority,
    *,
    positioned: bool = False,
) -> tuple[bytes, os.stat_result]:
    before = _require_same_tree(descriptor, authority)
    if not stat.S_ISREG(before.st_mode):
        raise FixtureTrustError(f"context source is not a regular file: {display_path}")
    if getattr(before, "st_nlink", 1) != 1:
        raise FixtureTrustError(
            f"context source has external hard links: {display_path}"
        )
    if before.st_size > max_file_bytes:
        raise FixtureLimitError(
            f"context source exceeds max_file_bytes: {display_path}"
        )
    if before.st_size > remaining_total_bytes:
        raise FixtureLimitError("context exceeds max_total_bytes")
    chunks: list[bytes] = []
    remaining = before.st_size
    offset = 0
    while remaining:
        count = min(remaining, 1024 * 1024)
        chunk = (
            os.pread(descriptor, count, offset)
            if positioned
            else os.read(descriptor, count)
        )
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        offset += len(chunk)
    data = b"".join(chunks)
    after = _require_same_tree(descriptor, authority)
    if (
        _stable_identity(before) != _stable_identity(after)
        or len(data) != before.st_size
    ):
        raise FixtureMutationError(
            f"context source changed while reading: {display_path}"
        )
    return data, before


def read_project_file(
    authority: ProjectRootAuthority,
    logical_path: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read one root-relative authority file through a retained no-follow fd."""
    try:
        descriptor = authority.open_regular(logical_path)
    except OSError as error:
        raise FixtureTrustError(
            f"cannot open trusted project file safely: {logical_path}"
        ) from error
    try:
        data, _metadata = _read_descriptor(
            descriptor,
            logical_path,
            max_bytes,
            max_bytes,
            authority,
            positioned=True,
        )
        return data
    finally:
        os.close(descriptor)


def _open_absolute_regular(path: Path, authority: ProjectRootAuthority) -> int:
    absolute = Path(os.path.abspath(path))
    components = absolute.parts[1:]
    if not components:
        raise FixtureTrustError(f"context source is not a regular file: {path}")
    current = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in components[:-1]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        descriptor = os.open(components[-1], _ENTRY_FLAGS, dir_fd=current)
        try:
            _require_same_tree(descriptor, authority)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(current)


def _read_regular_file(
    authority: ProjectRootAuthority,
    source: str,
    max_file_bytes: int,
    remaining_total_bytes: int,
) -> tuple[bytes, int]:
    configured = Path(source)
    try:
        descriptor = (
            _open_absolute_regular(configured, authority)
            if configured.is_absolute()
            else authority.open_regular(configured.as_posix())
        )
    except OSError as error:
        message = (
            f"cannot traverse context source safely: {source}"
            if "/" in source
            else f"cannot open context file safely: {source}"
        )
        raise FixtureTrustError(message) from error
    try:
        data, metadata = _read_descriptor(
            descriptor,
            source,
            max_file_bytes,
            remaining_total_bytes,
            authority,
        )
        return data, metadata.st_mode
    finally:
        os.close(descriptor)


def _node(
    descriptor: int,
    authority: ProjectRootAuthority,
    *,
    digest: str | None = None,
) -> _Node:
    metadata = _require_same_tree(descriptor, authority)
    if stat.S_ISDIR(metadata.st_mode):
        kind: _FileKind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
        if metadata.st_nlink != 1:
            raise FixtureTrustError("fixture regular file has external hard links")
    else:
        raise FixtureTrustError("fixture entry is not a regular file or directory")
    return _Node(
        kind=kind,
        dev=metadata.st_dev,
        ino=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        mount_id=_read_mount_id(descriptor),
        digest=digest,
    )


def _discover_fixture_directory(
    directory_fd: int,
    *,
    authority: ProjectRootAuthority,
    task_root: str,
    relative: tuple[str, ...],
    depth: int,
    budget: _DiscoveryBudget,
    files: list[_StagedFixtureFile],
    entries: dict[str, _Node],
) -> None:
    root_key = "/".join(relative)
    entries[root_key] = _node(directory_fd, authority)
    try:
        iterator = os.scandir(directory_fd)
    except OSError as error:
        raise FixtureTrustError(
            f"cannot enumerate Harbor task root safely: {task_root}"
        ) from error
    with iterator:
        for entry in iterator:
            budget.take_entry()
            destination = "/".join((task_root, *relative, entry.name))
            try:
                validate_logical_path(destination)
            except ValueError as error:
                raise FixtureTrustError(
                    f"Harbor task entry has an unsafe destination: {destination}"
                ) from error
            try:
                child_fd = os.open(
                    entry.name,
                    _ENTRY_FLAGS,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise FixtureTrustError(
                    "Harbor task entry is a symlink or cannot be opened safely: "
                    f"{destination}"
                ) from error
            try:
                metadata = _require_same_tree(child_fd, authority)
                child_relative = (*relative, entry.name)
                snapshot_path = "/".join(child_relative)
                if stat.S_ISDIR(metadata.st_mode):
                    next_depth = depth + 1
                    if next_depth > budget.config.max_depth:
                        raise FixtureLimitError("fixture exceeds max_depth")
                    budget.take_directory()
                    _discover_fixture_directory(
                        child_fd,
                        authority=authority,
                        task_root=task_root,
                        relative=child_relative,
                        depth=next_depth,
                        budget=budget,
                        files=files,
                        entries=entries,
                    )
                elif stat.S_ISREG(metadata.st_mode):
                    budget.take_file()
                    data, stable = _read_descriptor(
                        child_fd,
                        destination,
                        budget.config.max_file_bytes,
                        budget.config.max_total_bytes - budget.bytes,
                        authority,
                    )
                    budget.take_bytes(len(data))
                    digest = hashlib.sha256(data).hexdigest()
                    entries[snapshot_path] = _node(
                        child_fd,
                        authority,
                        digest=digest,
                    )
                    files.append(_StagedFixtureFile(destination, data, stable.st_mode))
                else:
                    raise FixtureTrustError(
                        "Harbor task entry is not a regular file or directory: "
                        f"{destination}"
                    )
            finally:
                os.close(child_fd)


def _hash_open_file(
    descriptor: int,
    expected: _Node,
    authority: ProjectRootAuthority,
) -> str:
    before = _node(descriptor, authority)
    if before != _Node(
        kind=expected.kind,
        dev=expected.dev,
        ino=expected.ino,
        mode=expected.mode,
        size=expected.size,
        mount_id=expected.mount_id,
    ):
        raise FixtureMutationError("Harbor task fixture changed while sealing")
    digest = hashlib.sha256()
    remaining = expected.size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    if remaining or os.read(descriptor, 1):
        raise FixtureMutationError("Harbor task fixture changed while sealing")
    after = _node(descriptor, authority)
    if before != after:
        raise FixtureMutationError("Harbor task fixture changed while sealing")
    return digest.hexdigest()


def _verify_fixture_directory(
    directory_fd: int,
    *,
    authority: ProjectRootAuthority,
    task_root: str,
    relative: tuple[str, ...],
    depth: int,
    budget: _DiscoveryBudget,
    expected: dict[str, _Node],
    seen: set[str],
) -> None:
    root_key = "/".join(relative)
    observed_root = _node(directory_fd, authority)
    if expected.get(root_key) != observed_root:
        raise FixtureMutationError(
            f"Harbor task fixture changed while sealing: {task_root}"
        )
    seen.add(root_key)
    try:
        iterator = os.scandir(directory_fd)
    except OSError as error:
        raise FixtureMutationError(
            f"cannot verify Harbor task root safely: {task_root}"
        ) from error
    with iterator:
        for entry in iterator:
            budget.take_entry()
            child_relative = (*relative, entry.name)
            path = "/".join(child_relative)
            expected_node = expected.get(path)
            if expected_node is None:
                raise FixtureMutationError(
                    f"Harbor task fixture changed while sealing: {task_root}"
                )
            try:
                child_fd = os.open(entry.name, _ENTRY_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise FixtureMutationError(
                    f"Harbor task fixture changed while sealing: {task_root}"
                ) from error
            try:
                metadata = _require_same_tree(child_fd, authority)
                if stat.S_ISDIR(metadata.st_mode):
                    next_depth = depth + 1
                    if next_depth > budget.config.max_depth:
                        raise FixtureLimitError("fixture exceeds max_depth")
                    budget.take_directory()
                    if expected_node.kind != "directory":
                        raise FixtureMutationError(
                            f"Harbor task fixture changed while sealing: {task_root}"
                        )
                    _verify_fixture_directory(
                        child_fd,
                        authority=authority,
                        task_root=task_root,
                        relative=child_relative,
                        depth=next_depth,
                        budget=budget,
                        expected=expected,
                        seen=seen,
                    )
                elif stat.S_ISREG(metadata.st_mode):
                    budget.take_file()
                    if expected_node.kind != "file":
                        raise FixtureMutationError(
                            f"Harbor task fixture changed while sealing: {task_root}"
                        )
                    digest = _hash_open_file(child_fd, expected_node, authority)
                    if digest != expected_node.digest:
                        raise FixtureMutationError(
                            f"Harbor task fixture content changed while sealing: "
                            f"{task_root}/{path}"
                        )
                    budget.take_bytes(expected_node.size)
                    seen.add(path)
                else:
                    raise FixtureMutationError(
                        f"Harbor task fixture changed while sealing: {task_root}"
                    )
            finally:
                os.close(child_fd)


def _seal_fixture_trees(
    authority: ProjectRootAuthority,
    fixture_roots: tuple[str, ...],
    config: ContextConfig,
    *,
    used_files: int,
    used_bytes: int,
) -> list[_StagedFixtureFile]:
    if not fixture_roots:
        return []
    try:
        validate_context_destinations(fixture_roots)
    except ValueError as error:
        raise FixtureTrustError(
            f"selected Harbor task root destinations are invalid: {error}"
        ) from error
    budget = _DiscoveryBudget(config, files=used_files, bytes=used_bytes)
    files: list[_StagedFixtureFile] = []
    snapshots: list[_FixtureSnapshot] = []
    for task_root in fixture_roots:
        budget.take_directory()
        try:
            task_fd = authority.open_directory(task_root)
        except OSError as error:
            raise FixtureTrustError(
                f"Harbor task root is missing, not a directory, or unsafe: {task_root}"
            ) from error
        try:
            entries: dict[str, _Node] = {}
            _discover_fixture_directory(
                task_fd,
                authority=authority,
                task_root=task_root,
                relative=(),
                depth=0,
                budget=budget,
                files=files,
                entries=entries,
            )
            snapshots.append(_FixtureSnapshot(task_root, entries))
        finally:
            os.close(task_fd)

    _fixture_sealing_checkpoint("before-verification")
    verification = _DiscoveryBudget(config, files=used_files, bytes=used_bytes)
    for snapshot in snapshots:
        verification.take_directory()
        try:
            task_fd = authority.open_directory(snapshot.root)
        except (OSError, FixtureTrustError) as error:
            raise FixtureMutationError(
                f"Harbor task fixture changed while sealing: {snapshot.root}"
            ) from error
        try:
            seen: set[str] = set()
            _verify_fixture_directory(
                task_fd,
                authority=authority,
                task_root=snapshot.root,
                relative=(),
                depth=0,
                budget=verification,
                expected=snapshot.entries,
                seen=seen,
            )
            if seen != set(snapshot.entries):
                raise FixtureMutationError(
                    f"Harbor task fixture changed while sealing: {snapshot.root}"
                )
        finally:
            os.close(task_fd)
    return files


def resolve_context(
    root: Path,
    config: ContextConfig,
) -> tuple[ResolvedContextFile, ...]:
    """Resolve context metadata; use ``seal_context`` when bytes are needed."""
    sealed = seal_context(root, config)
    return tuple(
        ResolvedContextFile(
            destination=item.destination,
            mode=item.mode,
            size=item.content.size,
            sha256=item.content.sha256,
        )
        for item in sealed.manifest.files
    )


def seal_context(
    root: Path,
    config: ContextConfig,
    *,
    key_prefix: str = "",
    fixture_roots: tuple[str, ...] = (),
    authority: ProjectRootAuthority | None = None,
) -> SealedContext:
    """Seal explicit files and complete selected fixture trees into immutable bytes."""
    owned_authority = authority is None
    authority = authority or open_project_root(root)
    manifest_files: list[ContextManifestFile] = []
    sealed_files: list[SealedContextFile] = []
    total = 0
    try:
        for spec in config.files:
            data, source_mode = _read_regular_file(
                authority,
                spec.source,
                config.max_file_bytes,
                config.max_total_bytes - total,
            )
            total += len(data)
            digest = hashlib.sha256(data).hexdigest()
            descriptor = ContentObject(
                sha256=digest,
                key=content_object_key(digest, prefix=key_prefix),
                size=len(data),
                media_type="application/octet-stream",
            )
            manifest_files.append(
                ContextManifestFile(
                    destination=spec.destination,
                    mode=493 if source_mode & 0o111 else 420,
                    content=descriptor,
                )
            )
            sealed_files.append(SealedContextFile(descriptor=descriptor, content=data))

        fixture_files = _seal_fixture_trees(
            authority,
            fixture_roots,
            config,
            used_files=len(manifest_files),
            used_bytes=total,
        )
        for item in fixture_files:
            digest = hashlib.sha256(item.content).hexdigest()
            descriptor = ContentObject(
                sha256=digest,
                key=content_object_key(digest, prefix=key_prefix),
                size=len(item.content),
                media_type="application/octet-stream",
            )
            manifest_files.append(
                ContextManifestFile(
                    destination=item.destination,
                    mode=493 if item.mode & 0o111 else 420,
                    content=descriptor,
                )
            )
            sealed_files.append(
                SealedContextFile(descriptor=descriptor, content=item.content)
            )

        try:
            validate_context_destinations(item.destination for item in manifest_files)
        except ValueError as error:
            raise FixtureTrustError(
                f"context destination validation failed: {error}"
            ) from error
        ordered = sorted(
            zip(manifest_files, sealed_files, strict=True),
            key=lambda pair: pair[0].destination,
        )
        authority.verify_path()
        return SealedContext(
            manifest=ContextManifest(
                schema_version=1,
                files=tuple(item[0] for item in ordered),
            ),
            files=tuple(item[1] for item in ordered),
        )
    finally:
        if owned_authority:
            authority.close()
