from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    del config
    expected_text = os.environ.get("TETRABENCH_EXPECT_DOCKER_TESTS")
    if expected_text is None:
        return
    try:
        expected = int(expected_text)
    except ValueError as exc:
        raise pytest.UsageError(
            "TETRABENCH_EXPECT_DOCKER_TESTS must be an integer"
        ) from exc
    actual = sum(item.get_closest_marker("docker") is not None for item in items)
    if actual != expected:
        raise pytest.UsageError(
            f"expected {expected} Docker tests, collected {actual}; "
            "a required Docker marker may be missing"
        )
