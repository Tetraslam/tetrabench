"""Private operator configuration; only AuthSpec references enter a run plan."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

import boto3
from botocore.config import Config
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tetrabench.auth_config import (
    EnvAuthReference,
    Identifier,
    NativeAuthReference,
)
from tetrabench.auth_sessions import (
    AuthError,
    LocalSessionStore,
    SessionStore,
    read_private,
)
from tetrabench.models import ResolvedStorageConfig
from tetrabench.nativeauth_s3 import S3SessionStore

AUTH_CONFIG_FILE_ENV = "TETRABENCH_AUTH_CONFIG_FILE"
AUTH_CONFIG_CONTENT_ENV = "TETRABENCH_AUTH_CONFIG_CONTENT"


class LocalAuthBackend(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["local"]
    approved_private_backend: Literal[True]
    state_directory: str


class S3AuthBackend(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["s3"]
    approved_private_backend: Literal[True]
    storage: ResolvedStorageConfig
    access_key: EnvAuthReference
    secret_key: EnvAuthReference
    session_token: EnvAuthReference | None = None
    kms_key_id: str | None = None


class NativeAuthProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    harness: Literal["codex", "opencode", "pi"]
    binding: Identifier
    generation: Annotated[int, Field(ge=1)]
    backend: Annotated[LocalAuthBackend | S3AuthBackend, Field(discriminator="kind")]


class AuthConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    runtime_directory: str
    profiles: dict[Identifier, NativeAuthProfile] = Field(max_length=128)


def parse_auth_config_file(
    data: bytes, *, format: Literal["toml", "json"] = "toml"
) -> AuthConfigFile:
    """No provider calls, secret resolution, native commands, or login hooks."""
    try:
        if len(data) > 128 * 1024:
            raise ValueError
        if format == "json":
            from tetrabench.auth_sessions import private_json

            result = AuthConfigFile.model_validate(private_json(data))
        else:
            result = AuthConfigFile.model_validate(tomllib.loads(data.decode()))
        paths = [
            result.runtime_directory,
            *(
                profile.backend.state_directory
                for profile in result.profiles.values()
                if isinstance(profile.backend, LocalAuthBackend)
            ),
        ]
        if any(
            not Path(path).is_absolute() or ".." in Path(path).parts for path in paths
        ):
            raise ValueError
        return result
    except (ValueError, UnicodeError, ValidationError):
        raise AuthError(
            "invalid private auth profile configuration; no values retained"
        ) from None


def load_auth_config_file(
    path: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> AuthConfigFile:
    """Load private TOML, or an explicit controller-only JSON transport definition."""
    env = os.environ if environment is None else environment
    content = env.get(AUTH_CONFIG_CONTENT_ENV)
    selected = path or (
        Path(env[AUTH_CONFIG_FILE_ENV]) if env.get(AUTH_CONFIG_FILE_ENV) else None
    )
    if content and selected:
        raise AuthError("select one private auth configuration source")
    if content:
        return parse_auth_config_file(content.encode(), format="json")
    selected = selected or Path.home() / ".config/tetrabench/auth.toml"
    if not selected.is_file():
        raise AuthError(
            "native OAuth needs a private auth.toml profile "
            "or controller auth configuration"
        )
    return parse_auth_config_file(read_private(selected))


def load_auth_profile(
    config: AuthConfigFile, reference: NativeAuthReference, harness: str
) -> NativeAuthProfile:
    profile = config.profiles.get(reference.profile)
    if profile is None:
        raise AuthError(
            "declared native auth profile is not configured on this runtime"
        )
    if (profile.harness, profile.binding, profile.generation) != (
        harness,
        reference.binding,
        reference.generation,
    ):
        raise AuthError(
            "private profile does not match harness, binding, or credential generation"
        )
    return profile


def profile_store(
    profile: NativeAuthProfile,
    *,
    engine: Literal["docker", "modal"],
    environment: Mapping[str, str],
    artifact_buckets: Sequence[str],
) -> SessionStore:
    backend = profile.backend
    if isinstance(backend, LocalAuthBackend):
        if engine != "docker":
            raise AuthError(
                "Modal native OAuth requires an approved private S3 authority, "
                "not a Volume"
            )
        return LocalSessionStore(
            Path(backend.state_directory),
            binding=profile.binding,
            local_filesystem=True,
        )
    refs = [backend.access_key, backend.secret_key]
    if backend.storage.bucket in artifact_buckets:
        raise AuthError("credential authority requires a separate private bucket")
    if backend.session_token is not None:
        refs.append(backend.session_token)
    names = [ref.name for ref in refs]
    if len(set(names)) != len(names) or any(
        not name.startswith("TETRABENCH_AUTH_") for name in names
    ):
        raise AuthError(
            "auth backend needs distinct TETRABENCH_AUTH_* credential references"
        )
    if any(not environment.get(name) for name in names):
        raise AuthError("private auth backend credential reference is missing")
    # Never use the artifact store's client, ambient AWS chain, or role fallback.
    kwargs = {
        "aws_access_key_id": environment[backend.access_key.name],
        "aws_secret_access_key": environment[backend.secret_key.name],
        "region_name": backend.storage.region,
        "config": Config(
            retries={"total_max_attempts": 1},
            connect_timeout=10,
            read_timeout=30,
            ignore_configured_endpoint_urls=True,
            proxies={},
        ),
    }
    if backend.session_token is not None:
        kwargs["aws_session_token"] = environment[backend.session_token.name]
    if backend.storage.provider == "tigris":
        kwargs["endpoint_url"] = backend.storage.endpoint_url
    client = boto3.client("s3", **kwargs)
    return S3SessionStore(
        client,
        backend.storage,
        binding=profile.binding,
        artifact_buckets=artifact_buckets,
        approved_private_backend=True,
        kms_key_id=backend.kms_key_id,
    )


def auth_profile_command(
    action: Literal["login", "status", "logout", "reseed"],
    *,
    name: str,
    executable: str,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    pi_module: Path | None = None,
):
    """CLI-facing handler. No Python setup is needed beyond the private TOML file.

    Claimed profiles require retained run references and physical stop evidence.
    Reseed does not stop resources. Local login into an S3 authority explicitly
    publishes to that approved backend; it is not an automatic export.
    """
    from tetrabench.auth import auth_login, auth_logout, auth_reseed, auth_status
    from tetrabench.auth_config import AuthSpec

    env = dict(os.environ if environment is None else environment)
    config = load_auth_config_file(config_path, environment=env)
    profile = config.profiles.get(name)
    if profile is None:
        raise AuthError("native auth profile is not configured")
    ref = NativeAuthReference(
        profile=name, binding=profile.binding, generation=profile.generation
    )
    spec = AuthSpec(mode="chatgpt_oauth", reference=ref)
    store = profile_store(
        profile, engine="docker", environment=env, artifact_buckets=[]
    )
    if action == "status":
        return auth_status(profile.harness, spec, store=store)
    # Provider credentials remain in the client only, never the native command env.
    native_env = {
        key: value
        for key, value in env.items()
        if not key.startswith("TETRABENCH_AUTH_")
    }
    handler = {"login": auth_login, "logout": auth_logout, "reseed": auth_reseed}[
        action
    ]
    if action == "logout":
        return auth_logout(
            profile.harness,
            spec,
            executable=executable,
            runtime_parent=Path(config.runtime_directory),
            environment=native_env,
            artifact_roots=[],
            store=store,
            pi_module=pi_module,
        )
    stopped_owner = None
    if action == "reseed":
        previous = store.read(name)
        if previous is not None and previous.state.owner is not None:
            from tetrabench.auth_recovery import prove_previous_consumer_stopped

            previous_owner = previous.state.owner
            previous_run = previous.state.consumer_run_id

            def stopped_owner(owner: str) -> bool:
                return owner == previous_owner and prove_previous_consumer_stopped(
                    owner,
                    previous_run,
                    cli_runtime_parent=Path(config.runtime_directory),
                )

    return handler(
        profile.harness,
        spec,
        executable=executable,
        runtime_parent=Path(config.runtime_directory),
        environment=native_env,
        artifact_roots=[],
        store=store,
        pi_module=pi_module,
        stopped_owner=stopped_owner,
    )
