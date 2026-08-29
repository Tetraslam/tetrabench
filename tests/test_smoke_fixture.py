import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "tools/smoke_fixture.py"
SPEC = importlib.util.spec_from_file_location("smoke_fixture", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SMOKE_FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE_FIXTURE)
MAX_HOLD_SECONDS = SMOKE_FIXTURE.MAX_HOLD_SECONDS
fixture_with_hold = SMOKE_FIXTURE.fixture_with_hold


def _fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "fixture"
    solution = fixture / "solution/solve.sh"
    solution.parent.mkdir(parents=True)
    solution.write_text("#!/bin/sh\nset -eu\nprintf done\\n\n")
    return fixture


def test_fixture_hold_is_bounded_and_does_not_mutate_source(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original = (fixture / "solution/solve.sh").read_text()

    with fixture_with_hold(fixture, MAX_HOLD_SECONDS) as copied:
        assert copied != fixture
        assert (
            f"sleep {MAX_HOLD_SECONDS}\n" in (copied / "solution/solve.sh").read_text()
        )

    assert (fixture / "solution/solve.sh").read_text() == original
    assert not copied.exists()


@pytest.mark.parametrize("seconds", [-1, MAX_HOLD_SECONDS + 1])
def test_fixture_hold_rejects_out_of_bounds(tmp_path: Path, seconds: int) -> None:
    with pytest.raises(ValueError, match="hold seconds"):
        with fixture_with_hold(_fixture(tmp_path), seconds):
            pass


def test_zero_hold_uses_source_fixture(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with fixture_with_hold(fixture, 0) as selected:
        assert selected == fixture


@pytest.mark.parametrize("value", ["-1", str(MAX_HOLD_SECONDS + 1), "later"])
def test_cli_rejects_invalid_hold_seconds(value: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--run-id",
            "invalid-hold",
            "--hold-seconds",
            value,
            "--yes",
        ],
        cwd=MODULE_PATH.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "hold seconds must" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
