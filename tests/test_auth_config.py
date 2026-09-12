from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tetrabench.auth_config import (
    AUTH_ENV_NAMES,
    AuthSpec,
    EnvAuthReference,
    NativeAuthReference,
    validate_auth_spec,
)
from tetrabench.nativeauth import refuse_ambient_conflicts


def oauth_spec() -> AuthSpec:
    return AuthSpec(
        mode="chatgpt_oauth",
        reference=NativeAuthReference(
            profile="eval-codex",
            binding="dedicated-local",
            generation=1,
        ),
    )


@pytest.mark.parametrize(
    "mode,harness,reference",
    [
        ("api_key", "codex", EnvAuthReference(name="EVAL_KEY")),
        ("api_key", "claude-code", EnvAuthReference(name="EVAL_KEY")),
        ("claude_setup_token", "claude-code", EnvAuthReference(name="EVAL_TOKEN")),
        ("chatgpt_oauth", "codex", oauth_spec().reference),
        ("chatgpt_oauth", "opencode", oauth_spec().reference),
        ("chatgpt_oauth", "pi", oauth_spec().reference),
    ],
)
def test_six_explicit_flows(mode, harness, reference):
    spec = AuthSpec(mode=mode, reference=reference)
    validate_auth_spec(harness, spec)
    assert AuthSpec.model_validate_json(spec.model_dump_json()) == spec
    assert "native" not in json.loads(spec.model_dump_json())


@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
def test_claude_subscription_never_in_alternative_clients(harness):
    spec = AuthSpec(mode="claude_setup_token", reference=EnvAuthReference(name="TOKEN"))
    with pytest.raises(ValueError, match="Claude Code only"):
        validate_auth_spec(harness, spec)


@pytest.mark.parametrize(
    "reference",
    [
        {"kind": "env", "name": "literal-secret-value"},
        {"kind": "env", "name": "TOKEN", "schema_version": 2},
        {"kind": "command", "command": "op read secret"},
        {
            "kind": "native_session",
            "profile": "../escape",
            "generation": 1,
            "binding": "local",
        },
        {
            "kind": "native_session",
            "profile": "ok",
            "generation": True,
            "binding": "local",
        },
        {
            "kind": "native_session",
            "profile": "ok",
            "generation": 1,
            "binding": "local",
            "token": "SYNTHETIC",
        },
    ],
)
def test_reference_is_strict_typed_and_versioned(reference):
    with pytest.raises(ValidationError):
        AuthSpec.model_validate({"mode": "chatgpt_oauth", "reference": reference})


def test_reference_type_follows_mode():
    with pytest.raises(ValidationError):
        AuthSpec(mode="api_key", reference=oauth_spec().reference)
    with pytest.raises(ValidationError):
        AuthSpec(mode="chatgpt_oauth", reference=EnvAuthReference(name="TOKEN"))


def test_legacy_env_unmodified_when_auth_absent():
    env = {"OPENROUTER_API_KEY": "${LEGACY_KEY}"}
    validate_auth_spec("opencode", None, env=env)
    assert env == {"OPENROUTER_API_KEY": "${LEGACY_KEY}"}


@pytest.mark.parametrize("key", sorted(AUTH_ENV_NAMES))
def test_explicit_mode_refuses_competing_env_selectors(key):
    with pytest.raises(ValueError, match="mixed"):
        validate_auth_spec("codex", oauth_spec(), env={key: "${REFERENCE}"})
    with pytest.raises(RuntimeError, match="mixed"):
        refuse_ambient_conflicts(
            "codex", "chatgpt_oauth", {key: "SYNTHETIC"}, source_name=None
        )


def test_exact_reference_only_no_destination_fallback():
    refuse_ambient_conflicts(
        "codex", "api_key", {"MY_KEY": "SYNTHETIC"}, source_name="MY_KEY"
    )
    with pytest.raises(RuntimeError, match="mixed"):
        refuse_ambient_conflicts(
            "codex",
            "api_key",
            {
                "MY_KEY": "SYNTHETIC",
                "OPENAI_API_KEY": "SYNTHETIC",
            },
            source_name="MY_KEY",
        )


def test_versions_and_native_routes_are_pinned():
    with pytest.raises(ValueError, match="version"):
        validate_auth_spec("codex", oauth_spec(), version="0.114.0")
    with pytest.raises(ValueError, match="provider"):
        validate_auth_spec("pi", oauth_spec(), model="openai/gpt-5")
    validate_auth_spec("pi", oauth_spec(), model="openai-codex/gpt-5")
