"""Exercise the required-native gate through real pytest collection/reporting."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def suite(pytester, monkeypatch):
    monkeypatch.setenv("TETRABENCH_REQUIRE_NATIVE_CONSUMERS", "1")
    helpers = str(Path(__file__).parent)
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {helpers!r})\n"
        "import native_consumer_support\n"
        "native_consumer_support.REQUIRED_NATIVE_SUITES = {'test_required.py': True}\n"
        "pytest_plugins = ['native_consumer_support']\n"
    )
    pytester.makeini("[pytest]\nmarkers = native: requires native consumer\n")
    return pytester


@pytest.mark.parametrize("count", [1, 5])
def test_required_native_gate_accepts_growing_suites(suite, count):
    suite.makepyfile(
        test_required=(
            "import pytest\npytestmark = pytest.mark.native\n"
            f"@pytest.mark.parametrize('value', range({count}))\n"
            "def test_consumer(value): assert value >= 0\n"
        )
    )
    suite.runpytest_subprocess("-m", "native", "--strict-markers").assert_outcomes(
        passed=count
    )


def test_required_native_gate_rejects_missing_suite(suite):
    suite.makepyfile(
        test_other=("import pytest\n@pytest.mark.native\ndef test_other(): pass\n")
    )
    result = suite.runpytest_subprocess("-m", "native", "--strict-markers")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*required native suite incomplete*test_required.py*"])


def test_required_native_gate_rejects_partially_unmarked_suite(suite):
    suite.makepyfile(
        test_required=(
            "import pytest\n@pytest.mark.native\ndef test_native(): pass\n"
            "def test_unmarked(): pass\n"
        )
    )
    result = suite.runpytest_subprocess("-m", "native", "--strict-markers")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*unmarked cases=*test_unmarked*"])


def test_required_native_gate_rejects_skip(suite):
    suite.makepyfile(
        test_required=(
            "import pytest\npytestmark = pytest.mark.native\n"
            "def test_skips(): pytest.skip('consumer unavailable')\n"
        )
    )
    suite.runpytest_subprocess("-m", "native", "--strict-markers").assert_outcomes(
        failed=1
    )


def test_remaining_suite_excludes_native_without_completeness_failure(suite):
    suite.makepyfile(
        test_required=(
            "import pytest\n@pytest.mark.native\ndef test_native(): pass\n"
            "def test_unit(): pass\n"
        )
    )
    suite.runpytest_subprocess("-m", "not native", "--strict-markers").assert_outcomes(
        passed=1, deselected=1
    )
