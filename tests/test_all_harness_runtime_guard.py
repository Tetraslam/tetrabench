"""Adapter dispatch uses real verifier APIs without promoting unknown evidence."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.agent.context import AgentContext
from test_adapter_reasoning_guard import adopted_pi
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.capabilities import (
    Binding,
    CapabilityIdentity,
    CapabilitySnapshot,
    CapabilitySnapshotRef,
    Choice,
    Control,
    Setting,
    make_evidence,
)
from tetrabench.harness_config import HarnessConfig, capability_config
from tetrabench.harnesses import STABLE_VERSIONS, compile_agent_config, seal_harness
from tetrabench.reasoning import harness_config_digest


def adopted(tmp_path, name, *, strict=False):
    if name == "pi":
        return adopted_pi(tmp_path)[0]
    option, control = (
        ("variant", "variant") if name == "opencode" else ("reasoning_effort", "effort")
    )
    model = "anthropic/claude-opus-5[1m]" if name == "claude-code" else "openai/model"
    source = HarnessConfig(
        name=name, version=STABLE_VERSIONS[name], model=model, options={option: "high"}
    )
    source = capability_config(seal_harness(source, tmp_path))
    identity = CapabilityIdentity(
        harness=name,
        harness_version=source.version,
        native_adapter_version=source.version,
        requested_model=model,
        resolved_model=model.split("/", 1)[1],
        provider_id=model.split("/", 1)[0],
        route_id="native-test",
        protocol="unknown",
        endpoints=(),
        fallback_policy_json="{}",
        auth_mode="none",
        config_digest=harness_config_digest(source),
        route_status="bound",
    )
    evidence = make_evidence(
        "https://example.test/native",
        "synthetic native metadata",
        "2026-09-10T00:00:00Z",
        "native control metadata",
    )
    choice = Choice(
        name="high",
        settings=(
            Setting(
                binding=Binding(surface="options", path=(option,)), value_json='"high"'
            ),
        ),
        normalization="identity",
    )
    snapshot = CapabilitySnapshot(
        identity=identity,
        status="supported",
        controls=(
            Control(
                name=control,
                kind="choices",
                status="supported",
                choices=(choice,),
                evidence=(evidence,),
            ),
        ),
        verification_policy="strict-startup" if strict else "native-startup",
    )
    spec = HarnessConfig.model_validate(
        source.model_dump(mode="python")
        | {"capability_snapshot": CapabilitySnapshotRef.from_snapshot(snapshot)}
    )
    return AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(spec, tmp_path)), logs_dir=tmp_path / "logs"
    )


class MetadataEnvironment(CaptureEnvironment):
    def __init__(self, name):
        super().__init__(name, STABLE_VERSIONS[name])
        self.metadata_calls = 0

    async def exec(self, **kwargs):
        if "runtime_metadata.py" not in kwargs["command"]:
            return await super().exec(**kwargs)
        self.commands.append(kwargs)
        self.metadata_calls += 1
        probe = json.loads(
            next(
                content
                for path, content in self.uploads.items()
                if path.endswith("startup-probe.json")
            )
        )
        if probe["policy"] == "strict-startup":
            return SimpleNamespace(
                return_code=78,
                stdout='{"error":"strict startup metadata remains unverified"}',
                stderr="",
            )
        return SimpleNamespace(
            return_code=0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "scope": "native-startup",
                    "snapshot_sha256": probe["snapshot_sha256"],
                    "verified_fields": ["native_version", "configuration_files"],
                    "unverified_fields": ["endpoint", "protocol"],
                    "inference_validated": False,
                    "future_dispatch_verified": False,
                }
            ),
            stderr="",
        )


@pytest.mark.parametrize("name", STABLE_VERSIONS)
def test_all_four_adopted_adapters_dispatch_and_keep_verification_evidence(
    tmp_path, name
):
    instance: Any = adopted(tmp_path, name)
    environment = MetadataEnvironment(name)
    asyncio.run(instance.run("no actual inference", environment, AgentContext()))
    assert any(
        instance._is_native_model_command(
            row["command"].removeprefix("set -o pipefail; ")
        )
        for row in environment.commands
    )
    report = instance._capability_verification
    assert report["inference_validated"] is False
    assert environment.metadata_calls == 1 and report["scope"] == "native-startup"
    assert report["unverified_fields"] == ["endpoint", "protocol"]
    assert report["future_dispatch_verified"] is False
    provenance = json.loads((instance.logs_dir / "tetrabench-harness.json").read_text())
    assert provenance["capability_verification"] == report
    assert provenance["capability_startup_route_verified"] is False


@pytest.mark.parametrize("name", ["opencode", "codex", "claude-code"])
def test_strict_unknown_startup_refuses_before_model_dispatch(tmp_path, name):
    instance: Any = adopted(tmp_path, name, strict=True)
    environment = MetadataEnvironment(name)
    with pytest.raises(
        (ValueError, NonZeroAgentExitCodeError), match=r"startup|Command failed"
    ):
        asyncio.run(instance.run("must not execute", environment, AgentContext()))
    assert not any(
        instance._is_native_model_command(
            row["command"].removeprefix("set -o pipefail; ")
        )
        for row in environment.commands
    )


def test_observed_auth_has_native_source_and_is_not_inferred_from_configuration(
    tmp_path,
):
    instance: Any = adopted(tmp_path, "codex")
    assert instance._auth_evidence()["observed_mode"] is None
    instance._runtime_auth_hook = SimpleNamespace(
        observed_auth_provenance=lambda: {
            "observed": {"mode": "api_key", "source": "native_status"},
            "observations": 1,
        }
    )
    assert instance._auth_evidence()["observed_mode"] == "api_key"
    assert instance._auth_evidence()["account_verified"] is False
    instance._runtime_auth_hook = SimpleNamespace(
        observed_auth_provenance=lambda: {
            "observed": {"mode": "api_key", "source": "env_presence"}
        }
    )
    assert instance._auth_evidence()["observed_mode"] is None
