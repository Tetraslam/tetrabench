from __future__ import annotations

import pytest
from pydantic import ValidationError

from tetrabench.models import (
    MAX_HARBOR_ATTEMPTS,
    MAX_HARBOR_CONCURRENCY,
    MAX_HARBOR_TASKS,
    AwsStorageConfig,
    ContextConfig,
    ContextFileSpec,
    ProjectConfig,
    ResolvedPlan,
    ResolvedTrial,
    TigrisStorageConfig,
)
from tetrabench.plan import plan_digest
from tetrabench.records import ContextManifest, RequestRecord


def _resolved_plan(**changes: object) -> ResolvedPlan:
    value: dict[str, object] = {
        "schema_version": 1,
        "section": "systems-design",
        "controller": {"kind": "modal"},
        "execution": {"kind": "modal"},
        "storage": None,
        "selection": {},
        "harbor": {},
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


def test_harbor_model_and_agent_are_opaque_and_limits_are_positive() -> None:
    config = ProjectConfig.model_validate(
        {
            "schema_version": 1,
            "harbor": {
                "agent_name": "custom.agent:Agent",
                "model_name": "opaque/provider/model",
                "attempts": 2,
                "concurrency": 4,
            },
        }
    )
    assert config.harbor.model_name == "opaque/provider/model"
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ProjectConfig.model_validate({"schema_version": 1, "harbor": {"attempts": 0}})
    for field, value in (
        ("attempts", MAX_HARBOR_ATTEMPTS + 1),
        ("concurrency", MAX_HARBOR_CONCURRENCY + 1),
    ):
        with pytest.raises(ValidationError, match="less than or equal"):
            ProjectConfig.model_validate(
                {"schema_version": 1, "harbor": {field: value}}
            )


def test_resolved_plan_rejects_unbounded_task_expansion() -> None:
    trials = tuple(
        {"task_id": f"task-{index}", "harbor_task": f"task-{index}"}
        for index in range(MAX_HARBOR_TASKS + 1)
    )
    with pytest.raises(ValidationError, match="more than 256 trials"):
        _resolved_plan(trials=trials, runnable=True, not_runnable_reasons=())


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

    def context_file(index: int, size: int = 0) -> dict[str, object]:
        return {
            "destination": f"files/{index}",
            "mode": 420,
            "size": size,
            "sha256": "0" * 64,
        }

    with pytest.raises(ValidationError, match="more than 256 files"):
        _resolved_plan(context=tuple(context_file(index) for index in range(257)))
    with pytest.raises(ValidationError, match="16 MiB"):
        _resolved_plan(context=(context_file(0, 16 * 1024 * 1024 + 1),))
    with pytest.raises(ValidationError, match="128 MiB"):
        _resolved_plan(
            context=tuple(context_file(index, 16 * 1024 * 1024) for index in range(9))
        )
    with pytest.raises(ValidationError, match="less than or equal"):
        _resolved_plan(context=(context_file(0, 1 << 53),))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("a", "a"),
        ("a", "a/b"),
        ("café", "cafe\N{COMBINING ACUTE ACCENT}"),
        ("Straße", "STRASSE"),
    ],
)
def test_complete_destination_contract_rejects_portable_collisions(
    first: str, second: str
) -> None:
    context = tuple(
        {
            "destination": destination,
            "mode": 420,
            "size": 0,
            "sha256": "0" * 64,
        }
        for destination in (first, second)
    )
    with pytest.raises(ValidationError, match=r"unique|prefix|NFC|casefold"):
        _resolved_plan(context=context)


def test_context_discovery_defaults_and_entry_relation() -> None:
    config = ContextConfig()
    assert config.max_entries == 10_000
    assert config.max_directories == 10_000
    assert config.max_depth == 64
    with pytest.raises(ValidationError, match="max_entries cannot be lower"):
        ContextConfig(max_files=2, max_entries=1)


@pytest.mark.parametrize("task_id", ["", " task", "task ", "bad/id"])
def test_resolved_trial_rejects_invalid_task_ids(task_id: str) -> None:
    with pytest.raises(ValidationError, match="task_id"):
        ResolvedTrial.model_validate({"task_id": task_id, "harbor_task": "task"})


def test_resolved_request_verifies_embedded_plan_digest() -> None:
    plan = _resolved_plan()
    manifest = ContextManifest(schema_version=1, files=())
    from tetrabench.canonical_json import sha256_hex
    from tetrabench.plan import canonical_model_bytes

    manifest_digest = sha256_hex(canonical_model_bytes(manifest))
    with pytest.raises(ValidationError, match="does not match"):
        RequestRecord(
            schema_version=1,
            run_id="run",
            plan_sha256="0" * 64,
            plan=plan,
            context_manifest_sha256=manifest_digest,
            context_manifest=manifest,
        )
    request = RequestRecord(
        schema_version=1,
        run_id="run",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=manifest_digest,
        context_manifest=manifest,
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
