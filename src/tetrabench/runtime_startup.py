"""Run native startup metadata checks through the existing execution context."""

from __future__ import annotations

import json
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from importlib.resources import files
from typing import Any

from tetrabench.canonical_json import sha256_hex
from tetrabench.capabilities import MetadataError, metadata_text, parse_metadata
from tetrabench.harness_config import ResolvedHarness
from tetrabench.harnesses import native_configuration_layers
from tetrabench.reasoning import validate_snapshot_for_harness
from tetrabench.resources import AGENT_RESOURCE_ROOT

MetadataExecutor = Callable[..., Awaitable[Any]]


def startup_capability_probe(
    harness: ResolvedHarness,
    *,
    native_files: Mapping[str, str],
    absent_files: tuple[str, ...] = (),
    resource_root: str = AGENT_RESOURCE_ROOT,
    command: tuple[str, ...] | None = None,
    settings_file: str | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    if harness.capability_snapshot is None:
        raise MetadataError("startup verification requires an adopted snapshot")
    snapshot = validate_snapshot_for_harness(harness.capability_snapshot, harness)
    policy = policy or snapshot.verification_policy
    if policy not in {"native-startup", "strict-startup"}:
        raise MetadataError("unknown native startup verification policy")
    option, control_name = {
        "opencode": ("variant", "variant"),
        "codex": ("reasoning_effort", "effort"),
        "claude-code": ("reasoning_effort", "effort"),
        "pi": ("thinking", "thinking"),
    }[harness.name]
    selected = harness.options.get(option)
    control = next((c for c in snapshot.controls if c.name == control_name), None)
    choice = (
        next((c for c in control.choices if c.name == selected), None)
        if control
        else None
    )
    bindings = dict(native_files)
    for resource in harness.resources:
        path = resource_root + "/" + resource.destination
        if path in bindings and bindings[path] != resource.sha256:
            raise MetadataError("native/resource configuration bindings disagree")
        bindings[path] = resource.sha256
    if not bindings and not absent_files:
        raise MetadataError(
            "bind the actual native configuration files or their absence"
        )
    from tetrabench.native_discovery import _config_execution

    layers = native_configuration_layers(harness)
    executable_config = any(
        _config_execution(layer.config, harness) for layer in layers.layers
    )
    executable_config |= any(
        harness.options.get(key)
        for key in (
            "mcp_config",
            "agent",
            "agents",
            "extension",
        )
    )
    probe = {
        "schema_version": 1,
        "identity": snapshot.identity.model_dump(mode="json"),
        "snapshot_sha256": snapshot.digest,
        "policy": policy,
        "selected": selected,
        "native_value": parse_metadata(choice.native_value_json)
        if choice and choice.native_value_json is not None
        else None,
        "command": list(
            command or ({"claude-code": "claude"}.get(harness.name, harness.name),)
        ),
        "files": [
            {
                "path": p,
                "sha256": h,
                "opencode_schema_normalization": harness.name == "opencode"
                and not p.startswith(resource_root + "/"),
            }
            for p, h in sorted(bindings.items())
        ],
        "absent_files": list(absent_files),
        "settings_file": settings_file,
        "setting_sources": ""
        if harness.discovery == "isolated"
        else harness.options.get("setting_sources"),
        # Do not run configured hooks/helpers twice, or turn discovery into inference.
        "metadata_allowed": not executable_config,
        "refreshable_auth": harness.auth is not None
        and harness.auth.mode == "chatgpt_oauth",
    }
    if harness.name == "pi":
        from tetrabench.runtime_reasoning import runtime_capability_probe

        captured = runtime_capability_probe(
            harness,
            resource_root=resource_root,
            native_files=native_files,
            absent_native_files=absent_files,
        )
        probe.update(
            capture=captured["capture"], catalog_entry=captured["catalog_entry"]
        )
        flags = [
            "--" + name.replace("_", "-")
            for name in (
                "no_extensions",
                "no_skills",
                "no_prompt_templates",
                "no_themes",
                "no_context_files",
            )
            if harness.options.get(name) or harness.discovery == "isolated"
        ]
        if harness.discovery == "isolated":
            flags.append("--no-approve")
        probe["pi_discovery_flags"] = flags
    return probe


def startup_capability_assets(probe: Mapping[str, Any]) -> dict[str, str]:
    text = metadata_text(dict(probe))
    if len(text.encode()) > 128 * 1024:
        raise MetadataError("native startup probe exceeds 128 KiB")
    return {
        "startup-probe.json": text,
        **{
            name: files("tetrabench").joinpath(name).read_text()
            for name in ("native_control.py", "runtime_metadata.py")
        },
    }


async def _guard_directory(agent: Any, environment: Any) -> str:
    if agent._guard_directory is None:
        agent._guard_directory = "/tmp/tetrabench-capability-" + uuid.uuid4().hex  # nosec B108
        await agent.exec_as_agent(
            environment, command="mkdir -m 700 " + shlex.quote(agent._guard_directory)
        )
    return agent._guard_directory


async def _pi_files(agent: Any, environment: Any, env: dict[str, str]):
    directory = await _guard_directory(agent, environment)
    root = env.get("PI_CODING_AGENT_DIR") or agent._pi_config_root
    if root is None:
        root = directory + "/pi"
        await agent.exec_as_agent(
            environment, command="mkdir -p -m 700 " + shlex.quote(root)
        )
    env["PI_CODING_AGENT_DIR"] = root
    native_files = {}
    absent = []
    for name in ("settings.json", "models.json"):
        target = root + "/" + name
        digest = agent._uploaded_native_files.get(
            target
        ) or agent._uploaded_native_files.get(
            "/tmp/harbor-pi-agent/" + name  # nosec B108
        )
        if digest is None:
            absent.append(target)
        else:
            native_files[target] = digest
    return native_files, tuple(absent)


async def validate_runtime_capability(
    agent: Any,
    environment: Any,
    *,
    env: dict[str, str],
    cwd: str | None = None,
    metadata_executor: MetadataExecutor | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    """Callable adapter hook for all four harnesses, before the native model command.

    Auth owners may provide metadata_executor (or hook.execute_metadata) to run
    metadata processes sequentially with status/refresh/write-back accounting in
    their existing lease. Without that interface an authenticated run uses only
    file/version checks for non-Pi and reports model/route metadata unverified.
    Pi and strict-startup require actual startup metadata. No credential state is
    cloned or read by this hook. No continuous dispatch verification is claimed.
    """
    if agent.harness.capability_snapshot is None:
        return {"scope": "not-adopted", "inference_validated": False}
    native_files = dict(agent._uploaded_native_files)
    absent: tuple[str, ...] = ()
    settings_file = None
    if agent.harness.name == "pi":
        native_files, absent = await _pi_files(agent, environment, env)
    elif agent.harness.name == "opencode":
        # Reuse the native adapter's exact registration bytes, never redacted logs.
        tokens = shlex.split(agent._build_register_config_command() or "")
        if "echo" not in tokens or ">" not in tokens:
            raise MetadataError("native OpenCode configuration writer contract changed")
        content = tokens[tokens.index("echo") + 1] + "\n"
        target = tokens[tokens.index(">") + 1]
        native_files[target] = sha256_hex(content.encode())
    elif agent.harness.name == "claude-code":
        target = str(agent._REMOTE_SETTINGS_PATH)
        settings_file = target if target in native_files else None
        if not native_files:
            absent = (target,)
    elif not native_files:
        absent = ((env.get("CODEX_HOME") or "~/.codex") + "/config.toml",)
    probe = startup_capability_probe(
        agent.harness,
        native_files=native_files,
        absent_files=absent,
        settings_file=settings_file,
        policy=policy,
    )
    directory = await _guard_directory(agent, environment)
    for name, content in startup_capability_assets(probe).items():
        await agent._upload_config_text(
            environment,
            content=content,
            remote_path=directory + "/" + name,
            filename=name,
        )
    command = (
        "if [ -f ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
        'export PATH="$HOME/.local/bin:$PATH"; '
        + shlex.join(
            [
                "python3",
                directory + "/runtime_metadata.py",
                directory + "/startup-probe.json",
            ]
        )
    )
    hook = agent._runtime_auth_hook
    executor = metadata_executor or (
        getattr(hook, "execute_metadata", None) if hook else None
    )
    if agent.harness.auth is not None and executor is None:
        command += " --files-only"
    if executor:
        result = await executor(environment, command, env=env, cwd=cwd, timeout_sec=40)
    else:
        result = await agent.exec_as_agent(
            environment, command=command, env=env, cwd=cwd, timeout_sec=40
        )
    if result.return_code:
        raise MetadataError("native startup verification failed before inference")
    try:
        report = json.loads(result.stdout or "")
        if (
            not isinstance(report, dict)
            or report.get("snapshot_sha256") != probe["snapshot_sha256"]
            or report.get("inference_validated") is not False
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise MetadataError(
            "native startup verification returned invalid evidence"
        ) from None
    agent._capability_verification = report
    return report
