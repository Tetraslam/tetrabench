"""Explicit CLI metadata auth: private native state, no implicit browser login."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from typing import Any

from platformdirs import user_runtime_path

from tetrabench.auth import NativeRuntime, auth_session
from tetrabench.auth_config import (
    EnvAuthReference,
    credential_env_name,
)
from tetrabench.auth_operations import cli_operation_lifetime
from tetrabench.auth_retention import KnownAuthRetention
from tetrabench.auth_sessions import AuthError, read_private
from tetrabench.harness_config import HarnessConfig
from tetrabench.harnesses import validate_explicit_auth_configuration

_retention: ContextVar[tuple[NativeRuntime, KnownAuthRetention] | None] = ContextVar(
    "metadata_auth_retention", default=None
)


def _capture_known_values(
    runtime: NativeRuntime, retention: KnownAuthRetention
) -> None:
    if runtime.claim is not None:
        retention.native(runtime.harness, runtime.claim.snapshot.state.native)
        retention.native(runtime.harness, read_private(runtime.credential_path))
    else:
        key = credential_env_name(
            runtime.harness, runtime.spec.mode, model=runtime.model
        )
        value = runtime.environment.get(key)
        if value:
            retention.add(value)
        elif runtime.harness == "codex":
            from tetrabench.auth_sessions import private_json

            document = private_json(read_private(runtime.credential_path))
            value = document.get("OPENAI_API_KEY")
            if not isinstance(value, str) or not value:
                raise AuthError("native API-key retention source is unavailable")
            retention.add(value)
    if not retention.complete:
        raise AuthError("metadata credential retention evidence is incomplete")


def assert_safe_metadata(runtime: NativeRuntime, value: Any) -> None:
    """Reject known initial/refreshed literals, including JSON-escaped echoes."""
    current = _retention.get()
    retention = (
        current[1]
        if current is not None and current[0] is runtime
        else KnownAuthRetention()
    )
    _capture_known_values(runtime, retention)
    if isinstance(value, bytes):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            value = value.decode("utf-8", errors="replace")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise AuthError("metadata retention nesting exceeds limit")
        if isinstance(item, str):
            if any(secret in item for secret in retention.values):
                raise AuthError("credential-bearing native metadata refused")
        elif isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)


def authentication_evidence(runtime: NativeRuntime | None) -> dict[str, Any]:
    observed = runtime.last_auth_status if runtime else None
    return {
        "provided": runtime is not None,
        "observed": asdict(observed) if observed else None,
        "account_verified": False,
        "account_capability_checked": False,
        "provider_metadata_fetch": "not-observed",
    }


@contextmanager
def metadata_auth_context(
    config: HarnessConfig,
    *,
    allow_authenticated_read: bool = False,
    modules: Path | None = None,
    node: str | None = None,
    auth_config: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Iterator[NativeRuntime | None]:
    if not allow_authenticated_read:
        if auth_config is not None:
            raise AuthError("--auth-config requires --allow-authenticated-read")
        yield None
        return
    from tetrabench.auth_selection import resolve_harness_auth

    config = resolve_harness_auth(
        config, allow_online=True, config_path=auth_config, environment=environment
    )
    spec = config.auth
    if spec is None:
        raise AuthError("--allow-authenticated-read requires an explicit harness.auth")
    from tetrabench.auth_config import AuthSpec

    if not isinstance(spec, AuthSpec):
        raise AuthError("authenticated metadata requires a resolved auth reference")
    validate_explicit_auth_configuration(config)
    env = dict(os.environ if environment is None else environment)
    store = None
    if isinstance(spec.reference, EnvAuthReference):
        if not env.get(spec.reference.name):
            from tetrabench.diagnostics import missing_credentials

            raise missing_credentials([spec.reference.name])
        if auth_config is not None:
            raise AuthError("API-key/setup-token references do not use --auth-config")
        parent = user_runtime_path("tetrabench") / "metadata-auth"
    else:
        from tetrabench.auth_profiles import (
            load_auth_config_file,
            load_auth_profile,
            profile_runtime_directory,
            profile_store,
        )

        private = load_auth_config_file(auth_config, environment=env)
        profile = load_auth_profile(private, spec.reference, config.name)
        store = profile_store(
            profile, engine="docker", environment=env, artifact_buckets=()
        )
        parent = profile_runtime_directory(private, environment=env)
    from tetrabench.native_discovery import native_installation

    installation = native_installation(config.name, modules=modules, node=node)
    if config.name == "pi":
        executable = installation.node
        pi_module = (
            installation.pi_package / "dist/index.js"
            if installation.pi_package
            else None
        )
    else:
        executable = installation.command[-1]
        pi_module = None
    # Backend credentials are held by its client, never forwarded to native CLI.
    source = (
        spec.reference.name if isinstance(spec.reference, EnvAuthReference) else None
    )
    native_env = {
        key: value
        for key, value in env.items()
        if not key.startswith("TETRABENCH_AUTH_") or key == source
    }
    native_env["PATH"] = (
        str(Path(installation.node).parent)
        + os.pathsep
        + native_env.get("PATH", os.defpath)
    )
    with cli_operation_lifetime(parent, operation="native-metadata") as owner:
        with auth_session(
            config.name,
            spec,
            executable=executable,
            runtime_parent=parent,
            environment=native_env,
            artifact_roots=(),
            store=store,
            pi_module=pi_module,
            consumer_id=owner,
            model=config.model,
            version=config.version,
        ) as runtime:
            if (
                runtime.last_auth_status is None
                or runtime.last_auth_status.mode != spec.mode
            ):
                raise AuthError("native metadata auth mode was not observed")
            retention = KnownAuthRetention()
            _capture_known_values(runtime, retention)
            token = _retention.set((runtime, retention))
            try:
                yield runtime
            finally:
                _retention.reset(token)
