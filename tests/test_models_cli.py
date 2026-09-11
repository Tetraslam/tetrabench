"""Native acquisition is automatic; adoption binds the post-change request."""

import json

import pytest
from typer.testing import CliRunner

from tetrabench.capabilities import (
    Binding,
    CapabilityIdentity,
    CapabilitySnapshot,
    Choice,
    Control,
    Setting,
    make_evidence,
)
from tetrabench.cli import app
from tetrabench.config import load_harness_override
from tetrabench.harness_config import HarnessConfig, bind_capability_adoption
from tetrabench.harnesses import seal_harness
from tetrabench.plan import canonical_model_bytes
from tetrabench.reasoning import harness_config_digest


def snapshot(config):
    identity = CapabilityIdentity(
        harness=config.name,
        harness_version=config.version,
        native_adapter_version="test-native",
        requested_model=config.model,
        resolved_model="model",
        provider_id="openai",
        route_id="fixture-route",
        protocol="responses",
        endpoints=("https://example.test/v1",),
        fallback_policy_json="{}",
        auth_mode="none",
        config_digest=harness_config_digest(config),
        route_status="bound",
    )
    evidence = make_evidence(
        "https://example.test/native",
        "native metadata",
        "2026-09-10T00:00:00Z",
        "native-model-list",
    )
    setting = Setting(
        binding=Binding(surface="options", path=("reasoning_effort",)),
        value_json='"high"',
    )
    control = Control(
        name="effort",
        kind="choices",
        status="supported",
        choices=(Choice(name="high", settings=(setting,), normalization="identity"),),
        evidence=(evidence,),
    )
    return CapabilitySnapshot(
        identity=identity, status="supported", controls=(control,), evidence=(evidence,)
    )


def project(tmp_path):
    path = tmp_path / "run.toml"
    path.write_text(
        '# preserve this comment\n[harness]\nname="codex"\n'
        'version="0.154.0"\nmodel="openai/model"\n'
    )
    return path


def test_model_inspect_calls_installed_acquisition_without_manual_payload(
    tmp_path, monkeypatch
):
    path = project(tmp_path)
    calls = []

    def inspect(config, **options):
        calls.append(options)
        result = snapshot(config)
        return {
            "capability": result.model_dump(mode="json"),
            "snapshot_sha256": result.digest,
            "inference_validated": False,
        }

    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = CliRunner().invoke(
        app, ["models", "inspect", "--harness", str(path), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["inference_validated"] is False
    assert calls[0]["refresh"] is False
    assert calls[0]["allow_config_execution"] is False


def test_adopt_preview_then_write_binds_post_adoption_and_preserves_comments(
    tmp_path, monkeypatch
):
    path = project(tmp_path)
    before = path.read_bytes()
    captures = []

    def collect(config, **_):
        result = snapshot(config)
        captures.append(result)
        return result

    monkeypatch.setattr("tetrabench.native_discovery.collect_installed", collect)
    command = [
        "models",
        "adopt",
        "--harness",
        str(path),
        "--control",
        "effort",
        "--select",
        "high",
        "--json",
    ]
    preview = CliRunner().invoke(app, command)
    assert preview.exit_code == 0, preview.output
    assert path.read_bytes() == before
    written = CliRunner().invoke(app, [*command, "--write"])
    assert written.exit_code == 0, written.output
    assert path.read_text().startswith("# preserve this comment")
    adopted = load_harness_override(path)
    assert adopted.options["reasoning_effort"] == "high"
    assert adopted.capability_snapshot is not None
    bound = adopted.capability_snapshot.snapshot()
    assert bound.identity.config_digest == harness_config_digest(adopted)
    assert bound.capture_identity == captures[-1].identity
    assert bound.adopted_from_digest == captures[-1].digest
    resolved = seal_harness(adopted, tmp_path)
    assert b'"capability_snapshot"' in canonical_model_bytes(resolved)


def test_snapshot_detects_model_option_and_auth_config_drift(tmp_path):
    source = load_harness_override(project(tmp_path))
    bound = bind_capability_adoption(
        source, snapshot(source), control="effort", select="high"
    )
    for change in (
        {"model": "openai/other"},
        {"options": {"reasoning_effort": "low"}},
        {"env": {"OPENAI_API_KEY": "${OTHER_KEY}"}},
    ):
        data = bound.model_dump(mode="python") | change
        with pytest.raises(ValueError, match=r"snapshot|drift"):
            HarnessConfig.model_validate(data)


def test_unknown_native_metadata_never_writes_or_clamps(tmp_path, monkeypatch):
    path = project(tmp_path)
    before = path.read_bytes()

    def collect(config, **_):
        return snapshot(config).model_copy(update={"status": "unknown"})

    monkeypatch.setattr("tetrabench.native_discovery.collect_installed", collect)
    result = CliRunner().invoke(
        app,
        [
            "models",
            "adopt",
            "--harness",
            str(path),
            "--control",
            "effort",
            "--select",
            "high",
            "--write",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert path.read_bytes() == before
