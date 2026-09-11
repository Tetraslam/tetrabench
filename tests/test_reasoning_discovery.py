"""Offline schema-shaped native captures, not live provider acceptance tests."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from tetrabench.canonical_json import loads_canonical_json
from tetrabench.capabilities import (
    MAX_METADATA_BYTES,
    Binding,
    Budget,
    CapabilityIdentity,
    CapabilitySnapshot,
    Choice,
    Control,
    MetadataError,
    Setting,
    make_evidence,
    metadata_text,
    parse_metadata,
)
from tetrabench.discovery import (
    NATIVE_VERSIONS,
    attach_catalog,
    catalog_controls,
    collect_codex_pages,
    discover,
    observation_from_json,
    pi_clamp_thinking_level,
    pi_supported_thinking_levels,
)
from tetrabench.discovery_http import refresh_public_metadata
from tetrabench.harness_config import HarnessConfig
from tetrabench.reasoning import (
    adopt_handler,
    adopt_harness,
    harness_config_digest,
    inspect_handler,
    preview_adoption,
    select_reasoning,
    suggested_snippets,
)

DATE = "2026-09-10T18:00:00Z"
URL = "https://metadata.example/models"
EVIDENCE = make_evidence(URL, "{}", DATE, "schema-shaped offline fixture")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in reasoning tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)


def config_for(harness="codex", **updates):
    return HarnessConfig(
        name=harness,
        version=NATIVE_VERSIONS[harness][0],
        model="route/model",
        **updates,
    )


def identity_for(harness="codex", config=None, **updates):
    config = config or config_for(harness)
    data: dict[str, Any] = dict(
        harness=harness,
        harness_version=config.version,
        native_adapter_version=NATIVE_VERSIONS[harness][1],
        requested_model=config.model,
        resolved_model="model",
        provider_id="route",
        route_id="explicit-route",
        protocol="native-protocol",
        endpoints=(URL,),
        fallback_policy_json='{"allow_fallbacks": false}',
        auth_mode="api-key",
        profile_ref="test-profile",
        config_digest=harness_config_digest(config),
        route_status="bound",
    )
    data.update(updates)
    return CapabilityIdentity(**data)


def capture(identity, payload):
    return observation_from_json(identity, metadata_text(payload), observed_at=DATE)


def codex_payload(efforts=("low", "medium", "high")):
    # rust-v0.154.0 generated v2/{Model,ModelListResponse}.ts.
    return {
        "data": [
            {
                "id": "model-menu-id",
                "model": "model",
                "displayName": "Test model",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": effort, "description": effort}
                    for effort in efforts
                ],
                "defaultReasoningEffort": "medium",
                "hidden": False,
            }
        ],
        "nextCursor": None,
    }


def codex_snapshot(identity=None):
    identity = identity or identity_for()
    return discover(identity, observation=capture(identity, codex_payload()))


def pi_payload(**changes):
    # v0.85.1 packages/ai/src/types.ts Model + models.ts pure thinking methods.
    model = dict(
        id="model",
        provider="route",
        api="native-protocol",
        baseUrl=URL,
        reasoning=True,
        thinkingLevelMap={"minimal": None, "xhigh": "extreme"},
        cost={"input": 0.001, "output": 0.002},
        maxTokens=8192,
    )
    model.update(changes)
    return {"model": model}


def opencode_payload(variants=None):
    # v1.18.30 provider.ts Model/ListResult, after merge and disabled filtering.
    return {
        "all": [
            {
                "id": "route",
                "key": "DO-NOT-RETAIN",
                "options": {"apiKey": "hidden"},
                "models": {
                    "model": {
                        "id": "model",
                        "providerID": "route",
                        "api": {"id": "model", "url": URL, "npm": "native-protocol"},
                        "capabilities": {"reasoning": True},
                        "variants": variants
                        if variants is not None
                        else {
                            "deliberate": {
                                "reasoningEffort": "high",
                                "temperature": 0.2,
                            },
                            "budgeted": {
                                "thinking": {"type": "enabled", "budgetTokens": 2048}
                            },
                        },
                    }
                },
            }
        ],
        "default": {"route": "model"},
        "connected": ["route"],
    }


def test_offline_discovery_does_not_call_reader_or_auth(monkeypatch):
    monkeypatch.setenv("API_KEY_HELPER", "must-not-run")
    calls = []
    identity = identity_for()

    def reader(ident):
        return calls.append(ident)

    result = discover(identity, native_reader=reader)
    assert result.status == "unavailable"
    assert not calls
    assert "Install" in result.limitations[0]
    assert (
        discover(identity, refresh=True, native_reader=reader).status == "unavailable"
    )
    assert not calls


def test_explicit_native_metadata_read_and_no_error_leak():
    identity = identity_for()
    calls = []

    def reader(ident):
        calls.append(ident)
        return capture(ident, codex_payload())

    snapshot = discover(
        identity, refresh=True, allow_authenticated_read=True, native_reader=reader
    )
    assert calls == [identity]
    assert snapshot.controls[0].choices[0].name == "low"

    def broken(_):
        raise RuntimeError("Authorization: Bearer SECRET")

    with pytest.raises(MetadataError, match="native metadata read failed") as error:
        discover(
            identity, refresh=True, allow_authenticated_read=True, native_reader=broken
        )
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("harness_version", "0.155.0"),
        ("native_adapter_version", "0.155.0"),
        ("resolved_model", "another"),
        ("route_id", "alternate"),
        ("protocol", "different"),
        ("endpoints", ("https://other.example/v1",)),
        ("fallback_policy_json", '{"allow_fallbacks":true}'),
        ("auth_mode", "subscription"),
        ("profile_ref", "different-profile"),
        ("config_digest", "a" * 64),
    ],
)
def test_cached_snapshot_drift_requires_revalidation(field, value):
    old = codex_snapshot()
    changed = old.identity.model_copy(update={field: value})
    for refresh in (False, True):
        with pytest.raises(MetadataError, match="identity drift"):
            discover(changed, cached=old, refresh=refresh)


def test_snapshot_reuse_is_exact_and_does_not_refresh():
    snapshot = codex_snapshot()
    assert discover(snapshot.identity, cached=snapshot) is snapshot
    roundtrip = CapabilitySnapshot.model_validate_json(snapshot.to_bytes())
    assert roundtrip == snapshot
    assert roundtrip.digest == snapshot.digest
    parsed = loads_canonical_json(snapshot.to_bytes())
    assert isinstance(parsed, dict)
    assert isinstance(parsed["identity"], dict)
    assert parsed["identity"]["config_digest"]


def test_opencode_effective_variants_not_effort_names_and_secret_projection():
    identity = identity_for("opencode")
    snapshot = discover(identity, observation=capture(identity, opencode_payload()))
    settings = select_reasoning(
        snapshot, identity, control="variant", select="deliberate"
    )
    assert parse_metadata(settings[0].value_json) == "deliberate"
    deliberate = next(c for c in snapshot.controls[0].choices if c.name == "deliberate")
    assert deliberate.native_value_json == metadata_text(
        {
            "reasoningEffort": "high",
            "temperature": 0.2,
        }
    )
    assert "DO-NOT-RETAIN" not in snapshot.to_bytes().decode()
    assert "apiKey" not in snapshot.to_bytes().decode()
    assert loads_canonical_json(
        snapshot.to_bytes()
    )  # Native floats are inside JSON text.
    adopted = adopt_harness(
        config_for("opencode"),
        snapshot,
        identity,
        control="variant",
        select="deliberate",
    )
    assert adopted.options["variant"] == "deliberate"


def test_opencode_config_override_and_disabled_marker():
    identity = identity_for("opencode")
    payload = opencode_payload({"custom": {"thinking": {"budgetTokens": 3072}}})
    snapshot = discover(identity, observation=capture(identity, payload))
    assert [choice.name for choice in snapshot.controls[0].choices] == ["custom"]
    with pytest.raises(MetadataError, match="absent"):
        select_reasoning(snapshot, identity, control="variant", select="high")
    payload = opencode_payload({"removed": {"disabled": True}})
    with pytest.raises(MetadataError, match="merged native variants"):
        discover(identity, observation=capture(identity, payload))


@pytest.mark.parametrize("change", [{"apiKey": "hidden"}, {"headers": {"x": "secret"}}])
def test_credential_bearing_variant_never_serialized(change):
    identity = identity_for("opencode")
    with pytest.raises(ValueError, match="snapshot-safe"):
        discover(
            identity,
            observation=capture(identity, opencode_payload({"custom": change})),
        )


def test_pi_native_null_map_missing_map_and_clamp_up_and_down():
    model = pi_payload()["model"]
    assert pi_supported_thinking_levels(model) == (
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert pi_clamp_thinking_level(model, "minimal") == "low"
    assert pi_clamp_thinking_level(model, "max") == "xhigh"
    assert pi_clamp_thinking_level(model, "nonsense") == "off"
    assert pi_supported_thinking_levels({"reasoning": False}) == ("off",)
    assert "high" in pi_supported_thinking_levels({"reasoning": True})
    assert "xhigh" not in pi_supported_thinking_levels({"reasoning": True})


def test_pi_sdk_preferred_and_clamping_requires_explicit_acceptance():
    identity = identity_for("pi")
    payload = pi_payload()
    payload["supportedThinkingLevels"] = list(
        pi_supported_thinking_levels(payload["model"])
    )
    snapshot = discover(identity, observation=capture(identity, payload))
    assert snapshot.evidence[0].kind == "explicit"
    with pytest.raises(MetadataError, match="clamps"):
        select_reasoning(snapshot, identity, control="thinking", select="minimal")
    settings = select_reasoning(
        snapshot,
        identity,
        control="thinking",
        select="minimal",
        accept_normalization=True,
    )
    assert parse_metadata(settings[0].value_json) == "low"
    config = adopt_harness(
        config_for("pi"), snapshot, identity, control="thinking", select="xhigh"
    )
    assert config.options["thinking"] == "xhigh"  # Not wire value "extreme".
    payload["supportedThinkingLevels"] = ["off", "high"]
    with pytest.raises(MetadataError, match="disagree"):
        discover(identity, observation=capture(identity, payload))


def test_pi_fallback_is_labeled_and_no_reasoning_model_has_only_off_unclamped():
    identity = identity_for("pi")
    snapshot = discover(
        identity, observation=capture(identity, pi_payload(reasoning=False))
    )
    assert snapshot.evidence[0].kind == "native-heuristic"
    choices = snapshot.controls[0].choices
    assert [c.name for c in choices if c.normalization == "identity"] == ["off"]
    assert select_reasoning(snapshot, identity, control="thinking", select="off")
    with pytest.raises(MetadataError, match="clamps"):
        select_reasoning(snapshot, identity, control="thinking", select="high")


def test_codex_pagination_native_method_and_cursor():
    calls = []

    def request(method, params):
        calls.append((method, params))
        page = codex_payload()
        if params["cursor"] is None:
            page["data"] = []
            page["nextCursor"] = "page-two"
        return json.dumps(page)

    payload = collect_codex_pages(request, refresh=True, allow_authenticated_read=True)
    assert [c[1]["cursor"] for c in calls] == [None, "page-two"]
    assert all(c[0] == "model/list" for c in calls)
    identity = identity_for()
    snapshot = discover(
        identity, observation=observation_from_json(identity, payload, observed_at=DATE)
    )
    assert snapshot.controls[0].default_json == '"medium"'
    assert len(snapshot.controls[0].choices) == 3


def test_codex_pagination_rejects_repeats_truncation_and_no_opt_in():
    def request(*_):
        return '{"data":[],"nextCursor":"again"}'

    with pytest.raises(MetadataError, match="opt-in"):
        collect_codex_pages(request)
    with pytest.raises(MetadataError, match="repeated"):
        collect_codex_pages(request, refresh=True, allow_authenticated_read=True)
    identity = identity_for()
    with pytest.raises(MetadataError, match="incomplete"):
        discover(
            identity,
            observation=capture(identity, {"data": [], "nextCursor": "remaining"}),
        )


def test_claude_resolved_alias_effort_and_adaptive_independent():
    # sdk.d.ts ModelInfo, 0.3.267 with package claudeCodeVersion 2.1.267.
    config = HarnessConfig(name="claude-code", version="2.1.267", model="route/alias")
    identity = identity_for("claude-code", config)
    payload = [
        {
            "value": "alias",
            "resolvedModel": "model",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "high", "max"],
            "supportsAdaptiveThinking": True,
        }
    ]
    snapshot = discover(identity, observation=capture(identity, payload))
    assert snapshot.controls[1].kind == "adaptive"
    assert snapshot.controls[1].choices == ()
    updated = adopt_harness(config, snapshot, identity, control="effort", select="max")
    assert updated.options["reasoning_effort"] == "max"
    with pytest.raises(MetadataError, match="absent"):
        select_reasoning(snapshot, identity, control="effort", select="xhigh")
    payload[0].pop("resolvedModel")
    with pytest.raises(MetadataError, match="did not resolve"):
        discover(identity, observation=capture(identity, payload))


@pytest.mark.parametrize(
    "presence,status",
    [("omitted", "unknown"), ("null", "supported"), ("present", "supported")],
)
def test_openrouter_missing_null_empty_allowlists_distinct(presence, status):
    row: dict[str, Any] = {"reasoning": {"mandatory": False}}
    if presence != "omitted":
        row["reasoning"] = {
            "mandatory": False,
            "supported_efforts": None if presence == "null" else [],
        }
    control = catalog_controls(row, EVIDENCE, schema="openrouter")[0]
    assert control.choices_presence == presence
    assert control.status == status
    assert control.choices == ()  # Null does not invent a gateway or endpoint enum.


def test_modelsdev_budget_toggle_nullable_effort_no_provider_mapping():
    row: dict[str, Any] = {
        "reasoning_options": [
            {"type": "budget_tokens", "min": 1024.0, "max": 8192},
            {"type": "toggle"},
            {"type": "effort", "values": [None, "deliberate"]},
        ]
    }
    controls = catalog_controls(row, EVIDENCE, schema="models.dev")
    assert controls[0].budget is not None
    assert controls[0].budget.minimum == 1024
    assert controls[0].budget.binding is None
    assert controls[1].kind == "off"
    assert controls[2].choices[0].native_value_json == "null"
    assert controls[2].choices[1].settings == ()
    row["reasoning_options"] = [{"type": "budget_tokens", "min": 10.5}]
    with pytest.raises(MetadataError, match="integer"):
        catalog_controls(row, EVIDENCE, schema="models.dev")


def test_openrouter_visibility_and_mandatory_not_effort_inference():
    controls = catalog_controls(
        {
            "reasoning": {
                "mandatory": True,
                "supported_efforts": ["high"],
                "supports_max_tokens": True,
            },
            "supported_parameters": ["reasoning", "include_reasoning"],
        },
        EVIDENCE,
        schema="openrouter",
    )
    assert controls[1].kind == "off" and controls[1].status == "unsupported"
    assert controls[2].budget is not None
    assert controls[2].budget.minimum is None
    assert controls[3].kind == "visibility" and controls[3].status == "supported"


def test_multi_route_unknown_and_catalog_refresh_no_replacement():
    identity = identity_for(route_status="multiple")
    snapshot = codex_snapshot(identity)
    assert snapshot.status == "unknown"
    with pytest.raises(MetadataError, match="unknown"):
        select_reasoning(snapshot, identity, control="effort", select="high")
    with pytest.raises(MetadataError, match="bind the route"):
        attach_catalog(
            snapshot,
            '{"id":"model"}',
            evidence=EVIDENCE,
            schema="openrouter",
            refresh=True,
        )
    snapshot = codex_snapshot()
    with pytest.raises(MetadataError, match="explicit refresh"):
        attach_catalog(
            snapshot, '{"id":"model"}', evidence=EVIDENCE, schema="models.dev"
        )
    enriched = attach_catalog(
        snapshot,
        '{"id":"model","reasoning_options":[]}',
        evidence=EVIDENCE,
        schema="models.dev",
        refresh=True,
    )
    assert len(snapshot.evidence) == 1 and len(enriched.evidence) == 2


@pytest.mark.parametrize(
    "data",
    [
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1e9999}',
        "{",
        "[" * 40 + "0" + "]" * 40,
        b"\xff",
        '"' + "a" * MAX_METADATA_BYTES + '"',
    ],
)
def test_malformed_and_bounded_metadata(data):
    with pytest.raises(MetadataError):
        parse_metadata(data)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@metadata.example",
        "https://metadata.example/?key=hidden",
        "file:///tmp/a",
        "https://metadata.example/#secret",
        "https://metadata.example/ bad",
    ],
)
def test_endpoint_identity_rejects_credentials_and_ambiguous_urls(url):
    with pytest.raises(ValueError):
        identity_for(endpoints=(url,))


def test_public_metadata_read_explicit_refresh_only(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, timeout):
            calls.append((host, timeout))

        def request(self, *args, **kwargs):
            calls.append((args, kwargs))

        def getresponse(self):
            return self

        status = 200

        def read(self, size):
            assert size == MAX_METADATA_BYTES + 1
            return b'{"data":[]}'

        def close(self):
            calls.append("closed")

        def getheader(self, _name):
            return None

    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    with pytest.raises(MetadataError, match="explicit refresh"):
        refresh_public_metadata("https://openrouter.ai/api/v1/models")
    assert not calls
    assert refresh_public_metadata("https://openrouter.ai/api/v1/models", refresh=True)
    assert calls[1] == (
        ("GET", "/api/v1/models"),
        {"headers": {"Accept": "application/json"}},
    )
    with pytest.raises(MetadataError, match="allowed"):
        refresh_public_metadata(
            "https://openrouter.ai/api/v1/chat/completions", refresh=True
        )


def budget_snapshot(**changes):
    budget = Budget(
        minimum=1024,
        maximum=8192,
        output_limit=4096,
        less_than_output=True,
        binding=Binding(surface="options", path=("max_thinking_tokens",)),
        **changes,
    )
    identity = identity_for("claude-code")
    control = Control(
        name="budget",
        kind="budget",
        status="supported",
        budget=budget,
        evidence=(EVIDENCE,),
    )
    return CapabilitySnapshot(
        identity=identity, status="supported", controls=(control,)
    )


@pytest.mark.parametrize("budget", [True, -1, 0, 1023, 4096, 8193, 1.5])
def test_budget_bounds_and_output_dependency_fail_closed(budget):
    snapshot = budget_snapshot()
    with pytest.raises(MetadataError):
        select_reasoning(snapshot, snapshot.identity, control="budget", budget=budget)


def test_budget_actual_config_and_unknown_bounds():
    snapshot = budget_snapshot()
    adopted = adopt_harness(
        config_for("claude-code"),
        snapshot,
        snapshot.identity,
        control="budget",
        budget=2048,
    )
    assert adopted.options["max_thinking_tokens"] == 2048
    unknown = snapshot.controls[0].model_copy(update={"budget": Budget()})
    snapshot = snapshot.model_copy(update={"controls": (unknown,)})
    with pytest.raises(MetadataError, match="unknown"):
        select_reasoning(snapshot, snapshot.identity, control="budget", budget=2048)


def document_for(config):
    return (
        "# keep my comment\n[harness]\n"
        f'name = "{config.name}"\nversion = "{config.version}"\n'
        f'model = "{config.model}"\n'
        '[harness.options]\nreasoning_effort = "low" # preserve comment\n'
    )


def test_exact_preview_write_opt_in_and_stale_config_refusal(tmp_path):
    config = config_for(options={"reasoning_effort": "low"})
    identity = identity_for(config=config)
    snapshot = codex_snapshot(identity)
    document = document_for(config)
    path = tmp_path / "run.toml"
    path.write_text(document)
    preview = adopt_handler(path, snapshot, identity, control="effort", select="high")
    assert not preview["written"]
    assert path.read_text() == document
    assert '+reasoning_effort = "high" # preserve comment' in preview["diff"]
    assert "# keep my comment" in preview["document"]
    assert not preview["inference_validated"]
    written = adopt_handler(
        path, snapshot, identity, control="effort", select="high", write=True
    )
    assert written["written"]
    assert path.read_text() == preview["document"]
    assert written["reinspect_required"]
    with pytest.raises(MetadataError, match="drift"):
        adopt_handler(
            path, snapshot, identity, control="effort", select="medium", write=True
        )


def test_adoption_detects_concurrent_write(tmp_path, monkeypatch):
    config = config_for(options={"reasoning_effort": "low"})
    identity = identity_for(config=config)
    path = tmp_path / "run.toml"
    path.write_text(document_for(config))
    real_fsync = __import__("os").fsync

    def race(fd):
        real_fsync(fd)
        path.write_text("# concurrent user edit\n")

    monkeypatch.setattr("os.fsync", race)
    with pytest.raises(MetadataError, match="changed during adoption"):
        adopt_handler(
            path,
            codex_snapshot(identity),
            identity,
            control="effort",
            select="high",
            write=True,
        )
    assert path.read_text() == "# concurrent user edit\n"
    assert len(list(tmp_path.iterdir())) == 1


def test_user_assertion_unknown_normalization_and_unknown_control_refused():
    snapshot = codex_snapshot()
    control = snapshot.controls[0]
    for replacement in (
        control.model_copy(update={"status": "unknown"}),
        control.model_copy(
            update={
                "evidence": (EVIDENCE.model_copy(update={"kind": "user-assertion"}),)
            }
        ),
        control.model_copy(update={"choices": (Choice(name="high"),)}),
    ):
        changed = snapshot.model_copy(update={"controls": (replacement,)})
        with pytest.raises(MetadataError):
            select_reasoning(
                changed, snapshot.identity, control="effort", select="high"
            )


def test_inspect_handler_suggests_real_options_and_never_claims_inference():
    identity = identity_for()
    report = inspect_handler(identity, observation=capture(identity, codex_payload()))
    assert report["inference_validated"] is False
    snippets = report["suggestions"]
    assert 'reasoning_effort = "high"' in snippets[2]["config_snippet"]
    assert not any(s["requires_normalization_acceptance"] for s in snippets)


def test_native_setting_text_adoption_and_no_unrelated_changes():
    config = config_for()
    identity = identity_for(config=config)
    # Codex native config key, from its actual config schema. No provider mapping.
    setting = Setting(
        binding=Binding(surface="native-toml", path=("model_reasoning_effort",)),
        value_json='"high"',
    )
    control = Control(
        name="native-effort",
        kind="choices",
        status="supported",
        evidence=(EVIDENCE,),
        choices=(Choice(name="high", settings=(setting,), normalization="identity"),),
    )
    snapshot = CapabilitySnapshot(
        identity=identity, status="supported", controls=(control,)
    )
    adopted = adopt_harness(
        config, snapshot, identity, control="native-effort", select="high"
    )
    assert adopted.native_config is not None
    assert adopted.native_config.text is not None
    assert adopted.native_config.format == "toml"
    assert 'model_reasoning_effort = "high"' in adopted.native_config.text
    assert adopted.model == config.model


def test_default_removes_explicit_option():
    config = config_for(options={"reasoning_effort": "low"})
    identity = identity_for(config=config)
    snapshot = codex_snapshot(identity)
    adopted = adopt_harness(
        config, snapshot, identity, control="default", select="default"
    )
    assert adopted.options == {}
    result = preview_adoption(
        document_for(config), snapshot, identity, control="default", select="default"
    )
    assert "reasoning_effort" not in result["document"]
    assert suggested_snippets(snapshot)[-1]["select"] == "default"


def test_recorded_public_metadata_non_reasoning_is_not_fabricated():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "reasoning_public.json").read_text()
    )
    controls = catalog_controls(fixture["model"], EVIDENCE, schema="openrouter")
    assert controls[0].status == "unknown"  # Omission can also mean a dynamic router.
    assert controls[0].choices == ()


def test_budget_dependencies_write_together():
    dependency = Setting(
        binding=Binding(surface="options", path=("disable_adaptive_thinking",)),
        value_json="true",
    )
    snapshot = budget_snapshot(requires=(dependency,))
    updated = adopt_harness(
        config_for("claude-code"),
        snapshot,
        snapshot.identity,
        control="budget",
        budget=2048,
    )
    assert updated.options == {
        "max_thinking_tokens": 2048,
        "disable_adaptive_thinking": True,
    }


def test_explicit_catalog_is_primary_for_a_known_native_mapping():
    snapshot = codex_snapshot()
    row = '{"id":"model","reasoning":{"mandatory":false,"supported_efforts":["low"]}}'
    enriched = attach_catalog(
        snapshot, row, evidence=EVIDENCE, schema="openrouter", refresh=True
    )
    with pytest.raises(MetadataError, match="unsupported by the refreshed catalog"):
        select_reasoning(enriched, enriched.identity, control="effort", select="high")
    assert select_reasoning(enriched, enriched.identity, control="effort", select="low")
    assert select_reasoning(
        snapshot, snapshot.identity, control="effort", select="high"
    )


def test_catalog_never_guesses_wire_effort_from_variant_names():
    identity = identity_for("opencode")
    native = discover(identity, observation=capture(identity, opencode_payload()))
    row = '{"id":"model","reasoning":{"mandatory":false,"supported_efforts":["high"]}}'
    enriched = attach_catalog(
        native, row, evidence=EVIDENCE, schema="openrouter", refresh=True
    )
    with pytest.raises(MetadataError, match="mapping unknown"):
        select_reasoning(enriched, identity, control="variant", select="deliberate")


def test_null_in_gateway_effort_array_preserved():
    controls = catalog_controls(
        {"reasoning": {"mandatory": False, "supported_efforts": [None, "high"]}},
        EVIDENCE,
        schema="openrouter",
    )
    assert controls[0].choices[0].native_value_json == "null"


def test_unknown_native_metadata_is_not_supported_by_default_control():
    identity = identity_for()
    payload = codex_payload()
    payload["data"][0].pop("supportedReasoningEfforts")
    snapshot = discover(identity, observation=capture(identity, payload))
    assert snapshot.status == "unknown"
    with pytest.raises(MetadataError, match="unknown"):
        select_reasoning(snapshot, identity, control="effort", select="high")


def test_unverified_version_and_adapter_return_unavailable_without_reader():
    identity = identity_for(native_adapter_version="0.155.0")

    def reader(_):
        pytest.fail("must not call an unverified native adapter")

    assert (
        discover(
            identity, refresh=True, allow_authenticated_read=True, native_reader=reader
        ).status
        == "unavailable"
    )


def test_native_route_change_rejected_even_if_model_id_matches():
    identity = identity_for("pi")
    with pytest.raises(MetadataError, match="route differs"):
        discover(
            identity,
            observation=capture(
                identity, pi_payload(baseUrl="https://changed.example")
            ),
        )


def test_snapshot_size_and_immutability():
    snapshot = codex_snapshot()
    with pytest.raises(ValueError):
        snapshot.status = "unknown"
    with pytest.raises(ValueError):
        CapabilitySnapshot(
            identity=snapshot.identity,
            status="unknown",
            limitations=("a" * (128 * 1024),),
        )


def test_native_reader_error_input_not_leaked():
    identity = identity_for("opencode")
    payload = opencode_payload({"invalid\nchoice": {"reasoningEffort": "ok"}})
    with pytest.raises(MetadataError) as error:
        discover(identity, observation=capture(identity, payload))
    assert "invalid\nchoice" not in str(error.value)


def test_same_identity_new_catalog_requires_explicit_refresh():
    snapshot = codex_snapshot()
    identity = snapshot.identity
    observation = capture(identity, codex_payload(("low",)))
    with pytest.raises(MetadataError, match="explicit refresh"):
        discover(identity, cached=snapshot, observation=observation)
    changed = discover(identity, cached=snapshot, observation=observation, refresh=True)
    assert len(changed.controls[0].choices) == 1
    assert len(snapshot.controls[0].choices) == 3


def test_all_pi_levels_disabled_cannot_adopt_native_fallback():
    identity = identity_for("pi")
    levels = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
    snapshot = discover(
        identity,
        observation=capture(
            identity, pi_payload(thinkingLevelMap=dict.fromkeys(levels))
        ),
    )
    with pytest.raises(MetadataError, match="unknown"):
        select_reasoning(snapshot, identity, control="thinking", select="off")


def test_recorded_public_reasoning_metadata():
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "reasoning_openrouter_gpt5.json"
        ).read_text()
    )
    controls = catalog_controls(fixture["model"], EVIDENCE, schema="openrouter")
    assert [c.name for c in controls[0].choices] == ["high", "medium", "low", "minimal"]
    assert controls[0].default_json == '"medium"'
    assert controls[1].status == "unsupported"


def test_canonical_snapshot_load_rejects_noncanonical_input():
    snapshot = codex_snapshot()
    assert CapabilitySnapshot.from_bytes(snapshot.to_bytes()) == snapshot
    with pytest.raises(ValueError):
        CapabilitySnapshot.from_bytes(snapshot.to_bytes() + b"\n")


def test_reference_integrity_and_no_digest_cycle():
    from tetrabench.capabilities import CapabilitySnapshotRef
    from tetrabench.reasoning import validate_snapshot_for_harness

    class BoundHarness(HarnessConfig):
        capability_snapshot: CapabilitySnapshotRef | None = None

    snapshot = codex_snapshot()
    reference = CapabilitySnapshotRef.from_snapshot(snapshot)
    config = BoundHarness(**config_for().model_dump(), capability_snapshot=reference)
    assert harness_config_digest(config) == snapshot.identity.config_digest
    assert validate_snapshot_for_harness(reference, config) == snapshot
    with pytest.raises(ValueError, match="digest mismatch"):
        CapabilitySnapshotRef(sha256="0" * 64, snapshot_json=reference.snapshot_json)


def test_after_adoption_reference_binds_only_proven_diff():
    from tetrabench.reasoning import (
        rebind_after_adoption,
        validate_snapshot_for_harness,
    )

    config = config_for()
    snapshot = codex_snapshot()
    updated = adopt_harness(
        config, snapshot, snapshot.identity, control="effort", select="high"
    )
    rebound = rebind_after_adoption(
        config,
        updated,
        snapshot,
        identity=snapshot.identity,
        control="effort",
        select="high",
    )
    assert rebound.capture_identity == snapshot.identity
    assert validate_snapshot_for_harness(rebound, updated) == rebound
    altered = updated.model_copy(update={"model": "other/model"})
    with pytest.raises(MetadataError, match="exceed"):
        rebind_after_adoption(
            config,
            altered,
            snapshot,
            identity=snapshot.identity,
            control="effort",
            select="high",
        )


def test_collector_missing_installation_returns_action_not_fabricated_list(
    tmp_path, monkeypatch
):
    from tetrabench.native_discovery import collect_installed

    monkeypatch.setattr("tetrabench.native_discovery.shutil.which", lambda *_: None)
    snapshot = collect_installed(config_for(), base=tmp_path, reuse_native_cache=False)
    assert snapshot.status == "unavailable"
    assert snapshot.controls == ()
    assert "Install" in " ".join(snapshot.limitations)


def test_plugins_require_opt_in_before_native_process(tmp_path, monkeypatch):
    from tetrabench.harness_config import NativeConfig
    from tetrabench.native_discovery import collect_installed

    config = config_for(
        "opencode", native_config=NativeConfig(text='{"plugin":["some-plugin"]}')
    )

    def forbidden(*_, **__):
        pytest.fail("native process must not start")

    monkeypatch.setattr("tetrabench.native_discovery.run_native", forbidden)
    snapshot = collect_installed(config, base=tmp_path, reuse_native_cache=False)
    assert snapshot.status == "unavailable"
    assert "allow_config_execution" in " ".join(snapshot.limitations)


def test_native_file_and_resource_contents_bind_snapshot_and_write(
    tmp_path, monkeypatch
):
    import tomlkit

    from tetrabench.harness_config import NativeConfig, ResourceSource
    from tetrabench.native_discovery import _prepared

    native = tmp_path / "native.toml"
    native.write_text('model_reasoning_summary = "auto"\n')
    resource = tmp_path / "notes.md"
    resource.write_text("first\n")
    config = config_for(
        native_config=NativeConfig(format="toml", path="native.toml"),
        resources=[ResourceSource(source="notes.md", destination="notes.md")],
    )
    identity = identity_for(config=_prepared(config, tmp_path))
    snapshot = codex_snapshot(identity)
    path = tmp_path / "run.toml"
    original = tomlkit.dumps({"harness": config.model_dump(exclude_none=True)})
    path.write_text(original)
    preview = adopt_handler(path, snapshot, identity, control="effort", select="high")
    assert preview["after_config_digest"] != identity.config_digest
    real_fsync = __import__("os").fsync

    def race(fd):
        real_fsync(fd)
        target = Path(f"/proc/self/fd/{fd}").readlink()
        if target.name.startswith(".reasoning-"):
            resource.write_text("changed during file replacement\n")

    monkeypatch.setattr("os.fsync", race)
    with pytest.raises(MetadataError, match="harness/resources"):
        adopt_handler(
            path, snapshot, identity, control="effort", select="high", write=True
        )
    assert path.read_text() == original
    assert native.read_text() == 'model_reasoning_summary = "auto"\n'


def test_expected_config_file_hash_checked_before_adoption(tmp_path):
    config = config_for(options={"reasoning_effort": "low"})
    snapshot = codex_snapshot(identity_for(config=config))
    path = tmp_path / "run.toml"
    path.write_text(document_for(config))
    with pytest.raises(MetadataError, match="expected inspection bytes"):
        adopt_handler(
            path,
            snapshot,
            snapshot.identity,
            control="effort",
            select="high",
            write=True,
            expected_file_sha256="0" * 64,
        )


def test_no_model_rpc_method_available_to_collector():
    from tetrabench.native_discovery_worker import ControlProcess

    process = object.__new__(ControlProcess)
    with pytest.raises(ValueError, match="non-metadata RPC"):
        process.rpc("thread/start", {}, 1)


def test_request_binding_rejects_unsupported_current_selection():
    from tetrabench.reasoning import validate_snapshot_for_harness

    config = config_for(options={"reasoning_effort": "ultra"})
    snapshot = codex_snapshot(identity_for(config=config))
    with pytest.raises(MetadataError, match="absent"):
        validate_snapshot_for_harness(snapshot, config)


def test_unknown_snapshot_can_prove_binding_but_not_reasoning_support():
    from tetrabench.reasoning import validate_snapshot_for_harness

    snapshot = codex_snapshot().model_copy(update={"status": "unknown"})
    with pytest.raises(MetadataError, match="unknown"):
        validate_snapshot_for_harness(snapshot, config_for())
    assert (
        validate_snapshot_for_harness(snapshot, config_for(), require_supported=False)
        == snapshot
    )


def test_native_variant_name_with_spaces_is_preserved():
    identity = identity_for("opencode")
    payload = opencode_payload({"deep thought": {"reasoningEffort": "high"}})
    snapshot = discover(identity, observation=capture(identity, payload))
    updated = adopt_harness(
        config_for("opencode"),
        snapshot,
        identity,
        control="variant",
        select="deep thought",
    )
    assert updated.options["variant"] == "deep thought"
