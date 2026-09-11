"""Shared coordinates and isolated execution for the locked native consumers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

PACKAGES = {
    "opencode": "opencode-ai",
    "codex": "@openai/codex",
    "claude-code": "@anthropic-ai/claude-code",
    "pi": "@earendil-works/pi-coding-agent",
}
MANIFEST = Path(__file__).resolve().parents[1] / "tools/native_consumers/package.json"
VERSIONS = {
    name: json.loads(MANIFEST.read_text())["dependencies"][package]
    for name, package in PACKAGES.items()
}

# File-level coverage, not a frozen test count. The option-boundary module also
# contains historical, Python-only fixtures; the other native suites are all-in.
REQUIRED_NATIVE_SUITES = {
    "test_harness_option_boundaries.py": False,
    "test_stable_native_consumers.py": True,
    "test_nativeauth_consumers.py": True,
    "test_reasoning_native_helper.py": True,
    "test_reasoning_installed.py": True,
}


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    collected = tuple(items)
    yield
    if (
        os.environ.get("TETRABENCH_REQUIRE_NATIVE_CONSUMERS") != "1"
        or config.option.markexpr != "native"
    ):
        return
    selected = {item.path.name for item in items}
    missing = set(REQUIRED_NATIVE_SUITES) - selected
    unmarked = [
        item.nodeid
        for item in collected
        if REQUIRED_NATIVE_SUITES.get(item.path.name)
        and item.get_closest_marker("native") is None
    ]
    if missing or unmarked:
        raise pytest.UsageError(
            f"required native suite incomplete: missing suites={sorted(missing)}; "
            f"unmarked cases={sorted(unmarked)}"
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if (
        os.environ.get("TETRABENCH_REQUIRE_NATIVE_CONSUMERS") == "1"
        and item.get_closest_marker("native") is not None
        and report.skipped
    ):
        report.outcome = "failed"
        report.longrepr = f"required native test skipped: {item.nodeid}"
    return report


def native_modules(*, required: bool = False) -> Path | None:
    value = os.environ.get("TETRABENCH_NATIVE_NODE_MODULES")
    if not value:
        if os.environ.get("TETRABENCH_REQUIRE_NATIVE_CONSUMERS") == "1":
            pytest.fail("CI requires TETRABENCH_NATIVE_NODE_MODULES; run the installer")
        if required:
            pytest.skip("run tools/install_native_consumers.py to enable native tests")
        return None
    root = Path(value).resolve()
    for name, package in PACKAGES.items():
        metadata = json.loads((root / package / "package.json").read_text())
        assert metadata["name"] == package
        assert metadata["version"] == VERSIONS[name], (name, metadata["version"])
    return root


def native_environment(
    work: Path, extra: dict[str, str] | None = None
) -> dict[str, str]:
    home = work / "native-home"
    home.mkdir(mode=0o700, exist_ok=True)
    (home / "codex").mkdir(mode=0o700, exist_ok=True)
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "CODEX_HOME": str(home / "codex"),
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "PI_CODING_AGENT_DIR": str(home / "pi"),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "CI": "true",
        **(extra or {}),
    }


def native_run(
    argv: list[str], work: Path, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=work,
        env=native_environment(work, extra),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
