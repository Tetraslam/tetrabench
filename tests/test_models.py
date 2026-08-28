from __future__ import annotations

import pytest
from pydantic import ValidationError

from tetrabench.models import (
    AwsStorageConfig,
    ContextFileSpec,
    ProjectConfig,
    ResolvedPlan,
    ResolvedRequest,
    ResolvedTrial,
    TigrisStorageConfig,
)
from tetrabench.plan import plan_digest


def _resolved_plan(**changes: object) -> ResolvedPlan:
    value: dict[str, object] = {
        "schema_version": 1,
        "section": "systems-design",
        "controller": {"kind": "modal"},
        "execution": {"kind": "modal"},
        "storage": None,
        "selection": {},
        "context": (),
        "trials": (),
        "runnable": False,
        "not_runnable_reasons": ("empty",),
    }
    value.update(changes)
    return ResolvedPlan.model_validate(value)


def test_storage_provider_defaults_are_separate() -> None:
    aws = AwsStorageConfig(provider="aws", bucket="bucket", region="us-west-2")
    tigris = TigrisStorageConfig(provider="tigris", bucket="bucket")

    assert "endpoint_url" not in aws.model_dump()
    assert aws.region == "us-west-2"
    assert "endpoint_url" not in tigris.model_dump()
    assert tigris.region == "auto"


def test_storage_rejects_invalid_provider_combinations() -> None:
    with pytest.raises(ValidationError):
        AwsStorageConfig.model_validate({"provider": "aws", "bucket": "bucket"})
    with pytest.raises(ValidationError, match="endpoint_url"):
        AwsStorageConfig.model_validate(
            {
                "provider": "aws",
                "bucket": "bucket",
                "region": "us-west-2",
                "endpoint_url": "https://s3.example.com",
            }
        )
    with pytest.raises(ValidationError):
        TigrisStorageConfig.model_validate(
            {
                "provider": "tigris",
                "bucket": "bucket",
                "region": "us-west-2",
            }
        )
    with pytest.raises(ValidationError):
        TigrisStorageConfig.model_validate(
            {
                "provider": "tigris",
                "bucket": "bucket",
                "endpoint_url": "https://example.com",
            }
        )
    with pytest.raises(ValidationError, match="endpoint_url"):
        TigrisStorageConfig.model_validate(
            {
                "provider": "tigris",
                "bucket": "bucket",
                "endpoint_url": "https://t3.storage.dev",
            }
        )


def test_docker_requires_explicit_local_controller() -> None:
    with pytest.raises(ValidationError, match="explicit local controller"):
        ProjectConfig.model_validate(
            {
                "schema_version": 1,
                "controller": {"kind": "modal"},
                "execution": {"kind": "docker"},
            }
        )

    config = ProjectConfig.model_validate(
        {
            "schema_version": 1,
            "controller": {"kind": "local"},
            "execution": {"kind": "docker"},
        }
    )
    assert config.controller.kind == "local"


def test_resolved_records_are_frozen() -> None:
    plan = _resolved_plan()

    with pytest.raises(ValidationError, match="frozen"):
        plan.__setattr__("runnable", True)
    with pytest.raises(ValidationError, match="frozen"):
        plan.controller.__setattr__("app_name", "changed")
    with pytest.raises(ValidationError, match="frozen"):
        plan.selection.__setattr__("include", ("changed",))
    assert isinstance(plan.selection.include, tuple)
    assert isinstance(plan.context, tuple)


def test_resolved_plan_enforces_direct_deserialization_invariants() -> None:
    with pytest.raises(ValidationError, match="incompatible"):
        _resolved_plan(execution={"kind": "docker"})
    with pytest.raises(ValidationError, match="destination"):
        _resolved_plan(
            context=(
                {
                    "destination": "../escape",
                    "mode": 420,
                    "size": 0,
                    "sha256": "0" * 64,
                },
            )
        )
    duplicate = {
        "destination": "same",
        "mode": 420,
        "size": 0,
        "sha256": "0" * 64,
    }
    with pytest.raises(ValidationError, match="destinations must be unique"):
        _resolved_plan(context=(duplicate, duplicate))


@pytest.mark.parametrize("task_id", ["", " task", "task ", "bad/id"])
def test_resolved_trial_rejects_invalid_task_ids(task_id: str) -> None:
    with pytest.raises(ValidationError, match="task_id"):
        ResolvedTrial.model_validate({"task_id": task_id, "harbor_task": "task"})


def test_resolved_request_verifies_embedded_plan_digest() -> None:
    plan = _resolved_plan()
    with pytest.raises(ValidationError, match="does not match"):
        ResolvedRequest(
            schema_version=1,
            run_id="run",
            plan_sha256="0" * 64,
            plan=plan,
        )
    request = ResolvedRequest(
        schema_version=1,
        run_id="run",
        plan_sha256=plan_digest(plan),
        plan=plan,
    )
    assert request.plan is plan


@pytest.mark.parametrize("value", [" bucket", "bucket ", "\tbucket", "bucket\n"])
def test_identity_strings_reject_surrounding_whitespace(value: str) -> None:
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        AwsStorageConfig(provider="aws", bucket=value, region="us-west-2")
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        ContextFileSpec(source=value, destination="input")
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        AwsStorageConfig(
            provider="aws",
            bucket="bucket",
            region="us-west-2",
            prefix=value,
        )
