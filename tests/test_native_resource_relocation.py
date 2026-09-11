"""Pinned offline OpenCode consumes references inside copied resource configs."""

import json

import pytest
import tomlkit
from native_consumer_support import native_modules
from test_reasoning_installed import native_config
from typer.testing import CliRunner

from tetrabench.cli import app
from tetrabench.config import load_harness_override
from tetrabench.harness_config import HarnessConfig, ResourceSource
from tetrabench.harnesses import seal_harness
from tetrabench.native_discovery import collect_installed
from tetrabench.plan import canonical_model_bytes
from tetrabench.reasoning import harness_config_digest
from tetrabench.resources import AGENT_RESOURCE_ROOT

pytestmark = pytest.mark.native


@pytest.mark.parametrize("suffix", ["json", "jsonc"])
def test_public_inspect_adopt_nested_resource_reference(tmp_path, suffix):
    modules = native_modules(required=True)
    assert modules is not None
    bundle = tmp_path / "bundle"
    (bundle / "prompts").mkdir(parents=True)
    prompt = bundle / "prompts/rules.md"
    prompt.write_text("Offline nested native prompt. No model call.\n")
    overlay = bundle / ("opencode." + suffix)
    text = json.dumps({"agent": {"build": {"prompt": "{file:prompts/rules.md}"}}})
    overlay.write_text(
        "// native JSONC comment\n" + text if suffix == "jsonc" else text
    )
    config = HarnessConfig.model_validate(
        native_config("opencode").model_dump()
        | {
            "discovery": "isolated",
            "resources": [
                ResourceSource(source="bundle", destination="opencode", directory=True)
            ],
        }
    )
    original_files = {path: path.read_bytes() for path in (overlay, prompt)}
    sealed = seal_harness(config, tmp_path)
    before = canonical_model_bytes(sealed)
    config_resource = next(
        item for item in sealed.resources if item.destination.endswith(suffix)
    )
    assert AGENT_RESOURCE_ROOT + "/opencode/prompts/rules.md" in config_resource.text
    observed = collect_installed(
        config, base=tmp_path, modules=modules, reuse_native_cache=False
    )
    assert observed.status == "supported", observed.limitations
    assert observed.identity.config_digest == harness_config_digest(
        config, base=tmp_path
    )
    assert canonical_model_bytes(seal_harness(config, tmp_path)) == before

    path = tmp_path / "run.toml"
    path.write_text(tomlkit.dumps({"harness": config.model_dump(exclude_none=True)}))
    initial = path.read_bytes()
    runner = CliRunner()
    common = [
        "--harness",
        str(path),
        "--native-modules",
        str(modules),
        "--json",
    ]
    inspected = runner.invoke(app, ["models", "inspect", *common, "--no-native-cache"])
    assert inspected.exit_code == 0, inspected.output
    report = json.loads(inspected.stdout)
    assert report["capability"]["status"] == "supported"
    assert (
        report["capability"]["identity"]["config_digest"]
        == observed.identity.config_digest
    )
    assert report["inference_validated"] is False
    command = [
        "models",
        "adopt",
        *common,
        "--control",
        "variant",
        "--select",
        "deliberate",
    ]
    preview = runner.invoke(app, command)
    assert preview.exit_code == 0, preview.output
    assert path.read_bytes() == initial
    written = runner.invoke(app, [*command, "--write"])
    assert written.exit_code == 0, written.output
    adopted = load_harness_override(path)
    assert adopted.options["variant"] == "deliberate"
    assert adopted.capability_snapshot is not None
    snapshot = adopted.capability_snapshot.snapshot()
    assert snapshot.capture_identity.config_digest == observed.identity.config_digest
    assert snapshot.identity.config_digest == harness_config_digest(adopted)
    assert seal_harness(adopted, tmp_path).resources == sealed.resources
    assert {p: p.read_bytes() for p in original_files} == original_files
    assert str(tmp_path / "resources") not in snapshot.model_dump_json()
