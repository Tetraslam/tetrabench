from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tetrabench.config import load_project_config
from tetrabench.models import ConfigOverrides

PROJECT = """\
schema_version = 1
[controller]
kind = "modal"
[execution]
kind = "modal"
[storage]
provider = "tigris"
bucket = "project-bucket"
[harbor]
agent_name = "oracle"
attempts = 1
concurrency = 1
"""


def test_unknown_project_key_fails(tmp_path: Path) -> None:
    (tmp_path / "tetrabench.toml").write_text(
        PROJECT + "\nunknown = true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unknown"):
        load_project_config(tmp_path)


def test_project_profile_override_precedence(tmp_path: Path) -> None:
    (tmp_path / "tetrabench.toml").write_text(PROJECT, encoding="utf-8")
    user_path = tmp_path / "user.toml"
    user_path.write_text(
        """\
schema_version = 1
[profiles.local.controller]
kind = "local"
[profiles.local.execution]
kind = "docker"
[profiles.local.storage]
provider = "aws"
bucket = "profile-bucket"
region = "us-east-1"
[profiles.local.harbor]
agent_name = "opencode"
model_name = "opaque/provider-model"
attempts = 2
concurrency = 3
""",
        encoding="utf-8",
    )
    overrides = ConfigOverrides.model_validate(
        {
            "storage": {
                "provider": "aws",
                "bucket": "cli-bucket",
                "region": "eu-west-1",
            }
        }
    )

    config = load_project_config(
        tmp_path,
        profile="local",
        overrides=overrides,
        user_path=user_path,
    )

    assert config.controller.kind == "local"
    assert config.execution.kind == "docker"
    assert config.storage is not None
    assert config.storage.bucket == "cli-bucket"
    assert config.storage.region == "eu-west-1"
    assert config.harbor.agent_name == "opencode"
    assert config.harbor.model_name == "opaque/provider-model"
    assert config.harbor.attempts == 2
    assert config.harbor.concurrency == 3


def test_unknown_profile_fails(tmp_path: Path) -> None:
    (tmp_path / "tetrabench.toml").write_text(PROJECT, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown user profile"):
        load_project_config(tmp_path, profile="missing", user_path=tmp_path / "none")


def test_layers_are_partial_and_only_final_profile_needs_compatibility(
    tmp_path: Path,
) -> None:
    (tmp_path / "tetrabench.toml").write_text(
        """\
schema_version = 1
[execution]
kind = "docker"
[selection]
include = ["task-a"]
""",
        encoding="utf-8",
    )
    user_path = tmp_path / "user.toml"
    user_path.write_text(
        """\
schema_version = 1
[profiles.local.controller]
kind = "local"
[profiles.local.selection]
exclude = ["task-b"]
""",
        encoding="utf-8",
    )

    config = load_project_config(tmp_path, profile="local", user_path=user_path)

    assert config.controller.kind == "local"
    assert config.execution.kind == "docker"
    assert config.selection.include == ["task-a"]
    assert config.selection.exclude == ["task-b"]


def test_partial_nested_fields_merge_without_recursive_unknown_keys(
    tmp_path: Path,
) -> None:
    (tmp_path / "tetrabench.toml").write_text(PROJECT, encoding="utf-8")
    user_path = tmp_path / "user.toml"
    user_path.write_text(
        """\
schema_version = 1
[profiles.named.storage]
bucket = "profile-bucket"
""",
        encoding="utf-8",
    )

    config = load_project_config(tmp_path, profile="named", user_path=user_path)

    assert config.storage is not None
    assert config.storage.provider == "tigris"
    assert config.storage.bucket == "profile-bucket"
    with pytest.raises(ValidationError, match="arbitrary"):
        ConfigOverrides.model_validate({"storage": {"arbitrary": {"nested": True}}})


def test_aws_endpoint_is_rejected_in_every_layer(tmp_path: Path) -> None:
    (tmp_path / "tetrabench.toml").write_text(
        """\
schema_version = 1
[storage]
provider = "aws"
bucket = "bucket"
region = "us-east-1"
endpoint_url = "https://example.com"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="endpoint_url"):
        load_project_config(tmp_path)
