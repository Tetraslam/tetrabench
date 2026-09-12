"""Codex route defaults require the exact pin, auth observation, and native route."""

import json

import pytest

from tetrabench.auth_config import AuthSpec, EnvAuthReference, NativeAuthReference
from tetrabench.capabilities import CapabilitySnapshot, MetadataError
from tetrabench.discovery import NATIVE_VERSIONS, discover, observation_from_json
from tetrabench.harness_config import HarnessConfig
from tetrabench.native_control import ControlError, ControlProcess, codex_route
from tetrabench.native_discovery import _identity
from tetrabench.reasoning import select_reasoning, validate_snapshot_for_harness


def resolve(
    config=None,
    *,
    version="0.154.0",
    requested_provider="openai",
    observed_auth_mode="api_key",
    environment=None,
):
    return codex_route(
        config or {},
        version=version,
        requested_provider=requested_provider,
        observed_auth_mode=observed_auth_mode,
        environment=environment or {},
    )


def harness():
    return HarnessConfig(
        name="codex",
        version="0.154.0",
        model="openai/gpt-6-astra",
        auth=AuthSpec(mode="api_key", reference=EnvAuthReference(name="TEST_KEY")),
    )


def snapshot(route):
    identity = _identity(harness(), route, observed=True)
    observation = observation_from_json(
        identity,
        json.dumps(
            {
                "data": [
                    {
                        "model": "gpt-6-astra",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                        "defaultReasoningEffort": "high",
                    }
                ],
                "nextCursor": None,
            }
        ),
        observed_at="2026-09-11T00:00:00Z",
        method="synthetic native metadata",
    )
    return discover(identity, observation=observation)


@pytest.mark.parametrize("provider", [None, "openai"])
def test_pinned_default_uses_verified_api_mode_and_retains_provenance(provider):
    route = resolve({"model_provider": provider})
    assert NATIVE_VERSIONS["codex"][0] == "0.154.0"
    assert (route["provider"], route["protocol"], route["endpoint"]) == (
        "openai",
        "responses",
        "https://api.openai.com/v1",
    )
    assert route["provenance"]["kind"] == "native-version-default"
    assert route["provenance"]["default_fields"] == (
        ["provider", "protocol", "endpoint"]
        if provider is None
        else ["protocol", "endpoint"]
    )
    assert all(
        "6b9826e3aa83b1a5947db50f4332cb9c65f1b340" in url
        for url in route["provenance"]["source_urls"]
    )
    captured = snapshot(route)
    assert captured.identity.auth_mode == "api_key"
    assert captured.identity.requested_model == "openai/gpt-6-astra"
    assert captured.identity.resolved_model == "gpt-6-astra"
    assert select_reasoning(
        captured, captured.identity, control="effort", select="high"
    )


@pytest.mark.parametrize(
    "options",
    [
        {"version": "0.154.1"},
        {"version": "0.153.0"},
        {"requested_provider": "custom"},
        {"observed_auth_mode": None},
        {"observed_auth_mode": "none"},
        {"observed_auth_mode": "chatgpt_oauth"},
        {"environment": {"OPENAI_BASE_URL": "https://example.test/v1"}},
    ],
)
def test_default_is_not_inferred_without_its_contract(options):
    route = resolve(**options)
    assert route["status"] == "unknown"
    assert route["provenance"]["default_fields"] == []


@pytest.mark.parametrize(
    "config",
    [
        {"model_provider": "custom"},
        {"model_provider": ""},
        {"openai_base_url": "https://example.test/v1"},
        {
            "model_provider": "openai",
            "model_providers": {
                "openai": {
                    "base_url": "https://example.test/v1",
                    "wire_api": "responses",
                }
            },
        },
        {"profile": "custom", "profiles": {"custom": {"model_provider": "custom"}}},
        {
            "model_provider": "custom",
            "model_providers": {"custom": {"wire_api": "responses"}},
        },
        {
            "model_provider": "custom",
            "model_providers": {"custom": {"base_url": "https://example.test/v1"}},
        },
    ],
)
def test_incomplete_or_changed_route_stays_unknown_and_strict_adoption_refuses(config):
    before = json.dumps(config, sort_keys=True)
    route = resolve(config)
    assert route["status"] == "unknown"
    captured = snapshot(route)
    assert captured.identity.route_status == "unknown"
    with pytest.raises(MetadataError, match="unavailable or unknown"):
        select_reasoning(captured, captured.identity, control="effort", select="high")
    assert json.dumps(config, sort_keys=True) == before


def test_custom_metadata_is_preserved_without_builtin_defaults():
    route = resolve(
        {
            "model_provider": "custom",
            "model_providers": {
                "custom": {
                    "wire_api": "custom-wire",
                    "base_url": "https://example.test/v1",
                }
            },
        }
    )
    assert route["status"] == "bound"
    assert route["protocol"] == "custom-wire"
    assert route["endpoint"] == "https://example.test/v1"
    assert route["provenance"]["kind"] == "native-config-read"
    assert route["provenance"]["default_fields"] == []


def test_unsafe_endpoint_cannot_bind_custom_codex_route():
    route = resolve(
        {
            "model_provider": "custom",
            "model_providers": {
                "custom": {
                    "wire_api": "responses",
                    "base_url": "https://example.test/v1?key=not-safe",
                }
            },
        }
    )
    assert _identity(harness(), route, observed=True).route_status == "unknown"


def test_auth_change_makes_snapshot_stale_without_opening_any_credential_store():
    captured = CapabilitySnapshot.from_bytes(snapshot(resolve()).to_bytes())
    changed = HarnessConfig.model_validate(
        harness().model_dump()
        | {
            "auth": AuthSpec(
                mode="chatgpt_oauth",
                reference=NativeAuthReference(
                    profile="test", binding="test", generation=1
                ),
            )
        }
    )
    with pytest.raises(MetadataError, match="does not bind"):
        validate_snapshot_for_harness(captured, changed)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"refreshToken": True},
        {"refreshToken": 0},
        {"refreshToken": False, "extra": True},
    ],
)
def test_metadata_account_read_cannot_refresh(params):
    process = object.__new__(ControlProcess)
    with pytest.raises(ControlError, match="disable token refresh"):
        process.rpc("account/read", params, 1)
