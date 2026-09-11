"""Startup proof against the published Pi bundle, not its unbundled SDK exports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from native_consumer_support import native_environment, native_modules, native_run

from tetrabench.harness_config import HarnessConfig, bind_capability_adoption
from tetrabench.harnesses import seal_harness
from tetrabench.native_discovery import collect_installed
from tetrabench.runtime_startup import (
    startup_capability_assets,
    startup_capability_probe,
)

pytestmark = pytest.mark.native


@pytest.fixture
def published_probe(tmp_path):
    modules = native_modules(required=True)
    assert modules is not None
    package = modules / "@earendil-works/pi-coding-agent"
    manifest = json.loads((package / "package.json").read_text())
    executable = modules / ".bin/pi"
    assert manifest["version"] == "0.85.1"
    assert manifest["bin"]["pi"] == "dist/bundle/cli.js"
    assert executable.resolve() == (package / manifest["bin"]["pi"]).resolve()
    model = {
        "id": "published-cache-only",
        "name": "Published fixture",
        "provider": "openai",
        "api": "openai-responses",
        "baseUrl": "https://example.test/v1",
        "reasoning": True,
        "thinkingLevelMap": {"xhigh": "extreme"},
        "contextWindow": 32768,
        "maxTokens": 8192,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    source = tmp_path / "source-catalog.json"
    source.write_text(
        json.dumps({"openai": {"lastModified": 4102444800000, "models": [model]}})
    )
    config = HarnessConfig(
        name="pi", version="0.85.1", model="openai/published-cache-only"
    )
    snapshot = collect_installed(
        config, base=tmp_path, modules=modules, native_cache=source
    )
    assert snapshot.status == "supported", snapshot.limitations
    bound = bind_capability_adoption(
        config, snapshot, control="thinking", select="xhigh"
    )
    env = native_environment(tmp_path)
    root = Path(env["PI_CODING_AGENT_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    probe = startup_capability_probe(
        seal_harness(bound, tmp_path),
        native_files={},
        resource_root=str(tmp_path / "resources"),
        absent_files=(str(root / "models.json"), str(root / "settings.json")),
        command=(str(executable),),
    )
    return probe, env, root


def run_published(tmp_path, probe, env):
    for name, contents in startup_capability_assets(probe).items():
        (tmp_path / name).write_text(contents)
    return native_run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--pid",
            "--fork",
            "--kill-child",
            "python3",
            str(tmp_path / "runtime_metadata.py"),
            str(tmp_path / "startup-probe.json"),
            "--isolated-network",
        ],
        tmp_path,
        env,
    )


def test_published_pi_fresh_catalog_adoption_startup(published_probe, tmp_path):
    probe, env, root = published_probe
    assert not (root / "models-store.json").exists()
    assert "NODE_OPTIONS" not in env
    result = run_published(tmp_path, probe, env)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["scope"] == "native-startup"
    assert report["observed_selection"] == "xhigh"
    assert report["metadata_complete"] is True
    assert report["future_dispatch_verified"] is False
    assert report["inference_validated"] is False
    assert (root / "models-store.json").exists()
    assert not (root / "models.json").exists()
    assert not (root / "settings.json").exists()
    repeated = run_published(tmp_path, probe, env)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)["metadata_complete"] is True


def test_published_pi_bad_probe_refused_after_actual_model_lookup(
    published_probe, tmp_path
):
    probe, env, _ = published_probe
    probe["capture"]["model"]["maxTokens"] = 4096
    result = run_published(tmp_path, probe, env)
    assert result.returncode == 78
    assert "Pi native model/catalog metadata drift" in result.stdout


def test_published_pi_missing_cache_model_refused(published_probe, tmp_path):
    probe, env, root = published_probe
    probe["catalog_entry"] = None
    result = run_published(tmp_path, probe, env)
    assert result.returncode == 78
    assert not (root / "models.json").exists()


def test_published_pi_existing_catalog_drift_not_replaced(published_probe, tmp_path):
    probe, env, root = published_probe
    assert run_published(tmp_path, probe, env).returncode == 0
    path = root / "models-store.json"
    stored = json.loads(path.read_text())
    stored["openai"]["models"][0]["thinkingLevelMap"]["xhigh"] = "high"
    path.write_text(json.dumps(stored))
    result = run_published(tmp_path, probe, env)
    assert result.returncode == 78
    assert (
        json.loads(path.read_text())["openai"]["models"][0]["thinkingLevelMap"]["xhigh"]
        == "high"
    )


def test_unbundled_cli_is_not_accepted_as_published_consumer(published_probe, tmp_path):
    probe, env, _ = published_probe
    package = Path(probe["command"][0]).resolve().parents[2]
    probe["command"] = ["node", str(package / "dist/cli.js")]
    result = run_published(tmp_path, probe, env)
    assert result.returncode == 78
    assert "published bin.pi entrypoint" in result.stdout
