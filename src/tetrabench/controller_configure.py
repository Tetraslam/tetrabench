"""Offline controller configuration intent and explicitly confirmed Secret writes."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

import modal

from tetrabench.auth_config import (
    EnvAuthReference,
    NativeAuthReference,
    ProfileAuthReference,
)
from tetrabench.auth_profiles import (
    AUTH_CONFIG_CONTENT_ENV,
    AUTH_CONFIG_FILE_ENV,
    AuthConfigFile,
    NativeAuthProfile,
    S3AuthBackend,
    load_auth_config_file,
    load_auth_profile,
    parse_auth_config_file,
    profile_store,
)
from tetrabench.auth_sessions import AuthError
from tetrabench.config import load_harness_override
from tetrabench.modal_app import ControllerDeploymentSpec, controller_deployment_spec
from tetrabench.models import ProjectConfig
from tetrabench.nativeauth_s3 import S3SessionStore

READ_TIMEOUT_SECONDS = 30
WRITE_TIMEOUT_SECONDS = 60
STORAGE_ENV_NAMES = frozenset({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"})
_RESERVED_ENV_NAMES = frozenset({AUTH_CONFIG_CONTENT_ENV, AUTH_CONFIG_FILE_ENV, "HOME"})
_WARNINGS = [
    "One writer per Secret; Modal provides no compare-and-swap for this operation.",
    "No deployment or authentication readiness is established by configuration.",
    "No application mutation retries or rollback; Modal's public SDK may retry "
    "transport with its native idempotency key.",
]
_UPDATE_WARNING = (
    "Update merges selected keys and preserves unrelated keys, including unknown "
    "old credential variables. It does not remove them or prove a safe auth switch. "
    "When supplied, the selected-profile JSON replaces that single variable."
)


def _selected_profiles(
    names: Sequence[str],
    *,
    path: Path | None,
    environment: Mapping[str, str] | None,
    artifact_bucket: str,
) -> AuthConfigFile | None:
    if not names:
        return None
    if len(names) > 128 or any(
        re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name) is None for name in names
    ):
        raise ValueError("invalid selected auth profile name")
    config = load_auth_config_file(path, environment=environment)
    selected = {}
    for name in sorted(set(names)):
        profile = config.profiles.get(name)
        if profile is None:
            raise AuthError("selected auth profile is not configured")
        backend = profile.backend
        if not isinstance(backend, S3AuthBackend):
            raise AuthError("controller auth profiles require a private S3 backend")
        if backend.storage.bucket == artifact_bucket:
            raise AuthError("credential authority requires a separate private bucket")
        selected[name] = profile
    return config.model_copy(update={"profiles": selected})


def _required_names(
    config: ProjectConfig,
    harness: Path | None,
    auth_config: AuthConfigFile | None,
) -> set[str]:
    required = set(STORAGE_ENV_NAMES)
    profiles = auth_config.profiles if auth_config is not None else {}
    for profile in profiles.values():
        backend = profile.backend
        if not isinstance(backend, S3AuthBackend):
            raise AuthError("controller auth profiles require a private S3 backend")
        required.update((backend.access_key.name, backend.secret_key.name))
        if backend.session_token is not None:
            required.add(backend.session_token.name)
    try:
        selected_harness = load_harness_override(harness) if harness else config.harness
    except (ValueError, OSError):
        raise ValueError("invalid harness configuration; no values retained") from None
    if selected_harness is not None:
        # Legacy harness.env also contains references only, never copied values.
        required.update(value[2:-1] for value in selected_harness.env.values())
        auth = selected_harness.auth
        if auth is not None:
            if isinstance(auth.reference, EnvAuthReference):
                required.add(auth.reference.name)
            elif isinstance(auth.reference, NativeAuthReference):
                if auth_config is None:
                    raise AuthError("selected native auth profile is not configured")
                load_auth_profile(
                    auth_config,
                    auth.reference,
                    selected_harness.name,
                )
            elif isinstance(auth.reference, ProfileAuthReference):
                selected = profiles.get(auth.reference.profile)
                if selected is None or selected.harness != selected_harness.name:
                    raise AuthError(
                        "include the harness's matching profile with --auth-profile"
                    )
    return required


def _transport(profiles: dict[str, NativeAuthProfile]) -> str:
    # The lifecycle parser owns omission semantics. Do not serialize the host's
    # runtime_directory, resolve HOME here, or include unselected auth profiles.
    content = json.dumps(
        {
            "schema_version": 1,
            "profiles": {
                name: profile.model_dump(mode="json", exclude_none=True)
                for name, profile in profiles.items()
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    parse_auth_config_file(content.encode(), format="json")
    return content


def configure_controller(
    config: ProjectConfig,
    *,
    profile: str,
    auth_profiles: Sequence[str] = (),
    env_names: Collection[str] = (),
    harness: Path | None = None,
    auth_config_path: Path | None = None,
    write: bool = False,
    confirmed: bool = False,
    update: bool = False,
    create_environment: bool = False,
    environment: Mapping[str, str] | None = None,
    modal_module: Any = modal,
) -> dict[str, object]:
    """Return safe intent/results; resolve values only after a confirmed write.

    The caller loads the requested run profile and owns interactive confirmation.
    Provider errors return ok=False without retaining exception text or values.
    After an ambiguous write, inspect native state before another invocation.
    """
    if not profile or len(profile) > 128 or any(ord(c) < 32 for c in profile):
        raise ValueError("controller configuration requires a run profile name")
    if write and not confirmed:
        raise ValueError("controller configuration write requires confirmation")
    spec = controller_deployment_spec(config, profile)
    if len(env_names) > 256 or any(
        re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) is None for name in env_names
    ):
        raise ValueError("invalid selected environment variable name")
    names = set(env_names)
    if names & _RESERVED_ENV_NAMES:
        raise ValueError("controller auth transport and HOME are internally managed")
    if config.storage is None:
        raise ValueError("controller configuration requires artifact storage")
    auth_config = _selected_profiles(
        auth_profiles,
        path=auth_config_path,
        environment=environment,
        artifact_bucket=config.storage.bucket,
    )
    profiles = auth_config.profiles if auth_config is not None else {}
    required = _required_names(config, harness, auth_config)
    missing = required - names
    if missing:
        raise ValueError(
            "explicit --env selection required: " + ", ".join(sorted(missing))
        )
    report = spec.as_dict() | {
        "ok": True,
        "action": "update" if update else "create",
        "write": write,
        "auth_profiles": sorted(profiles),
        "env_names": sorted(names),
        "secret_keys": sorted(
            names | ({AUTH_CONFIG_CONTENT_ENV} if profiles else set())
        ),
        "create_environment": create_environment,
        "environment_state": "unchecked",
        "backend_privacy": "unchecked" if profiles else "not_applicable",
        "deployed": False,
        "readiness": "unproven",
        "warnings": [*_WARNINGS, *([_UPDATE_WARNING] if update else [])],
    }
    report.update(secret_state="unchecked")  # nosec B106
    if not write:
        return report
    source = os.environ if environment is None else environment
    values: dict[str, str] = {}
    for name in sorted(names):
        value = source.get(name)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(
                "selected environment variable is missing or invalid: " + name
            )
        if len(value.encode()) > 128 * 1024:
            raise ValueError("selected environment variable exceeds 128 KiB: " + name)
        values[name] = value
    if profiles:
        values[AUTH_CONFIG_CONTENT_ENV] = _transport(profiles)
        try:
            for selected in profiles.values():
                store = profile_store(
                    selected,
                    engine="modal",
                    environment=values,
                    artifact_buckets=[config.storage.bucket],
                )
                if not isinstance(store, S3SessionStore):
                    raise AuthError("controller auth requires S3SessionStore")
                # Reuse the real topology/privacy gate without reading credential
                # objects, claiming a session, or inferring login readiness.
                store._gate()
        except Exception:
            return report | {
                "ok": False,
                "backend_privacy": "failed",
                "error": "auth_backend_privacy_failed",
            }
        report["backend_privacy"] = "verified_at_write"
    return asyncio.run(
        _write_secret(
            spec,
            values,
            report,
            update=update,
            create_environment=create_environment,
            modal_module=modal_module,
        )
    )


async def _write_secret(
    spec: ControllerDeploymentSpec,
    values: dict[str, str],
    report: dict[str, object],
    *,
    update: bool,
    create_environment: bool,
    modal_module: Any,
) -> dict[str, object]:
    stage = "modal_auth"
    try:
        client = await asyncio.wait_for(
            modal_module.Client.from_env.aio(), READ_TIMEOUT_SECONDS
        )
        stage = "environment_lookup"
        target = modal_module.Environment.from_name(
            spec.environment_name, create_if_missing=False, client=client
        )
        try:
            await asyncio.wait_for(
                target.hydrate.aio(client=client), READ_TIMEOUT_SECONDS
            )
        except modal.exception.NotFoundError:
            report["environment_state"] = "absent"
            if not create_environment:
                return report | {"ok": False, "error": "environment_missing"}
            # Updating an absent environment cannot update an existing Secret.
            if update:
                return report | {"ok": False, "error": "secret_missing"}
            stage = "environment_create"
            report["environment_state"] = "unknown"
            await asyncio.wait_for(
                modal_module.Environment.objects.create.aio(
                    spec.environment_name, client=client
                ),
                WRITE_TIMEOUT_SECONDS,
            )
            report["environment_state"] = "created"
        else:
            report["environment_state"] = "existing"
        stage = "secret_lookup"
        secret = modal_module.Secret.from_name(
            spec.secret_name, environment_name=spec.environment_name, client=client
        )
        try:
            await asyncio.wait_for(
                secret.hydrate.aio(client=client), READ_TIMEOUT_SECONDS
            )
        except modal.exception.NotFoundError:
            report.update(secret_state="absent")  # nosec B106
            if update:
                return report | {"ok": False, "error": "secret_missing"}
        else:
            report.update(secret_state="existing")  # nosec B106
            if not update:
                return report | {"ok": False, "error": "secret_exists_use_update"}
        stage = "secret_update" if update else "secret_create"
        report.update(secret_state="unknown")  # nosec B106
        # Exactly one public mutation invocation. The pinned SDK owns transport
        # retries/idempotency; its public methods expose no retry override.
        if update:
            await asyncio.wait_for(secret.update.aio(values), WRITE_TIMEOUT_SECONDS)
        else:
            await asyncio.wait_for(
                modal_module.Secret.objects.create.aio(
                    spec.secret_name,
                    values,
                    allow_existing=False,
                    environment_name=spec.environment_name,
                    client=client,
                ),
                WRITE_TIMEOUT_SECONDS,
            )
        report["secret_state"] = "updated" if update else "created"
        return report
    except (Exception, asyncio.CancelledError):
        # A timeout or interrupted response may follow a committed mutation. Do
        # not print SDK exception messages, retry, or delete partial resources.
        return report | {
            "ok": False,
            "error": stage + "_failed",
            "next_action": (
                "Inspect native state before retrying; no rollback was attempted."
            ),
        }
