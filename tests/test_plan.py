from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tetrabench.canonical_json import dumps_canonical_json, sha256_hex
from tetrabench.models import ResolvedPlan, is_legacy_reward_plan
from tetrabench.plan import (
    canonical_model_bytes,
    parse_canonical_model,
    plan_digest,
    resolve_plan,
)
from tetrabench.records import ContextManifest, RequestRecord

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
    manifest = ContextManifest(schema_version=1, files=())
    request = RequestRecord(
        schema_version=1,
        run_id="example-run",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )

    assert plan_bytes == (
        b'{"context":[],"controller":{"app_name":"tetrabench","function_name":'
        b'"controller","kind":"modal","secret_name":"tetrabench-controller"},'
        b'"execution":{"kind":"modal"},"harbor":{"agent_name":"oracle","attempts"'
        b':1,"concurrency":1,"model_name":null},"not_runnable_reasons":["section '
        b"'systems-design' contains no selected tasks"
        b'"],"runnable":false,'
        b'"schema_version":1,"section":"systems-design","selection":{"exclude":'
        b'[],"include":[]},"storage":{"bucket":"replace-with-private-bucket",'
        b'"endpoint_url":"https://t3.storage.dev","prefix":"","provider":'
        b'"tigris","region":"auto"},"trials":[]}'
    )
    assert plan_digest(plan) == (
        "af01aa14492dd6680423979437a0bb628e08cda7c2635d1d66fb867e7460f1a6"
    )
    request_bytes = canonical_model_bytes(request)
    assert parse_canonical_model(request_bytes, RequestRecord) == request


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


def test_old_plan_without_reward_policy_retains_legacy_numeric_identity() -> None:
    legacy = ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "integration",
            "controller": {"kind": "local"},
            "execution": {"kind": "docker"},
            "storage": None,
            "selection": {},
            "harbor": {},
            "context": (),
            "trials": ({"task_id": "task", "harbor_task": "task"},),
            "runnable": True,
            "not_runnable_reasons": (),
        }
    )
    value = legacy.model_dump(mode="json")
    value["trials"][0].pop("reward_policy")
    old_bytes = dumps_canonical_json(value)
    plan = parse_canonical_model(old_bytes, ResolvedPlan)

    assert plan.trials[0].reward_policy == "numeric"
    assert is_legacy_reward_plan(plan)
    assert canonical_model_bytes(plan) == old_bytes
    assert plan_digest(plan) == sha256_hex(old_bytes)
