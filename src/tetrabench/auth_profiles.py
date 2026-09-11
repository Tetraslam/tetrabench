"""Private operator configuration; only AuthSpec references enter a run plan."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self

import boto3
from botocore.config import Config
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tetrabench.auth_config import (
    EnvAuthReference,
    Identifier,
    NativeAuthReference,
)
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    LocalSessionStore,
    SessionStore,
    StateRead,
    read_private,
)
from tetrabench.models import ResolvedStorageConfig
from tetrabench.nativeauth_s3 import S3SessionStore

if TYPE_CHECKING:
    from tetrabench.auth import AuthStatus

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
    # Trust the selected bucket's organization administrators, not public access.
    trust_organization_admins: bool = False

    @model_validator(mode="after")
    def validate_organization_trust(self) -> Self:
        if self.trust_organization_admins and self.storage.provider != "tigris":
            raise ValueError("organization admin trust requires Tigris")
        names = [self.access_key.name, self.secret_key.name]
        if self.session_token is not None:
            names.append(self.session_token.name)
        if len(set(names)) != len(names) or any(
            not name.startswith("TETRABENCH_AUTH_") for name in names
        ):
            raise ValueError("auth backend needs distinct TETRABENCH_AUTH_* env refs")
        return self


class NativeAuthProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    harness: Literal["codex", "opencode", "pi"]
    binding: Identifier
    generation: Annotated[int, Field(ge=1, le=2**53 - 1)] | None = None
    backend: Annotated[LocalAuthBackend | S3AuthBackend, Field(discriminator="kind")]


class AuthConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    runtime_directory: str | None = None
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
            *(
                [result.runtime_directory]
                if result.runtime_directory is not None
                else []
            ),
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
    selected = selected or auth_config_path(environment=env)
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
    if (
        profile.harness != harness
        or profile.binding != reference.binding
        or (
            profile.generation is not None
            and profile.generation != reference.generation
        )
    ):
        raise AuthError(
            "private profile does not match harness, binding, or credential generation"
        )
    return profile


def auth_config_path(
    path: Path | None = None, *, environment: Mapping[str, str] | None = None
) -> Path:
    env = os.environ if environment is None else environment
    if path is not None:
        return path.absolute()
    if env.get(AUTH_CONFIG_FILE_ENV):
        return Path(env[AUTH_CONFIG_FILE_ENV]).absolute()
    home = Path(env.get("HOME", str(Path.home())))
    return (
        Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "tetrabench/auth.toml"
    )


def _validate_name(name: str) -> None:
    try:
        NativeAuthReference(profile=name, binding="validate", generation=1)
    except ValueError:
        raise AuthError("invalid native auth profile name") from None


def profile_runtime_directory(
    config: AuthConfigFile, *, environment: Mapping[str, str] | None = None
) -> Path:
    """Resolve a local CLI runtime when called, never while parsing transport."""
    from tetrabench.auth_bootstrap import default_runtime_directory

    if config.runtime_directory is not None:
        return Path(config.runtime_directory)
    return default_runtime_directory(os.environ if environment is None else environment)


def _validate_current(
    name: str, profile: NativeAuthProfile, current: StateRead | None
) -> None:
    if current is not None and (
        current.state.profile != name
        or current.state.harness != profile.harness
        or current.state.binding != profile.binding
    ):
        raise AuthError("native auth authority does not match the configured profile")


def _read_selected_state(
    name: str, profile: NativeAuthProfile, env: Mapping[str, str], allow_online: bool
) -> StateRead | None:
    from tetrabench.auth_bootstrap import read_local_current

    if isinstance(profile.backend, LocalAuthBackend):
        current = read_local_current(
            Path(profile.backend.state_directory), profile.binding, name
        )
    else:
        if allow_online is not True:
            raise AuthError(
                "S3 auth profile selection requires an explicit online read"
            )
        current = profile_store(
            profile, engine="docker", environment=env, artifact_buckets=[]
        ).read(name)
    _validate_current(name, profile, current)
    return current


def resolve_profile_reference(
    name: str,
    *,
    harness: str | None = None,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    allow_online: bool = False,
) -> NativeAuthReference:
    """Read-only selection. The resulting generation is frozen before sealing."""
    from tetrabench.nativeauth import inspect_native_store

    _validate_name(name)
    env = os.environ if environment is None else environment
    config = load_auth_config_file(config_path, environment=env)
    profile = config.profiles.get(name)
    if profile is None or (harness is not None and profile.harness != harness):
        raise AuthError(
            "selected native auth profile is missing or belongs to another harness"
        )
    current = _read_selected_state(name, profile, env, allow_online)
    if current is None or current.state.phase != "ready":
        raise AuthError(
            "native auth profile is not ready; login or explicit stopped-owner "
            "recovery is required"
        )
    if (
        profile.generation is not None
        and profile.generation != current.state.generation
    ):
        raise AuthError(
            "fixed auth profile generation is stale; "
            "omit generation for managed lifecycle"
        )
    inspect_native_store(profile.harness, current.state.native)
    return NativeAuthReference(
        profile=name, binding=profile.binding, generation=current.state.generation
    )


def onboard_login(
    name: str,
    agent: str | None = None,
    backend_path: Path | None = None,
    replace: bool = False,
    config_path: Path | None = None,
    executable: str | None = None,
    pi_module: Path | None = None,
    device_auth: bool = True,
    *,
    environment: Mapping[str, str] | None = None,
) -> AuthStatus:
    """Create private local metadata, then let the native CLI initialize authority."""
    from tetrabench.auth import auth_login
    from tetrabench.auth_bootstrap import (
        bootstrap_profile,
        ensure_private_directory,
        require_local_filesystem,
    )
    from tetrabench.auth_config import AuthSpec
    from tetrabench.nativeauth import preflight_native_auth, refuse_ambient_conflicts

    _validate_name(name)
    env = dict(os.environ if environment is None else environment)
    if env.get(AUTH_CONFIG_CONTENT_ENV):
        raise AuthError(
            "onboarding requires a private file, not controller auth transport"
        )
    path = auth_config_path(config_path, environment=env)
    existing = (
        load_auth_config_file(path, environment=env)
        if path.exists() or path.is_symlink()
        else None
    )
    profile = existing.profiles.get(name) if existing is not None else None
    agent = agent or (profile.harness if profile else None)
    if agent not in {"codex", "opencode", "pi"}:
        raise AuthError("a new OAuth profile requires --agent codex, opencode, or pi")
    if profile is not None and profile.harness != agent:
        raise AuthError(
            "existing auth profile cannot be rebound; choose a new profile name"
        )
    prerequisite = preflight_native_auth(
        agent, executable=executable, pi_module=pi_module, environment=env
    )
    native_env = {
        key: value
        for key, value in env.items()
        if not key.startswith("TETRABENCH_AUTH_")
    }
    refuse_ambient_conflicts(agent, "chatgpt_oauth", native_env, source_name=None)
    backend = None
    if backend_path is not None:
        try:
            backend = S3AuthBackend.model_validate(
                tomllib.loads(read_private(backend_path.absolute()).decode())
            )
        except (ValueError, UnicodeError):
            raise AuthError(
                "--backend requires a private, approved S3 backend definition "
                "with env references"
            ) from None
    config = bootstrap_profile(path, name, agent, backend, env)
    profile = config.profiles[name]
    if isinstance(profile.backend, LocalAuthBackend):
        state_root = ensure_private_directory(Path(profile.backend.state_directory))
        require_local_filesystem(state_root)
    store = profile_store(
        profile, engine="docker", environment=env, artifact_buckets=[]
    )
    current = store.read(name)
    _validate_current(name, profile, current)
    if current is not None and current.state.phase == "claimed":
        raise AuthBusyError(
            "auth profile is claimed; use explicit reseed with stopped-owner evidence"
        )
    if current is not None and current.state.phase == "ready" and not replace:
        raise AuthError(
            "auth profile is already ready; use --replace for a fresh native login"
        )
    generation = current.state.generation + 1 if current else 1
    if profile.generation is not None and profile.generation != generation:
        raise AuthError(
            "fixed auth profile must target the next generation; "
            "omit generation for managed lifecycle"
        )
    ref = NativeAuthReference(
        profile=name, binding=profile.binding, generation=generation
    )
    result = auth_login(
        agent,
        AuthSpec(mode="chatgpt_oauth", reference=ref),
        executable=prerequisite.executable,
        runtime_parent=profile_runtime_directory(config, environment=env),
        environment=native_env,
        artifact_roots=[],
        store=store,
        pi_module=prerequisite.pi_module,
        device_auth=device_auth,
        previous_generation=current.state.generation if current else None,
        version=prerequisite.version,
    )
    if load_auth_config_file(path, environment=env).profiles.get(name) != profile:
        raise AuthError(
            "auth profile changed during login; stop and inspect the original authority"
        )
    return result


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
        approved_private_backend=backend.approved_private_backend,
        kms_key_id=backend.kms_key_id,
        trust_organization_admins=backend.trust_organization_admins,
    )


def auth_profile_command(
    action: Literal["login", "status", "logout", "reseed"],
    *,
    name: str,
    executable: str,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    pi_module: Path | None = None,
    allow_online: bool = False,
    device_auth: bool = True,
):
    """CLI-facing handler. No Python setup is needed beyond the private TOML file.

    Claimed profiles require retained run references and physical stop evidence.
    Reseed does not stop resources. Local login into an S3 authority explicitly
    publishes to that approved backend; it is not an automatic export.
    """
    from tetrabench.auth import AuthStatus, auth_login, auth_logout, auth_reseed
    from tetrabench.auth_config import AuthSpec
    from tetrabench.nativeauth import inspect_native_store

    env = dict(os.environ if environment is None else environment)
    config = load_auth_config_file(config_path, environment=env)
    profile = config.profiles.get(name)
    if profile is None:
        raise AuthError("native auth profile is not configured")
    if action == "login" and profile.generation is None:
        return onboard_login(
            name,
            config_path=config_path,
            executable=executable,
            pi_module=pi_module,
            environment=env,
            device_auth=device_auth,
        )
    if action == "status":
        current = _read_selected_state(name, profile, env, allow_online)
        if current is None:
            return AuthStatus(
                harness=profile.harness, mode="chatgpt_oauth", state="absent"
            )
        if (
            profile.generation is not None
            and profile.generation != current.state.generation
        ):
            raise AuthError("stale credential generation or native harness binding")
        return AuthStatus(
            harness=profile.harness,
            mode="chatgpt_oauth",
            state=current.state.phase,
            generation=current.state.generation,
            consumer_id=current.state.owner,
            native=inspect_native_store(profile.harness, current.state.native)
            if current.state.phase == "ready"
            else None,
        )
    store = profile_store(
        profile, engine="docker", environment=env, artifact_buckets=[]
    )
    generation = profile.generation
    if generation is None:
        current = store.read(name)
        _validate_current(name, profile, current)
        generation = current.state.generation if current else 1
        if action == "reseed" and current is not None:
            generation += 1
    ref = NativeAuthReference(
        profile=name, binding=profile.binding, generation=generation
    )
    spec = AuthSpec(mode="chatgpt_oauth", reference=ref)
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
            runtime_parent=profile_runtime_directory(config, environment=env),
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
                    cli_runtime_parent=profile_runtime_directory(
                        config, environment=env
                    ),
                )

    return handler(
        profile.harness,
        spec,
        executable=executable,
        runtime_parent=profile_runtime_directory(config, environment=env),
        environment=native_env,
        artifact_roots=[],
        store=store,
        pi_module=pi_module,
        stopped_owner=stopped_owner,
        device_auth=device_auth,
    )
