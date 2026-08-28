from __future__ import annotations

from pathlib import Path

import pytest

from tetrabench.context import resolve_context
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


@pytest.mark.parametrize("destination", ["/absolute", "../escape", "a/../b", "a//b"])
def test_context_rejects_unsafe_destinations(destination: str) -> None:
    with pytest.raises(ValueError, match="destination"):
        ContextFileSpec(source="input", destination=destination)
