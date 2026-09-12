"""Versioned, secret-free authentication references for resolved harnesses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AuthMode = Literal["api_key", "chatgpt_oauth", "claude_setup_token"]
AuthHarness = Literal["codex", "claude-code", "opencode", "pi"]
Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]


class EnvAuthReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    kind: Literal["env"] = "env"
    name: Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")]


class NativeAuthReference(BaseModel):
    """A generation identifies a login lineage, not a rotating access token."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    kind: Literal["native_session"] = "native_session"
    profile: Identifier
    generation: Annotated[int, Field(ge=1, le=2**53 - 1)]
    binding: Identifier


AuthReference = Annotated[
    EnvAuthReference | NativeAuthReference, Field(discriminator="kind")
]


class AuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    mode: AuthMode
    reference: AuthReference

    @model_validator(mode="after")
    def check_reference(self) -> AuthSpec:
        if (self.mode == "chatgpt_oauth") != isinstance(
            self.reference, NativeAuthReference
        ):
            raise ValueError("ChatGPT OAuth requires a native_session reference only")
        return self


class ProfileAuthReference(BaseModel):
    """Authoring selector; never accepted in an immutable execution record."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["profile"]
    profile: Identifier


class ProfileAuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    mode: Literal["chatgpt_oauth"]
    reference: ProfileAuthReference


# These select credentials or alternate billing routes, not ordinary model options.
AUTH_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT_ID",
        "CODEX_HOME",
        "CODEX_AUTH_JSON",
        "CODEX_AUTH_JSON_PATH",
        "CODEX_FORCE_AUTH_JSON",
        "CODEX_ACCESS_TOKEN",
        "CODEX_REFRESH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CONFIG_DIR",
        "OPENCODE_AUTH_CONTENT",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_TEST_HOME",
        "PI_CODING_AGENT_DIR",
        "PI_AUTH_FILE",
        "PI_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    }
)

NATIVE_AUTH_PINS: dict[str, str] = {
    "codex": "0.154.0",
    "claude-code": "2.1.269",
    "opencode": "1.18.30",
    "pi": "0.85.1",
}

NATIVE_AUTH_VERSIONS = {
    name: frozenset({version}) for name, version in NATIVE_AUTH_PINS.items()
} | {"claude-code": frozenset({"2.1.267", "2.1.269"})}


def validate_auth_spec(
    harness: str,
    spec: AuthSpec | ProfileAuthSpec | None,
    *,
    env: Mapping[str, str] | None = None,
    version: str | None = None,
    model: str | None = None,
) -> None:
    """Offline validation. None preserves the existing harness.env contract."""
    if spec is None:
        return
    if harness not in NATIVE_AUTH_PINS:
        raise ValueError("explicit auth is unsupported for this harness")
    if version is not None and version not in NATIVE_AUTH_VERSIONS[harness]:
        raise ValueError("explicit auth requires the verified native version")
    if spec.mode == "api_key" and model is not None:
        credential_env_name(harness, spec.mode, model=model)
    if spec.mode == "claude_setup_token" and harness != "claude-code":
        raise ValueError("Claude subscription credentials are Claude Code only")
    if spec.mode == "chatgpt_oauth" and harness == "claude-code":
        raise ValueError("Claude Code does not support Codex OAuth")
    if set(env or {}) & authentication_environment_names():
        raise ValueError(
            "explicit auth cannot be mixed with harness.env auth selectors"
        )
    if model and spec.mode == "chatgpt_oauth":
        prefix = {"opencode": "openai/", "pi": "openai-codex/"}.get(harness)
        if prefix and not model.startswith(prefix):
            raise ValueError(
                "ChatGPT OAuth requires the harness's native Codex provider"
            )


def api_key_provider(model: str | None) -> tuple[str, tuple[str, ...]]:
    """Use Harbor's pinned provider metadata; never guess an env name."""
    from harbor.agents.model_connection import (
        PROVIDERS,
        ModelConnectionSpec,
        resolve_model_connection,
    )

    if not model or "/" not in model:
        raise ValueError("API-key auth requires the selected provider/model")
    connection = resolve_model_connection(
        model, ModelConnectionSpec(passthrough=True), lambda *names: None
    )
    provider = connection.provider
    metadata = PROVIDERS.get(provider or "")
    if (
        metadata is None
        or not metadata.api_key_envs
        or any(
            name.startswith("AWS_")
            or not name.endswith(("API_KEY", "TOKEN", "API_TOKEN"))
            for name in metadata.api_key_envs
        )
    ):
        raise ValueError(
            "selected provider has no verified single API-key auth contract"
        )
    return provider or "", metadata.api_key_envs


def authentication_environment_names() -> frozenset[str]:
    from harbor.agents.model_connection import PROVIDERS

    return AUTH_ENV_NAMES | frozenset(
        name
        for provider in PROVIDERS.values()
        for name in (
            *provider.api_key_envs,
            *provider.base_url_envs,
        )
        if not name.startswith("AWS_")
    )


def credential_env_name(
    harness: str, mode: AuthMode, *, model: str | None = None
) -> str:
    if mode == "claude_setup_token" and harness == "claude-code":
        return "CLAUDE_CODE_OAUTH_TOKEN"
    if mode == "api_key" and harness in {"codex", "claude-code"}:
        expected = "openai" if harness == "codex" else "anthropic"
        if model is not None and model.split("/", 1)[0] != expected:
            raise ValueError(
                "explicit API-key auth does not match this native harness provider"
            )
        return "OPENAI_API_KEY" if harness == "codex" else "ANTHROPIC_API_KEY"
    if mode == "api_key" and harness in {"opencode", "pi"}:
        provider, names = api_key_provider(model)
        if (
            harness == "opencode"
            and model is not None
            and model.split("/", 1)[0] != provider
        ):
            raise ValueError(
                "OpenCode API-key auth requires a native provider ID, not an alias"
            )
        return names[0]
    raise ValueError("native OAuth credentials belong in their native file store")


def validate_native_auth_config(value: object) -> None:
    """Explicit native auth cannot coexist with provider auth/endpoint overrides.

    MCP server credentials belong to tool transport, not model billing. Their
    existing sealed-reference validation continues to apply independently.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in {"mcp", "mcpservers"}:
                continue
            if normalized in {
                "apikey",
                "apikeyhelper",
                "authorization",
                "authtoken",
                "oauthtoken",
                "accesstoken",
                "refreshtoken",
                "authjson",
                "baseurl",
                "openaibaseurl",
                "bearertoken",
                "envkey",
                "experimentalauth",
                "httpheaders",
                "headers",
            }:
                raise ValueError(
                    "explicit auth refuses native model credential/route overrides"
                )
            validate_native_auth_config(child)
    elif isinstance(value, list):
        for child in value:
            validate_native_auth_config(child)
