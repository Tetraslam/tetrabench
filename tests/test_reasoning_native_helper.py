"""Exercise native helper call boundaries without constructing native runtimes."""

from pathlib import Path

import pytest
from native_consumer_support import native_modules, native_run

pytestmark = pytest.mark.native


def test_native_reasoning_helpers_offline(tmp_path):
    native_modules(required=True)
    script = Path(__file__).with_name("reasoning_native_helper.mjs")
    result = native_run(["node", "--test", str(script)], tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
