"""Naturally exiting OpenCode metadata retains route checks and credential custody."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from native_consumer_support import native_environment, native_modules, native_run

from tetrabench import native_control
from tetrabench.auth_config import AuthSpec, NativeAuthReference
from tetrabench.canonical_json import sha256_hex
from tetrabench.discovery import discover, observation_from_json
from tetrabench.harness_config import (
    HarnessConfig,
    NativeConfig,
    ResourceSource,
    bind_capability_adoption,
    capability_config,
)
from tetrabench.harnesses import native_configuration_layers, seal_harness
from tetrabench.native_control import CUSTODY_ENV, ControlError, opencode_model_metadata
from tetrabench.native_discovery import _identity
from tetrabench.resources import materialize_resources
from tetrabench.runtime_startup import (
    startup_capability_assets,
    startup_capability_probe,
)


@pytest.mark.parametrize("custody", ["required", "none"])
@pytest.mark.parametrize("stage", ["models", "config"])
@pytest.mark.parametrize("finish", ["normal", "nonzero", "killed", "forced"])
def test_metadata_response_requires_both_natural_zero_exits(
    tmp_path, monkeypatch, custody, stage, finish
):
    monkeypatch.setattr(native_control, "_credential_processes", 0)
    monkeypatch.setattr(native_control, "_graceful_credential_processes", 0)
    script = tmp_path / "fixture.py"
    script.write_text("""import json, os, sys, time
if sys.argv[1] == 'models':
    print('fixture/m')
    result = {'providerID':'fixture', 'api':{'url':'https://model.test/v1'}}
else:
    assert sys.argv[1:] == ['debug', 'config']
    options = {'baseURL':'https://override.test/v1'}
    result = {'provider':{'fixture':{'options':options}}}
print(json.dumps(result), flush=True)
stage = 'models' if sys.argv[1] == 'models' else 'config'
if stage == os.environ['FAIL_STAGE']:
    finish = os.environ['FINISH']
    if finish == 'nonzero': sys.exit(1)
    if finish == 'killed': os.kill(os.getpid(), 9)
    if finish == 'forced': time.sleep(30)
""")

    def collect():
        return opencode_model_metadata(
            [sys.executable, "-I", str(script)],
            tmp_path,
            {CUSTODY_ENV: custody, "FAIL_STAGE": stage, "FINISH": finish},
            provider="fixture",
            model_id="m",
            version="1.18.30",
        )

    if finish == "normal":
        result = collect()
        assert result["options"]["baseURL"] == "https://override.test/v1"
    else:
        with pytest.raises(ControlError):
            collect()
    if custody == "required":
        assert native_control.auth_custody_report() == {
            "schema_version": 1,
            "credential_processes": 2 if finish == "normal" or stage == "config" else 1,
            "graceful_credential_processes": 2
            if finish == "normal"
            else int(stage == "config"),
        }
    else:
        assert native_control.auth_custody_report()["credential_processes"] == 0


def test_models_cli_requires_verified_version_before_spawn(tmp_path):
    with pytest.raises(ControlError, match="unverified OpenCode"):
        opencode_model_metadata(
            ["must-not-run"],
            tmp_path,
            {},
            provider="openai",
            model_id="m",
            version="1.18.31",
        )


@pytest.mark.native
@pytest.mark.parametrize("discovery", ["native", "isolated"])
@pytest.mark.parametrize(
    "change", [None, "variant", "endpoint", "model-endpoint", "removed-model"]
)
def test_actual_oauth_metadata_natural_completion_and_resource_drift(
    tmp_path, discovery, change
):
    modules = native_modules(required=True)
    assert modules is not None
    command = [str(modules / "opencode-linux-x64/bin/opencode")]
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    "openai": {
                        "models": {
                            "gpt-6-astra": {
                                "variants": {"deliberate": {"reasoningEffort": "high"}}
                            }
                        }
                    }
                },
            }
        )
    )
    config = HarnessConfig(
        name="opencode",
        version="1.18.30",
        model="openai/gpt-6-astra",
        discovery=discovery,
        auth=AuthSpec(
            mode="chatgpt_oauth",
            reference=NativeAuthReference(
                profile="unused-synthetic", binding="unused-synthetic", generation=1
            ),
        ),
        native_config=NativeConfig(
            text=json.dumps(
                {
                    "provider": {
                        "openai": {
                            "models": {
                                "gpt-6-astra": {
                                    "variants": {
                                        "deliberate": {"reasoningEffort": "low"}
                                    }
                                }
                            }
                        }
                    }
                }
            )
        ),
        resources=[
            ResourceSource(source="overlay.json", destination="opencode/opencode.json")
        ],
    )
    resolved = seal_harness(config, tmp_path)
    config = capability_config(resolved)
    resource_root = tmp_path / "resources"
    materialize_resources(resolved.resources, resource_root)
    env = native_environment(tmp_path, {CUSTODY_ENV: "required"})
    env["OPENCODE_CONFIG_DIR"] = str(resource_root / "opencode")
    env["OPENCODE_FAKE_VCS"] = "git"
    if discovery == "isolated":
        env.update(
            OPENCODE_DISABLE_PROJECT_CONFIG="1",
            OPENCODE_DISABLE_CLAUDE_CODE="1",
            OPENCODE_DISABLE_EXTERNAL_SKILLS="1",
        )
    auth = Path(env["XDG_DATA_HOME"]) / "opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "openai": {
                    "type": "oauth",
                    "access": "SYNTHETIC_ACCESS_NOT_VALID",
                    "refresh": "SYNTHETIC_REFRESH_NOT_VALID",
                    "expires": 4102444800000,
                }
            }
        )
    )
    auth.chmod(0o600)
    original_auth = auth.read_bytes()
    request = {
        "command": command,
        "version": "1.18.30",
        "provider": "openai",
        "model": "gpt-6-astra",
        "native": native_configuration_layers(resolved).main_config,
        "authenticated": True,
        "require_auth_custody": True,
        "discovery": discovery,
        "config_directory": env["OPENCODE_CONFIG_DIR"],
    }
    request_file = tmp_path / "discovery.json"
    request_file.write_text(json.dumps(request))
    program = (
        "import json,os,sys; from pathlib import Path; "
        "from tetrabench.native_discovery_worker import _opencode; "
        "from tetrabench.native_control import auth_custody_report; "
        "request=json.loads(Path(sys.argv[1]).read_text()); "
        "r=_opencode(request,Path.cwd(),dict(os.environ)); "
        "r['auth_custody']=auth_custody_report(); print(json.dumps(r))"
    )
    response = native_run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            sys.executable,
            "-c",
            program,
            str(request_file),
        ],
        tmp_path,
        env,
    )
    assert response.returncode == 0, response.stdout + response.stderr
    data = json.loads(response.stdout)
    expected_custody = {
        "schema_version": 1,
        "credential_processes": 2,
        "graceful_credential_processes": 2,
    }
    assert data["auth_custody"] == expected_custody
    # The real built-in OAuth plugin zeroes costs only for OAuth-authenticated models.
    assert data["payload"]["all"][0]["models"]["gpt-6-astra"]["cost"]["input"] == 0
    identity = _identity(capability_config(resolved), data["route"], observed=True)
    captured = discover(
        identity,
        observation=observation_from_json(
            identity,
            json.dumps(data["payload"]),
            observed_at="2026-09-11T00:00:00Z",
            method=data["method"],
        ),
    )
    bound = bind_capability_adoption(
        config, captured, control="variant", select="deliberate"
    )
    resolved = seal_harness(bound, tmp_path)
    path = Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
    path.write_text(json.dumps(native_configuration_layers(resolved).main_config))
    probe = startup_capability_probe(
        resolved,
        native_files={str(path): sha256_hex(path.read_bytes())},
        resource_root=str(resource_root),
        command=tuple(command),
        policy="native-startup",
    )
    # Custody comes from the execution owner even if request JSON tries to omit it.
    probe["refreshable_auth"] = False
    if change:
        resource = resource_root / "opencode/opencode.json"
        value = json.loads(resource.read_text())
        provider = value["provider"]["openai"]
        if change == "variant":
            provider["models"]["gpt-6-astra"]["variants"]["deliberate"][
                "reasoningEffort"
            ] = "low"
        elif change == "endpoint":
            provider["options"] = {"baseURL": "https://changed.test/v1"}
        elif change == "removed-model":
            provider["blacklist"] = ["gpt-6-astra"]
        else:
            provider["models"]["gpt-6-astra"]["provider"] = {
                "api": "https://changed.test/v1"
            }
        resource.write_text(json.dumps(value))
        for binding in probe["files"]:
            if binding["path"] == str(resource):
                binding["sha256"] = sha256_hex(resource.read_bytes())
    for name, content in startup_capability_assets(probe).items():
        (tmp_path / name).write_text(content)
    result = native_run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "python3",
            str(tmp_path / "runtime_metadata.py"),
            str(tmp_path / "startup-probe.json"),
        ],
        tmp_path,
        env,
    )
    assert result.returncode == (78 if change else 0), result.stdout + result.stderr
    report = json.loads(result.stdout)
    if change == "removed-model":
        assert report["auth_custody"] == {
            "schema_version": 1,
            "credential_processes": 1,
            "graceful_credential_processes": 1,
        }
        assert report["error"] == "native authenticated metadata completion uncertain"
    else:
        assert report["auth_custody"] == expected_custody
    if change and change != "removed-model":
        assert report["error"] == (
            "native variant meaning changed"
            if change == "variant"
            else "native route drift"
        )
    elif not change:
        assert report["metadata_complete"] is True
        assert report["inference_validated"] is False
        assert report["future_dispatch_verified"] is False
        assert {
            "native_model",
            "native_provider",
            "native_selection",
            "protocol",
        }.issubset(report["verified_fields"])
        assert report["unverified_fields"] == ["endpoint"]
    assert auth.read_bytes() == original_auth
    assert "SYNTHETIC_" not in result.stdout + response.stdout
