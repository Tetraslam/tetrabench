"""Resolve explicitly selected local files without serializing source paths."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from tetrabench.models import ContextConfig, ResolvedContextFile


def _read_regular_file(path: Path, max_bytes: int) -> tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"cannot open context file safely: {path}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"context source is not a regular file: {path}")
        if before.st_size > max_bytes:
            raise ValueError(f"context source exceeds max_file_bytes: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        after = os.fstat(descriptor)
        if len(data) > max_bytes:
            raise ValueError(f"context source exceeds max_file_bytes: {path}")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"context source changed while reading: {path}")
        return data, before.st_mode
    finally:
        os.close(descriptor)


def resolve_context(
    root: Path,
    config: ContextConfig,
) -> tuple[ResolvedContextFile, ...]:
    resolved: list[ResolvedContextFile] = []
    total = 0
    for spec in config.files:
        configured_source = Path(spec.source)
        source = (
            configured_source
            if configured_source.is_absolute()
            else root / configured_source
        )
        data, source_mode = _read_regular_file(source, config.max_file_bytes)
        total += len(data)
        if total > config.max_total_bytes:
            raise ValueError("context exceeds max_total_bytes")
        mode = 493 if source_mode & 0o111 else 420
        resolved.append(
            ResolvedContextFile(
                destination=spec.destination,
                mode=mode,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return tuple(resolved)
