from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.models import ResolvedPlan, ResolvedRequest
from tetrabench.plan import (
    canonical_model_bytes,
    parse_canonical_model,
    plan_digest,
    resolve_plan,
)

ROOT = Path(__file__).parents[1]


def test_empty_section_plan_is_canonical_deterministic_and_not_runnable() -> None:
    first = resolve_plan(ROOT, "systems-design")
    second = resolve_plan(ROOT, "systems-design")

    assert first == second
    assert first.trials == ()
    assert first.runnable is False
    assert "no selected tasks" in first.not_runnable_reasons[0]
    assert canonical_model_bytes(first) == canonical_model_bytes(second)
    assert plan_digest(first) == plan_digest(second)


def test_plan_and_request_golden_bytes() -> None:
    plan = resolve_plan(ROOT, "systems-design")
    plan_bytes = canonical_model_bytes(plan)
    request = ResolvedRequest(
        schema_version=1,
        run_id="example-run",
        plan_sha256=plan_digest(plan),
        plan=plan,
    )

    assert plan_bytes == (
        b'{"context":[],"controller":{"app_name":"tetrabench","function_name":'
        b'"controller","kind":"modal","secret_name":"tetrabench-controller"},'
        b'"execution":{"kind":"modal"},"not_runnable_reasons":["section '
        b"'systems-design' contains no selected tasks"
        b'"],"runnable":false,'
        b'"schema_version":1,"section":"systems-design","selection":{"exclude":'
        b'[],"include":[]},"storage":{"bucket":"replace-with-private-bucket",'
        b'"endpoint_url":"https://t3.storage.dev","prefix":"","provider":'
        b'"tigris","region":"auto"},"trials":[]}'
    )
    assert plan_digest(plan) == (
        "421a4d777f1bba9531402b0c56b4802747ad09e0f470e9b15ee9c56fea1d61c9"
    )
    request_bytes = canonical_model_bytes(request)
    assert parse_canonical_model(request_bytes, ResolvedRequest) == request


def test_plan_parser_rejects_unknown_fields() -> None:
    value = resolve_plan(ROOT, "systems-design").model_dump(mode="json")
    value["secret_value"] = "nope"
    tampered = dumps_canonical_json(value)

    with pytest.raises(ValidationError, match="secret_value"):
        parse_canonical_model(tampered, ResolvedPlan)


def test_plan_has_no_secret_value_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-serialize")
    data = canonical_model_bytes(resolve_plan(ROOT, "systems-design"))

    assert b"must-not-serialize" not in data
    assert b"secret_value" not in data
