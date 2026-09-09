"""One catalog mutation for minimal category authoring."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from pathlib import Path

import tomlkit
from tomlkit.items import Table

from tetrabench.authoring import _catalog_path, _project_root, _validated_section
from tetrabench.catalog import load_catalog
from tetrabench.config import load_project_config
from tetrabench.storage import validate_logical_path


def create_category(project: Path, name: str, readme: str) -> None:
    """Append a category using the same advisory lock as task add.

    The caller supplies an existing README relative to the catalog directory;
    the only mutation is one atomic catalog replacement.
    """
    root = _project_root(project)
    name = _validated_section(name)
    readme = validate_logical_path(readme)
    config = load_project_config(root)
    catalog_path = _catalog_path(root, config.catalog_path)
    documentation = catalog_path.parent / readme
    if (
        documentation != documentation.resolve(strict=True)
        or not documentation.is_file()
    ):
        raise ValueError(
            "category README must be an existing regular file without symlinks"
        )
    parent = os.open(catalog_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    temporary = None
    try:
        lock = os.open(
            f".{catalog_path.name}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("catalog lock must be a single-link regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)

        def read_current():
            fd = os.open(
                catalog_path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                    raise ValueError("catalog must be a bounded regular file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("catalog exceeds 2 MiB")
                return info, data
            finally:
                os.close(fd)

        original_stat, original = read_current()
        catalog = load_catalog(root, config.catalog_path, catalog_data=original)
        if name in catalog.sections:
            raise ValueError("catalog category already exists")
        document = tomlkit.parse(original.decode())
        sections = document["sections"]
        if not isinstance(sections, Table):
            raise ValueError("catalog sections must be a table")
        entry = tomlkit.table()
        entry["readme"] = readme
        entry["tasks"] = []
        sections[name] = entry
        updated = tomlkit.dumps(document).encode()
        load_catalog(root, config.catalog_path, catalog_data=updated)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{catalog_path.name}.", dir=catalog_path.parent
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(updated)
                stream.flush()
            os.fchmod(fd, stat.S_IMODE(original_stat.st_mode))
            os.fsync(fd)
        finally:
            os.close(fd)
        checked_stat, checked = read_current()
        if checked != original or (checked_stat.st_dev, checked_stat.st_ino) != (
            original_stat.st_dev,
            original_stat.st_ino,
        ):
            raise RuntimeError("catalog changed while creating the category")
        os.replace(
            Path(temporary).name,
            catalog_path.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        temporary = None
        os.fsync(parent)
    finally:
        if temporary is not None:
            os.unlink(Path(temporary).name, dir_fd=parent)
        if lock is not None:
            os.close(lock)
        os.close(parent)
