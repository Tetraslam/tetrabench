"""Seal explicitly selected local regular files into immutable local bytes."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from tetrabench.models import ContextConfig, ResolvedContextFile
from tetrabench.records import ContentObject, ContextManifest, ContextManifestFile
from tetrabench.storage import content_object_key, verify_content_object


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
    ):
        raise RuntimeError(
            "context sealing requires POSIX dir_fd, O_DIRECTORY, O_NOFOLLOW, "
            "and O_NONBLOCK support"
        )


def _open_regular_file(path: Path) -> int:
    _require_safe_open_support()
    absolute = Path(os.path.abspath(path))
    components = absolute.parts[1:]
    if not components:
        raise ValueError(f"context source is not a regular file: {path}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    final_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        directory = os.open("/", directory_flags)
    except OSError as error:
        raise RuntimeError("cannot establish safe context traversal root") from error
    try:
        for component in components[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=directory)
            except OSError as error:
                raise ValueError(
                    f"cannot traverse context source safely: {path}"
                ) from error
            os.close(directory)
            directory = child
        try:
            return os.open(components[-1], final_flags, dir_fd=directory)
        except OSError as error:
            raise ValueError(f"cannot open context file safely: {path}") from error
    finally:
        os.close(directory)


def _read_regular_file(
    path: Path,
    max_file_bytes: int,
    remaining_total_bytes: int,
) -> tuple[bytes, int]:
    descriptor = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"context source is not a regular file: {path}")
        if before.st_size > max_file_bytes:
            raise ValueError(f"context source exceeds max_file_bytes: {path}")
        if before.st_size > remaining_total_bytes:
            raise ValueError("context exceeds max_total_bytes")
        data = os.read(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_ctime_ns,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_ctime_ns,
            after.st_mtime_ns,
        ):
            raise ValueError(f"context source changed while reading: {path}")
        if len(data) != before.st_size:
            raise ValueError(f"context source changed while reading: {path}")
        return data, before.st_mode
    finally:
        os.close(descriptor)


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
) -> SealedContext:
    """Read each selected file once and retain separately verified immutable bytes."""
    if len(config.files) > config.max_files:
        raise ValueError("context contains more than max_files")
    manifest_files: list[ContextManifestFile] = []
    sealed_files: list[SealedContextFile] = []
    destinations: set[str] = set()
    total = 0
    for spec in config.files:
        if spec.destination in destinations:
            raise ValueError("context destinations must be unique")
        destinations.add(spec.destination)
        configured_source = Path(spec.source)
        source = (
            configured_source
            if configured_source.is_absolute()
            else root / configured_source
        )
        remaining = config.max_total_bytes - total
        data, source_mode = _read_regular_file(
            source,
            config.max_file_bytes,
            remaining,
        )
        total += len(data)
        if total > config.max_total_bytes:
            raise ValueError("context exceeds max_total_bytes")
        mode = 493 if source_mode & 0o111 else 420
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
                mode=mode,
                content=descriptor,
            )
        )
        sealed_files.append(SealedContextFile(descriptor=descriptor, content=data))
    return SealedContext(
        manifest=ContextManifest(schema_version=1, files=tuple(manifest_files)),
        files=tuple(sealed_files),
    )
