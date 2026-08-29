"""Narrow configuration loading and precedence."""

from __future__ import annotations

import tomllib
from pathlib import Path

from platformdirs import user_config_path
from pydantic import BaseModel

from tetrabench.models import (
    ConfigOverrides,
    ProfilePatch,
    ProjectConfig,
    ProjectConfigPatch,
    UserConfig,
)

PROJECT_CONFIG_NAME = "tetrabench.toml"


def default_user_config_path() -> Path:
    return user_config_path("tetrabench") / "config.toml"


def _read_toml(path: Path, *, data: bytes | None = None) -> dict[str, object]:
    try:
        if data is not None:
            return tomllib.loads(data.decode("utf-8"))
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as error:
        raise ValueError(f"configuration file does not exist: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid TOML in {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise ValueError(f"invalid UTF-8 in {path}") from error


def load_user_config(path: Path | None = None) -> UserConfig:
    selected = path or default_user_config_path()
    if not selected.exists():
        return UserConfig(schema_version=1)
    return UserConfig.model_validate(_read_toml(selected))


def _merge_fields(
    current: dict[str, object],
    patch: dict[str, object],
    fields: tuple[str, ...],
) -> dict[str, object]:
    merged = dict(current)
    for field in fields:
        if field in patch and patch[field] is not None:
            merged[field] = patch[field]
    return merged


def _merge_variant(
    current: object,
    patch: BaseModel,
    *,
    discriminator: str,
    fields: tuple[str, ...],
) -> dict[str, object]:
    patch_values = patch.model_dump(exclude_none=True)
    current_values = current if isinstance(current, dict) else {}
    new_kind = patch_values.get(discriminator)
    if new_kind is not None and new_kind != current_values.get(discriminator):
        current_values = {}
    return _merge_fields(current_values, patch_values, fields)


def _apply_profile_patch(
    values: dict[str, object],
    layer: ProfilePatch,
) -> None:
    if layer.controller is not None:
        values["controller"] = _merge_variant(
            values.get("controller"),
            layer.controller,
            discriminator="kind",
            fields=("kind", "app_name", "function_name", "secret_name"),
        )
    if layer.execution is not None:
        values["execution"] = _merge_variant(
            values.get("execution"),
            layer.execution,
            discriminator="kind",
            fields=("kind",),
        )
    if layer.storage is not None:
        values["storage"] = _merge_variant(
            values.get("storage"),
            layer.storage,
            discriminator="provider",
            fields=("provider", "bucket", "region", "prefix"),
        )
    if layer.context_files is not None:
        context = values.get("context")
        context_values = context if isinstance(context, dict) else {}
        values["context"] = _merge_fields(
            context_values,
            {"files": layer.context_files},
            ("files",),
        )
    if layer.selection is not None:
        selection = values.get("selection")
        selection_values = selection if isinstance(selection, dict) else {}
        values["selection"] = _merge_fields(
            selection_values,
            layer.selection.model_dump(exclude_none=True),
            ("include", "exclude"),
        )
    if layer.harbor is not None:
        harbor = values.get("harbor")
        harbor_values = harbor if isinstance(harbor, dict) else {}
        values["harbor"] = _merge_fields(
            harbor_values,
            layer.harbor.model_dump(exclude_none=True),
            ("agent_name", "model_name", "attempts", "concurrency"),
        )


def load_project_config(
    root: Path,
    *,
    profile: str | None = None,
    overrides: ConfigOverrides | None = None,
    user_path: Path | None = None,
    project_data: bytes | None = None,
) -> ProjectConfig:
    """Validate and merge typed patches, then validate one complete profile."""
    project = ProjectConfigPatch.model_validate(
        _read_toml(root / PROJECT_CONFIG_NAME, data=project_data)
    )
    values = ProjectConfig(schema_version=1).model_dump(mode="python")
    values["schema_version"] = project.schema_version
    if project.catalog_path is not None:
        values["catalog_path"] = project.catalog_path
    _apply_profile_patch(values, project)
    if project.context is not None:
        context = values.get("context")
        context_values = context if isinstance(context, dict) else {}
        values["context"] = _merge_fields(
            context_values,
            project.context.model_dump(exclude_none=True),
            (
                "files",
                "max_files",
                "max_entries",
                "max_directories",
                "max_depth",
                "max_file_bytes",
                "max_total_bytes",
            ),
        )
    if profile is not None:
        profiles = load_user_config(user_path).profiles
        try:
            selected_profile = profiles[profile]
        except KeyError as error:
            raise ValueError(f"unknown user profile: {profile}") from error
        _apply_profile_patch(values, selected_profile)
    if overrides is not None:
        _apply_profile_patch(values, overrides)
    return ProjectConfig.model_validate(values)
