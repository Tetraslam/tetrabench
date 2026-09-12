"""Claude applied state, picker capability borrowing and restrictions."""

import json

import pytest
from native_consumer_support import VERSIONS, native_environment, native_modules

from tetrabench.canonical_json import sha256_hex
from tetrabench.harness_config import (
    HarnessConfig,
    NativeConfig,
    bind_capability_adoption,
)
from tetrabench.harnesses import seal_harness
from tetrabench.native_control import (
    ControlError,
    ControlProcess,
    claude_control_metadata,
    claude_model_metadata,
)
from tetrabench.native_discovery import collect_installed
from tetrabench.runtime_startup import startup_capability_probe

MODEL = "claude-opus-5[1m]"


def catalog(value="opus", resolved="claude-opus-5"):
    return {
        "value": value,
        "resolvedModel": resolved,
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
    }


def project(rows=None, *, requested=MODEL, applied=MODEL, effective=None, **data):
    return claude_model_metadata(
        {"models": [catalog()] if rows is None else rows, **data},
        {
            "applied": {"model": applied, "effort": "high"},
            "effective": {"effortLevel": "low", **(effective or {})},
        },
        requested=requested,
        version="2.1.267",
        environment={},
    )


def test_context_capability_borrowing_does_not_rewrite_applied_state():
    result = project()
    assert result["applied"] == {"model": MODEL, "effort": "high"}
    assert result["models"][0] == catalog()
    assert result["settings_effort"] == "low"
    assert result["selection_source"] == "context-modifier-capability-fallback"
    assert result["entitlement"] == "unknown"


def test_plain_model_can_borrow_a_context_variant_without_changing_identity():
    result = project(
        [catalog("opus[1m]", MODEL)], requested="claude-opus-5", applied="claude-opus-5"
    )
    assert result["applied"]["model"] == "claude-opus-5"
    assert result["models"][0]["resolvedModel"] == MODEL


def test_exact_selector_wins_but_does_not_bless_an_applied_model_change():
    exact = catalog(MODEL, MODEL)
    assert project([catalog(), exact])["models"] == [exact]
    with pytest.raises(ControlError, match="differs"):
        project([exact], applied="claude-sonnet-5")


@pytest.mark.parametrize("requested", ["opus[1m]", "default"])
def test_alias_resolution_uses_native_rows(requested):
    assert (
        project([catalog(requested)], requested=requested)["applied"]["model"] == MODEL
    )


def test_arbitrary_alias_is_not_resolved_using_an_unrelated_applied_row():
    with pytest.raises(ControlError, match="differs"):
        project(requested="unknown[1m]")


def test_conflicting_base_descriptors_are_not_borrowed():
    rows = [catalog(), {**catalog("default"), "supportedEffortLevels": ["low"]}]
    with pytest.raises(ControlError, match="conflicting"):
        project(rows)


@pytest.mark.parametrize("restriction", ["disabled", "unavailable"])
def test_explicit_native_unavailability_is_not_bypassed(restriction):
    rows = [catalog(), catalog(MODEL, MODEL)]
    data = {}
    if restriction == "disabled":
        rows[1]["disabled"] = True
    else:
        data["unavailable_models"] = [rows.pop()]
    with pytest.raises(ControlError, match="unavailable"):
        project(rows, **data)


def test_default_catalog_row_cannot_bypass_native_allowlist():
    with pytest.raises(ControlError, match="restricted"):
        project([catalog("default")], effective={"availableModels": ["sonnet"]})
    assert project(effective={"availableModels": ["opus"]})["restrictions"][
        "availableModels"
    ] == ["opus"]


def test_applied_state_is_required_and_version_gated():
    for version, settings in (
        ("2.1.266", {"applied": {"model": MODEL, "effort": "high"}, "effective": {}}),
        ("2.1.267", {"effective": {"model": MODEL, "effortLevel": "high"}}),
    ):
        with pytest.raises(ControlError):
            claude_model_metadata(
                {"models": [catalog()]},
                settings,
                requested=MODEL,
                version=version,
                environment={},
            )


@pytest.mark.native
@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("selector", [MODEL, "opus[1m]"])
def test_real_claude_applied_state_and_context_picker(tmp_path, disabled, selector):
    modules = native_modules(required=True)
    assert modules is not None
    env = native_environment(tmp_path)
    if disabled:
        env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1"
    settings = tmp_path / "settings.json"
    settings.write_text('{"effortLevel":"low","disableAllHooks":true}')
    command = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        str(modules / "@anthropic-ai/claude-code-linux-x64/claude"),
        "--print",
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--verbose",
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--no-session-persistence",
        "--settings",
        str(settings),
        "--model",
        selector,
        "--effort",
        "high",
    ]
    with ControlProcess(command, tmp_path, env) as process:
        data, effective = claude_control_metadata(process)
    assert data["account"]["tokenSource"] == "none"
    result = claude_model_metadata(
        data,
        effective,
        requested=selector,
        version=VERSIONS["claude-code"],
        environment=env,
    )
    assert result["applied"]["model"] == (
        selector if disabled and selector == "opus[1m]" else MODEL
    )
    assert result["applied"]["effort"] == "high"
    assert result["settings_effort"] == "low"
    assert result["restrictions"]["context_1m_disabled"] is disabled
    assert result["entitlement"] == "unknown"
    if disabled and selector == MODEL:
        assert all(not row["value"].endswith("[1m]") for row in data["models"])
        assert result["selection_source"] == "context-modifier-capability-fallback"
        assert result["models"][0]["resolvedModel"] == "claude-opus-5"


@pytest.mark.native
@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_real_claude_discovery_keeps_native_restrictions(
    tmp_path, monkeypatch, allowed, disabled
):
    from tetrabench import native_discovery

    isolate = native_discovery.isolated_auth_environment
    monkeypatch.setattr(
        native_discovery,
        "isolated_auth_environment",
        lambda *args, **kwargs: {
            **isolate(*args, **kwargs),
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1" if disabled else "0",
        },
    )
    modules = native_modules(required=True)
    assert modules is not None
    config = HarnessConfig(
        name="claude-code",
        version=VERSIONS["claude-code"],
        model="anthropic/" + MODEL,
        discovery="isolated",
        native_config=NativeConfig(
            text=json.dumps(
                {
                    "availableModels": ["opus"] if allowed else ["sonnet"],
                }
            )
        ),
    )
    snapshot = collect_installed(
        config, base=tmp_path, modules=modules, reuse_native_cache=False
    )
    if not allowed:
        assert snapshot.status == "unavailable", snapshot
        assert not snapshot.controls
        return
    assert snapshot.status == "supported", snapshot.limitations
    assert snapshot.identity.requested_model == "anthropic/" + MODEL
    assert snapshot.identity.resolved_model == MODEL
    metadata = json.loads(snapshot.metadata_json)
    assert metadata["applied"]["model"] == MODEL
    assert metadata["entitlement"] == "unknown"
    assert metadata["restrictions"]["context_1m_disabled"] is disabled

    # Exercise the actual shared startup consumer on the adopted snapshot, not
    # merely an in-memory projection of the discovery response.
    from test_startup_native_reasoning import run_probe

    bound = bind_capability_adoption(config, snapshot, control="effort", select="high")
    path = tmp_path / "startup-settings.json"
    text = json.dumps({"availableModels": ["opus"], "effortLevel": "low"})
    path.write_text(text)
    probe = startup_capability_probe(
        seal_harness(bound, tmp_path),
        native_files={str(path): sha256_hex(text.encode())},
        resource_root=str(tmp_path / "resources"),
        command=(str(modules / "@anthropic-ai/claude-code-linux-x64/claude"),),
        settings_file=str(path),
    )
    env = native_environment(
        tmp_path,
        {
            "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1" if disabled else "0",
        },
    )
    result = run_probe(tmp_path, probe, env)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["observed_selection"] == "high"
    assert report["native_settings_effort"] == "low"
    assert report["claude_metadata"]["applied"]["model"] == MODEL
    assert report["claude_metadata"]["entitlement"] == "unknown"
    assert report["inference_validated"] is False
    # A freshly hashed restricted config still cannot be blessed by the old
    # default row or the native applied state retained before an inference turn.
    path.write_text('{"availableModels":["sonnet"]}')
    probe["files"][0]["sha256"] = sha256_hex(path.read_bytes())
    result = run_probe(tmp_path, probe, env)
    assert result.returncode == 78, result.stdout
