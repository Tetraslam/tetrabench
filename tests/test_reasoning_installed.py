"""Real pinned control-only consumers in network/PID namespaces, no auth or prompts."""

from __future__ import annotations

import json

import pytest
from native_consumer_support import native_modules

from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.capabilities import CapabilitySnapshotRef, MetadataError
from tetrabench.discovery import NATIVE_VERSIONS
from tetrabench.harness_config import HarnessConfig, NativeConfig, ResourceSource
from tetrabench.native_discovery import collect_installed, inspect_installed_handler
from tetrabench.reasoning import (
    adopt_handler,
    harness_config_digest,
    select_reasoning,
    validate_snapshot_for_harness,
)

pytestmark = pytest.mark.native


def native_config(name: str) -> HarnessConfig:
    native = None
    model = "openai/gpt-5.4"
    if name == "opencode":
        model = "fixture/m"
        native = {
            "provider": {
                "fixture": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": "https://example.test/v1"},
                    "models": {
                        "m": {
                            "name": "metadata-fixture",
                            "reasoning": True,
                            "limit": {"context": 32768, "output": 4096},
                            "variants": {"deliberate": {"reasoningEffort": "high"}},
                        }
                    },
                }
            }
        }
    elif name == "pi":
        model = "fixture/m"
        native = {
            "models": {
                "providers": {
                    "fixture": {
                        "api": "openai-responses",
                        "baseUrl": "https://example.test/v1",
                        "models": [
                            {
                                "id": "m",
                                "reasoning": True,
                                "thinkingLevelMap": {
                                    "minimal": None,
                                    "xhigh": "extreme",
                                },
                            }
                        ],
                    }
                }
            }
        }
    elif name == "codex":
        native = {
            "model_provider": "fixture",
            "model_providers": {
                "fixture": {
                    "name": "fixture",
                    "base_url": "https://example.test/v1",
                    "wire_api": "responses",
                }
            },
        }
    else:
        model = "anthropic/claude-sonnet-4-6"
    return HarnessConfig(
        name=name,
        version=NATIVE_VERSIONS[name][0],
        model=model,
        native_config=NativeConfig(text=json.dumps(native)) if native else None,
    )


@pytest.fixture
def installed():
    modules = native_modules(required=True)
    assert modules is not None
    return modules


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code", "pi"])
def test_installed_control_metadata_no_network(installed, tmp_path, name):
    config = native_config(name)
    snapshot = collect_installed(
        config, base=tmp_path, modules=installed, reuse_native_cache=False
    )
    assert snapshot.status in {"supported", "unknown"}, snapshot.limitations
    assert snapshot.controls[0].choices
    assert snapshot.evidence[0].kind == "explicit"
    assert "network namespace" in " ".join(snapshot.limitations)
    assert snapshot.identity.config_digest == harness_config_digest(
        config, base=tmp_path
    )
    assert snapshot.identity.auth_mode == "none"
    assert snapshot.to_bytes()
    assert snapshot.evidence[0].method
    if name == "claude-code":
        assert "initialize.models" in snapshot.evidence[0].method
    if name == "codex":
        assert "model/list" in snapshot.evidence[0].method


def test_installed_pi_capture_adoption_and_bound_post_config(installed, tmp_path):
    config = native_config("pi")
    path = tmp_path / "run.toml"
    import tomlkit

    assert config.native_config is not None
    path.write_text(
        tomlkit.dumps(
            {
                "harness": {
                    "name": config.name,
                    "version": config.version,
                    "model": config.model,
                    "native_config": config.native_config.model_dump(exclude_none=True),
                }
            }
        )
    )
    report = inspect_installed_handler(
        config, base=tmp_path, modules=installed, reuse_native_cache=False
    )
    reference = CapabilitySnapshotRef.model_validate(
        {
            "sha256": report["snapshot_sha256"],
            "snapshot_json": dumps_canonical_json(report["capability"]).decode(),
        }
    )
    snapshot = reference.snapshot()
    with pytest.raises(MetadataError, match="clamps"):
        adopt_handler(
            path, snapshot, snapshot.identity, control="thinking", select="minimal"
        )
    preview = adopt_handler(
        path, snapshot, snapshot.identity, control="thinking", select="xhigh"
    )
    assert not preview["written"]
    result = adopt_handler(
        path,
        snapshot,
        snapshot.identity,
        control="thinking",
        select="xhigh",
        write=True,
    )
    rebound = CapabilitySnapshotRef.model_validate(result["after_capability_snapshot"])
    new_config = HarnessConfig.model_validate(
        tomlkit.parse(path.read_text())["harness"].unwrap()
    )
    assert new_config.options["thinking"] == "xhigh"
    validated = validate_snapshot_for_harness(rebound, new_config, base=tmp_path)
    assert validated.identity.config_digest == result["after_config_digest"]
    assert validated.capture_identity == snapshot.identity
    assert validated.adopted_from_digest == snapshot.digest


def test_installed_pi_reads_native_cache_without_modifying_it(installed, tmp_path):
    config = HarnessConfig(name="pi", version="0.85.1", model="openai/cached-fixture")
    cache = tmp_path / "models-store.json"
    contents = json.dumps(
        {
            "openai": {
                "lastModified": 4102444800000,
                "models": [
                    {
                        "id": "cached-fixture",
                        "provider": "openai",
                        "api": "openai-responses",
                        "baseUrl": "https://example.test/v1",
                        "reasoning": True,
                        "thinkingLevelMap": {"minimal": None, "xhigh": "high"},
                    }
                ],
            }
        }
    )
    cache.write_text(contents)
    snapshot = collect_installed(
        config, base=tmp_path, modules=installed, native_cache=cache
    )
    assert snapshot.status == "supported", snapshot.limitations
    assert snapshot.identity.resolved_model == "cached-fixture"
    assert cache.read_text() == contents
    assert "cache copy SHA-256" in " ".join(snapshot.limitations)


def test_installed_cli_inspects_without_payload_file(installed, tmp_path):
    import subprocess
    import sys

    import tomlkit

    config = native_config("pi")
    path = tmp_path / "run.toml"
    path.write_text(tomlkit.dumps({"harness": config.model_dump(exclude_none=True)}))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tetrabench.native_discovery",
            "inspect",
            "--harness",
            str(path),
            "--native-modules",
            str(installed),
            "--no-native-cache",
        ],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["capability"]["status"] == "supported", output
    assert output["suggestions"]
    assert output["inference_validated"] is False


@pytest.mark.parametrize("discovery", ["native", "isolated"])
def test_opencode_sealed_config_directory_disables_native_variant(
    installed, tmp_path, discovery
):
    overlay = tmp_path / "extra.json"
    overlay.write_text(
        json.dumps(
            {
                "provider": {
                    "fixture": {
                        "models": {
                            "m": {
                                "variants": {"deliberate": {"disabled": True}},
                            }
                        }
                    }
                }
            }
        )
    )
    values = native_config("opencode").model_dump()
    values.update(
        discovery=discovery,
        resources=[
            ResourceSource(
                source="extra.json",
                destination="opencode/opencode.json",
            )
        ],
    )
    config = HarnessConfig.model_validate(values)
    snapshot = collect_installed(
        config, base=tmp_path, modules=installed, reuse_native_cache=False
    )
    assert snapshot.status in {"supported", "unknown"}, snapshot.limitations
    variant = next(
        control for control in snapshot.controls if control.name == "variant"
    )
    assert "deliberate" not in [choice.name for choice in variant.choices]
    with pytest.raises(MetadataError):
        select_reasoning(
            snapshot, snapshot.identity, control="variant", select="deliberate"
        )


def test_opencode_resource_endpoint_has_native_priority_and_old_binding_rejects(
    installed, tmp_path
):
    original = collect_installed(
        native_config("opencode"),
        base=tmp_path,
        modules=installed,
        reuse_native_cache=False,
    )
    overlay = tmp_path / "extra.json"
    overlay.write_text(
        json.dumps(
            {
                "provider": {
                    "fixture": {
                        "options": {"baseURL": "https://alternate.test/v1"},
                    }
                }
            }
        )
    )
    config = HarnessConfig.model_validate(
        native_config("opencode").model_dump()
        | {
            "resources": [
                ResourceSource(
                    source="extra.json", destination="opencode/opencode.json"
                )
            ],
        }
    )
    observed = collect_installed(
        config, base=tmp_path, modules=installed, reuse_native_cache=False
    )
    assert observed.status == "supported", observed.limitations
    assert observed.identity.endpoints == ("https://alternate.test/v1",)
    # Even updating the config hash alone cannot conceal a changed native route.
    stale = original.identity.model_copy(
        update={"config_digest": observed.identity.config_digest}
    )
    with pytest.raises(MetadataError, match="identity drift"):
        observed.require_current(stale)
