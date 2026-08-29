from __future__ import annotations

import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tetrabench.context import SealedContextFile, resolve_context, seal_context
from tetrabench.models import ContextConfig, ContextFileSpec


def test_context_resolves_content_without_source_path(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    source.write_bytes(b"hello\n")
    config = ContextConfig(
        files=[ContextFileSpec(source="input.txt", destination="docs/input.txt")]
    )

    resolved = resolve_context(tmp_path, config)

    assert resolved[0].destination == "docs/input.txt"
    assert resolved[0].size == 6
    assert resolved[0].mode == 420
    assert resolved[0].sha256 == (
        "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
    )
    assert "source" not in resolved[0].model_dump()

    sealed = seal_context(tmp_path, config)
    assert sealed.files[0].content == b"hello\n"
    assert sealed.manifest.files[0].content == sealed.files[0].descriptor


def test_context_rejects_missing_and_symlink_sources(tmp_path: Path) -> None:
    missing = ContextConfig(
        files=[ContextFileSpec(source="missing", destination="missing")]
    )
    with pytest.raises(ValueError, match="cannot open"):
        resolve_context(tmp_path, missing)

    target = tmp_path / "target"
    target.write_text("data", encoding="utf-8")
    (tmp_path / "link").symlink_to(target)
    linked = ContextConfig(files=[ContextFileSpec(source="link", destination="link")])
    with pytest.raises(ValueError, match="safely"):
        resolve_context(tmp_path, linked)

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "file").write_bytes(b"data")
    (tmp_path / "linked-parent").symlink_to(real_parent, target_is_directory=True)
    parent_linked = ContextConfig(
        files=[ContextFileSpec(source="linked-parent/file", destination="file")]
    )
    with pytest.raises(ValueError, match=r"traverse.*safely"):
        resolve_context(tmp_path, parent_linked)


def test_context_rejects_special_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "fifo"
    fifo.parent.mkdir(exist_ok=True)
    import os

    os.mkfifo(fifo)
    config = ContextConfig(files=[ContextFileSpec(source="fifo", destination="fifo")])
    started = time.monotonic()
    with pytest.raises(ValueError, match="regular file"):
        seal_context(tmp_path, config)
    assert time.monotonic() - started < 1

    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(RuntimeError, match="requires POSIX dir_fd"):
        seal_context(tmp_path, config)


def test_context_normalizes_executable_mode(tmp_path: Path) -> None:
    source = tmp_path / "script"
    source.write_bytes(b"#!/bin/sh\n")
    source.chmod(0o711)
    config = ContextConfig(
        files=[ContextFileSpec(source="script", destination="bin/x")]
    )
    assert seal_context(tmp_path, config).manifest.files[0].mode == 493


def test_context_enforces_lowered_file_and_total_limits(tmp_path: Path) -> None:
    (tmp_path / "one").write_bytes(b"1234")
    (tmp_path / "two").write_bytes(b"5678")
    with pytest.raises(ValueError, match="max_file_bytes"):
        seal_context(
            tmp_path,
            ContextConfig(
                files=[ContextFileSpec(source="one", destination="one")],
                max_file_bytes=3,
            ),
        )
    with pytest.raises(ValueError, match="max_total_bytes"):
        seal_context(
            tmp_path,
            ContextConfig(
                files=[
                    ContextFileSpec(source="one", destination="one"),
                    ContextFileSpec(source="two", destination="two"),
                ],
                max_total_bytes=7,
            ),
        )
    with pytest.raises(ValueError, match="max_files"):
        ContextConfig(
            files=[ContextFileSpec(source="one", destination="one")],
            max_files=0,
        )


def test_context_checks_remaining_total_before_second_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tetrabench.context as context_module

    (tmp_path / "one").write_bytes(b"1234")
    (tmp_path / "two").write_bytes(b"5678")
    original_read = context_module.os.read
    requested: list[int] = []

    def recording_read(descriptor: int, count: int) -> bytes:
        requested.append(count)
        return original_read(descriptor, count)

    monkeypatch.setattr(context_module.os, "read", recording_read)
    config = ContextConfig(
        files=[
            ContextFileSpec(source="one", destination="one"),
            ContextFileSpec(source="two", destination="two"),
        ],
        max_total_bytes=7,
    )
    with pytest.raises(ValueError, match="max_total_bytes"):
        seal_context(tmp_path, config)
    assert requested == [4]


def test_context_detects_fstat_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tetrabench.context as context_module

    source = tmp_path / "input"
    source.write_bytes(b"data")
    original_fstat = context_module.os.fstat
    calls = 0

    def changing_fstat(descriptor: int) -> object:
        nonlocal calls
        result = original_fstat(descriptor)
        if stat.S_ISREG(result.st_mode):
            calls += 1
        values = {
            name: getattr(result, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_ctime_ns",
                "st_mtime_ns",
            )
        }
        if stat.S_ISREG(result.st_mode) and calls == 2:
            values["st_ctime_ns"] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(context_module.os, "fstat", changing_fstat)
    config = ContextConfig(files=[ContextFileSpec(source="input", destination="x")])
    with pytest.raises(ValueError, match="changed while reading"):
        seal_context(tmp_path, config)


def test_sealed_file_rejects_mutated_retained_bytes(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.write_bytes(b"data")
    config = ContextConfig(files=[ContextFileSpec(source="input", destination="x")])
    sealed = seal_context(tmp_path, config)
    with pytest.raises(ValueError, match=r"size|sha256"):
        SealedContextFile(descriptor=sealed.files[0].descriptor, content=b"changed")


@pytest.mark.parametrize("destination", ["/absolute", "../escape", "a/../b", "a//b"])
def test_context_rejects_unsafe_destinations(destination: str) -> None:
    with pytest.raises(ValueError, match="destination"):
        ContextFileSpec(source="input", destination=destination)
