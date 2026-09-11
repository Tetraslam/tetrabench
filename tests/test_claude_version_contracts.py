"""Dated Claude contracts: immutable .267 records and exact .269 consumers."""

from __future__ import annotations

import json
from typing import Any

import pytest
from native_consumer_support import (
    VERSIONS,
    native_environment,
    native_modules,
    native_run,
)

from tetrabench.canonical_json import sha256_hex
from tetrabench.capabilities import (
    CapabilityIdentity,
    CapabilitySnapshot,
    MetadataError,
)
from tetrabench.discovery import discover, native_adapter_version, observation_from_json
from tetrabench.harness_config import (
    HarnessConfig,
    ResolvedHarness,
    bind_capability_adoption,
    capability_config,
)
from tetrabench.harnesses import seal_harness
from tetrabench.native_control import (
    ControlError,
    ControlProcess,
    claude_control_metadata,
    claude_model_metadata,
)
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.reasoning import adopt_harness, harness_config_digest

# Captured with the retained 2d1ca9a wheel, before the preferred-version change.
# References are synthetic; this fixture never resolves a credential or path.
HISTORICAL_HARNESS = (
    b'{"ancillary_models":"primary","auth":{"mode":"api_key","reference":'
    b'{"kind":"env","name":"HISTORICAL_TEST_KEY","schema_version":1},'
    b'"schema_version":1},"discovery":"isolated","env":{},'
    b'"model":"anthropic/claude-opus-5[1m]","name":"claude-code",'
    b'"native_config":null,"options":{"permission_mode":"manual",'
    b'"system_prompt_file":"/tmp/tetrabench-resources/instructions.md"},'
    b'"resources":[{"destination":"instructions.md","mode":420,'
    b'"sha256":"690e3fc4d0adb76b835f8104f41e3d05c664a6fca16bfa93b14a7ffdce2e7c77",'
    b'"text":"Preserve exact historical instructions.\\n"}],"version":"2.1.267"}'
)
HISTORICAL_HARNESS_SHA = (
    "bd8fc181f6e44d1867e0e45d8113ca18deefbdf05740ce3f402f0396d0cfad66"
)
HISTORICAL_SNAPSHOT_SHA = (
    "758e51b019c9390754569c9cdcdfd5afa1c30578569cb347228e5537485032df"
)
HISTORICAL_BOUND_SHA = (
    "ae57ba08a96d49e51ff5bd1e953aa30c6b32d9be0e06da406bdbbfa85311ac6c"
)


def historical_capture():
    resolved = parse_canonical_model(HISTORICAL_HARNESS, ResolvedHarness)
    identity = CapabilityIdentity(
        harness="claude-code",
        harness_version="2.1.267",
        native_adapter_version="0.3.267",
        requested_model=resolved.model,
        resolved_model="claude-opus-5[1m]",
        provider_id="anthropic",
        route_id="fixture",
        protocol="claude-native",
        endpoints=("https://api.anthropic.com/v1",),
        fallback_policy_json="{}",
        auth_mode="api_key",
        profile_ref="env:HISTORICAL_TEST_KEY",
        config_digest=harness_config_digest(resolved),
        route_status="bound",
    )
    payload = json.dumps(
        {
            "models": [
                {
                    "value": "claude-opus-5[1m]",
                    "resolvedModel": "claude-opus-5[1m]",
                    "supportsEffort": True,
                    "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
                    "supportsAdaptiveThinking": True,
                }
            ],
            "applied": {"model": "claude-opus-5[1m]", "effort": "high"},
            "settings_effort": None,
            "restrictions": {"availableModels": None, "context_1m_disabled": False},
        }
    )
    observation = observation_from_json(
        identity, payload, observed_at="2026-09-11T20:15:24Z"
    )
    return resolved, observation


def test_267_auth_resource_and_discovery_bytes_survive_preferred_upgrade(tmp_path):
    assert sha256_hex(HISTORICAL_HARNESS) == HISTORICAL_HARNESS_SHA
    resolved, observation = historical_capture()
    assert canonical_model_bytes(resolved) == HISTORICAL_HARNESS
    snapshot = discover(observation.identity, observation=observation)
    assert snapshot.status == "supported"
    assert snapshot.digest == HISTORICAL_SNAPSHOT_SHA
    data = canonical_model_bytes(snapshot)
    assert (
        canonical_model_bytes(parse_canonical_model(data, CapabilitySnapshot)) == data
    )
    bound = seal_harness(
        bind_capability_adoption(
            capability_config(resolved), snapshot, control="effort", select="xhigh"
        ),
        tmp_path,
    )
    assert sha256_hex(canonical_model_bytes(bound)) == HISTORICAL_BOUND_SHA
    assert bound.version == "2.1.267"
    assert bound.capability_snapshot is not None
    assert (
        bound.capability_snapshot.snapshot().identity.native_adapter_version
        == "0.3.267"
    )


def test_old_snapshot_cannot_authorize_new_cli_version():
    resolved, observation = historical_capture()
    snapshot = discover(observation.identity, observation=observation)
    new_config = HarnessConfig.model_validate(
        capability_config(resolved).model_dump() | {"version": "2.1.269"}
    )
    with pytest.raises(MetadataError, match="drift"):
        adopt_harness(
            new_config, snapshot, snapshot.identity, control="effort", select="high"
        )
    new_identity = snapshot.identity.model_copy(
        update={
            "harness_version": "2.1.269",
            "native_adapter_version": "0.3.269",
        }
    )
    with pytest.raises(MetadataError):
        discover(new_identity, cached=snapshot)


@pytest.mark.parametrize("cli,sdk", [("2.1.267", "0.3.267"), ("2.1.269", "0.3.269")])
def test_native_contract_pairs_preserve_capture_provenance(cli, sdk):
    _, observation = historical_capture()
    identity = observation.identity.model_copy(
        update={
            "harness_version": cli,
            "native_adapter_version": sdk,
        }
    )
    assert native_adapter_version("claude-code", cli) == sdk
    capture = observation_from_json(
        identity, observation.payload_json, observed_at="2026-09-11T20:15:24Z"
    )
    assert f"@{sdk}/sdk.d.ts" in capture.evidence.source_url
    assert discover(identity, observation=capture).status == "supported"


@pytest.mark.parametrize(
    "cli,sdk",
    [
        ("2.1.267", "0.3.269"),
        ("2.1.269", "0.3.267"),
        ("2.1.268", "0.3.268"),
        ("2.1.270", "0.3.270"),
    ],
)
def test_unverified_or_crossed_native_contract_pairs_are_unavailable(cli, sdk):
    _, observation = historical_capture()
    identity = observation.identity.model_copy(
        update={
            "harness_version": cli,
            "native_adapter_version": sdk,
        }
    )
    assert discover(identity, observation=observation).status == "unavailable"


@pytest.mark.native
@pytest.mark.parametrize("requested", ["2.1.267", "2.1.269"])
def test_real_startup_requires_exact_requested_claude_binary(tmp_path, requested):
    from tetrabench.runtime_metadata import Drift, version

    modules = native_modules(required=True)
    assert modules is not None
    command = [str(modules / "@anthropic-ai/claude-code-linux-x64/claude")]
    if requested == VERSIONS["claude-code"]:
        version(command, tmp_path, native_environment(tmp_path), requested)
    else:
        with pytest.raises(Drift, match="version drift"):
            version(command, tmp_path, native_environment(tmp_path), requested)


@pytest.mark.native
@pytest.mark.parametrize("per_model", [False, True])
def test_269_native_caps_are_visible_in_applied_settings(tmp_path, per_model):
    modules = native_modules(required=True)
    assert modules is not None
    cap: dict[str, Any] = {"maxEffortLevel": "low"}
    if per_model:
        cap = {"modelSettings": {"claude-opus-5": cap}}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"disableAllHooks": True, **cap}))
    env = native_environment(tmp_path)
    command = [
        str(modules / "@anthropic-ai/claude-code-linux-x64/claude"),
        "--print",
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--verbose",
        "--no-session-persistence",
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        str(settings),
        "--model",
        "claude-opus-5",
        "--effort",
        "high",
    ]
    with ControlProcess(command, tmp_path, env) as process:
        data, applied = claude_control_metadata(process)
    metadata = claude_model_metadata(
        data,
        applied,
        requested="claude-opus-5",
        version=VERSIONS["claude-code"],
        environment=env,
    )
    assert metadata["applied"]["effort"] == "low"
    assert metadata["entitlement"] == "unknown"
    with pytest.raises(ControlError, match="unsupported"):
        claude_model_metadata(
            data, applied, requested="claude-opus-5", version="2.1.268", environment=env
        )


@pytest.mark.native
def test_269_new_status_path_is_not_exposed_in_projected_status(tmp_path):
    from tetrabench.nativeauth import NativeResult, parse_native_status

    modules = native_modules(required=True)
    assert modules is not None
    result = native_run(
        [
            str(modules / "@anthropic-ai/claude-code-linux-x64/claude"),
            "auth",
            "status",
            "--json",
        ],
        tmp_path,
    )
    raw = json.loads(result.stdout)
    assert raw["loggedIn"] is False
    assert raw["configDirectory"] == str(tmp_path / "native-home/claude")
    native = NativeResult(
        result.returncode,
        result.stdout.encode(),
        stdout=result.stdout.encode(),
        stderr=result.stderr.encode(),
    )
    status = parse_native_status("claude-code", native)
    assert status.mode == "none"
    assert str(tmp_path) not in repr(status)
