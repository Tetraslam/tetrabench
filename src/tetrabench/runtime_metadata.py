"""Native startup verification in the actual sandbox. Standard library only.

The owner runs this sequentially in its existing credential context. This script
never copies auth state, logs in, supplies a prompt, or creates a model turn.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

try:
    from .native_control import (
        CUSTODY_ENV,
        ControlError,
        ControlProcess,
        CredentialCompletionError,
        auth_custody_report,
        claude_control_metadata,
        claude_model_metadata,
        codex_route,
        opencode_model_metadata,
    )
except ImportError:
    # The execution hook uploads both standard-library modules side by side.
    from native_control import (  # ty: ignore[unresolved-import]
        CUSTODY_ENV,
        ControlError,
        ControlProcess,
        CredentialCompletionError,
        auth_custody_report,
        claude_control_metadata,
        claude_model_metadata,
        codex_route,
        opencode_model_metadata,
    )


class Drift(ValueError):
    pass


def passive_configuration(
    probe: dict[str, Any], cwd: Path, env: dict[str, str]
) -> bool:
    """Refuse a second hook/plugin execution, including task-local native layers."""
    paths = {Path(os.path.expanduser(item["path"])) for item in probe["files"]}
    bound_paths = set(paths)
    for directory in (cwd, *cwd.parents):
        paths.update(
            directory / name
            for name in (
                "opencode.json",
                "opencode.jsonc",
                ".opencode/opencode.json",
                ".opencode/opencode.jsonc",
                ".claude/settings.json",
                ".claude/settings.local.json",
            )
        )
        if any(
            (directory / name).exists()
            for name in (
                ".opencode/plugin",
                ".opencode/plugins",
                ".claude/agents",
                ".claude/plugins",
                ".pi/extensions",
            )
        ):
            return False
    if probe["identity"]["harness"] == "claude-code":
        root = Path(env.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        paths.add(root / "settings.json")
        if any((root / name).exists() for name in ("agents", "plugins")):
            return False
        paths.add(Path("/etc/claude-code/managed-settings.json"))
    if probe["identity"]["harness"] == "pi":
        root = Path(env.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
        if (root / "extensions").exists():
            return False
    dangerous = {
        "hooks",
        "plugin",
        "plugins",
        "enabledPlugins",
        "initialPrompt",
        "apiKeyHelper",
        "awsAuthRefresh",
        "awsCredentialExport",
        "mcp",
        "mcpServers",
        "mcp_servers",
        "command",
    }
    for path in paths:
        if not path.is_file() or path.suffix not in {".json", ".jsonc", ".toml"}:
            continue
        try:
            if path.suffix == ".toml":
                import tomllib

                value = tomllib.loads(read_bounded(str(path)).decode())
            else:
                value = json.loads(read_bounded(str(path)))
        except Exception:
            # The native executable still owns richer formats such as JSONC.
            if path.suffix == ".jsonc" and path in bound_paths:
                continue  # Already parsed/gated by the shared sealed-layer helper.
            return False
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if any(key in dangerous and child for key, child in item.items()):
                    return False
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return True


def read_bounded(path: str, limit: int = 128 * 1024) -> bytes:
    # Only HOME is expanded; never dereference a credential environment selector.
    target = os.path.expanduser(path)
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Drift("configuration is not a regular file")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(data) > limit or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise Drift("configuration changed or exceeded limit")
        return data


def verify_files(
    probe: dict[str, Any], originals: dict[str, bytes] | None = None
) -> dict[str, bytes]:
    observed = {}
    for binding in probe["files"]:
        data = read_bounded(binding["path"])
        observed[binding["path"]] = data
        if hashlib.sha256(data).hexdigest() != binding["sha256"]:
            if originals is None or not binding.get("opencode_schema_normalization"):
                raise Drift("native configuration/resource drift")
            before, after = json.loads(originals[binding["path"]]), json.loads(data)
            if (
                "$schema" not in before
                and after.get("$schema") == "https://opencode.ai/config.json"
            ):
                after.pop("$schema")
            if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
                raise Drift("native configuration/resource drift")
    for path in probe.get("absent_files", []):
        if os.path.lexists(os.path.expanduser(path)):
            raise Drift("unexpected native configuration file")
    return observed


def version(command: list[str], cwd: Path, env: dict[str, str], expected: str) -> None:
    with ControlProcess(
        [*command, "--version"], cwd, env, credential_consumer=False
    ) as process:
        text = process.line().strip()
    if expected not in text.split():
        raise Drift("native executable version drift")


def _get(port: int, path: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path)
        result = connection.getresponse()
        body = result.read(16 * 1024 * 1024 + 1)
        if result.status != 200 or len(body) > 16 * 1024 * 1024:
            raise ValueError("native metadata unavailable")
        return json.loads(body)
    finally:
        connection.close()


def opencode(probe: dict[str, Any], command: list[str], cwd: Path, env: dict[str, str]):
    # No config rewriting: use the very same global/project/resource layers.
    env = {
        **env,
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }
    identity = probe["identity"]
    if env.get(CUSTODY_ENV) == "required" or probe.get("refreshable_auth"):
        provider = opencode_model_metadata(
            command,
            cwd,
            env,
            provider=identity["provider_id"],
            model_id=identity["requested_model"].split("/", 1)[1],
            version=identity["harness_version"],
        )
        result = {"all": [provider]}
    else:
        with ControlProcess(
            [*command, "serve", "--hostname", "127.0.0.1", "--port", "0"], cwd, env
        ) as p:
            while not (match := re.search(r"http://127\.0\.0\.1:(\d+)", p.line())):
                pass
            result = _get(int(match[1]), "/provider")
    providers = [row for row in result["all"] if row["id"] == identity["provider_id"]]
    if len(providers) != 1:
        raise Drift("native provider disappeared")
    provider = providers[0]
    model_id = identity["requested_model"].split("/", 1)[1]
    model = provider["models"].get(model_id)
    if model is None:
        raise Drift("native model disappeared")
    return {
        "model": model["api"]["id"],
        "provider_id": provider["id"],
        "protocol": model["api"]["npm"],
        "endpoint": provider.get("options", {}).get("baseURL") or model["api"]["url"],
        "choices": model.get("variants"),
        "selected": probe["selected"],
        "selection_source": "native variant selector + effective variants",
    }


def codex(probe: dict[str, Any], command: list[str], cwd: Path, env: dict[str, str]):
    requested = probe["identity"]["requested_model"].split("/", 1)[1]
    args = [*command, "app-server", "-c", "model=" + json.dumps(requested)]
    if probe["selected"] is not None:
        args.extend(["-c", "model_reasoning_effort=" + json.dumps(probe["selected"])])
    with ControlProcess(args, cwd, env) as p:
        p.rpc(
            "initialize",
            {"clientInfo": {"name": "tetrabench-startup", "version": "1"}},
            1,
        )
        p.send({"method": "initialized", "params": {}})
        config = p.rpc("config/read", {"includeLayers": False}, 2)["config"]
        mode = None
        if probe["identity"].get("auth_mode") == "api_key":
            account = p.rpc("account/read", {"refreshToken": False}, 1000)
            if (account.get("account") or {}).get("type") != "apiKey":
                raise Drift("native Codex auth mode differs")
            mode = "api_key"
        rows = []
        cursor = None
        seen = set()
        for page in range(32):
            data = p.rpc(
                "model/list",
                {"cursor": cursor, "limit": 100, "includeHidden": True},
                page + 3,
            )
            rows.extend(data["data"])
            cursor = data["nextCursor"]
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise Drift("invalid native catalog pagination")
            seen.add(cursor)
        else:
            raise Drift("native catalog page limit")
    found = [row for row in rows if row["model"] == probe["identity"]["resolved_model"]]
    if len(found) != 1:
        raise Drift("native model disappeared")
    route = codex_route(
        config,
        version=probe["identity"]["harness_version"],
        requested_provider=probe["identity"]["requested_model"].split("/", 1)[0],
        observed_auth_mode=mode,
        environment=env,
    )
    return {
        "model": found[0]["model"],
        "provider_id": route["provider"],
        "protocol": route["protocol"],
        "endpoint": route["endpoint"],
        "route_provenance": route["provenance"],
        "choices": [
            row["reasoningEffort"] for row in found[0]["supportedReasoningEfforts"]
        ],
        "selected": config.get("model_reasoning_effort"),
        "selection_source": "native config/read after CLI overrides",
    }


def claude(probe: dict[str, Any], command: list[str], cwd: Path, env: dict[str, str]):
    args = [
        *command,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--model",
        probe["identity"]["requested_model"].split("/", 1)[1],
    ]
    if probe.get("settings_file"):
        args.extend(["--settings", os.path.expanduser(probe["settings_file"])])
    if probe.get("setting_sources") is not None:
        args.extend(["--setting-sources", probe["setting_sources"]])
    if probe["selected"] is not None:
        args.extend(["--effort", probe["selected"]])
    try:
        with ControlProcess(args, cwd, env) as p:
            data, settings = claude_control_metadata(p)
        metadata = claude_model_metadata(
            data,
            settings,
            requested=probe["identity"]["requested_model"].split("/", 1)[1],
            version=probe["identity"]["harness_version"],
            environment=env,
        )
    except ControlError:
        raise Drift(
            "native Claude applied settings unavailable, conflicting or restricted"
        ) from None
    selected = metadata["models"][0]
    return {
        "model": metadata["applied"]["model"],
        "protocol": "claude-native",
        "provider_id": data.get("account", {}).get("apiProvider", "unknown"),
        "endpoint": env.get("ANTHROPIC_BASE_URL", ""),
        "choices": selected.get("supportedEffortLevels"),
        "selected": metadata["applied"]["effort"],
        "settings_effort": metadata["settings_effort"],
        "selection_source": "native get_settings.applied",
        "claude_metadata": metadata,
    }


def seed_pi_catalog(probe: dict[str, Any], env: dict[str, str]) -> None:
    """Seed only an absent native models store, retaining original cache validators."""
    entry = probe.get("catalog_entry")
    if entry is None:
        return
    root = env.get("PI_CODING_AGENT_DIR")
    if not root or not Path(root).is_absolute():
        raise Drift("Pi startup requires the actual private native directory")
    data = json.dumps(
        {probe["identity"]["provider_id"]: entry}, allow_nan=False
    ).encode()
    if len(data) > 128 * 1024:
        raise Drift("native catalog seed exceeds limit")
    target = Path(root) / "models-store.json"
    try:
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        return  # Current native state is inspected, never silently replaced.
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def published_pi_entrypoint(command: list[str], env: dict[str, str]) -> Path:
    if len(command) == 1:
        selected = shutil.which(command[0], path=env.get("PATH"))
    elif len(command) == 2 and Path(command[0]).name == "node":
        selected = command[1]
    else:
        selected = None
    if not selected:
        raise Drift("select the published Pi package executable")
    target = Path(selected).resolve(strict=True)
    for root in target.parents:
        manifest = root / "package.json"
        if not manifest.is_file():
            continue
        package = json.loads(read_bounded(str(manifest)))
        if package.get("name") != "@earendil-works/pi-coding-agent":
            continue
        published = (root / package["bin"]["pi"]).resolve(strict=True)
        if package.get("version") != "0.85.1" or target != published:
            raise Drift("Pi startup requires the published bin.pi entrypoint")
        return target
    raise Drift("published Pi package metadata unavailable")


def pi(probe: dict[str, Any], command: list[str], cwd: Path, env: dict[str, str]):
    published_pi_entrypoint(command, env)
    seed_pi_catalog(probe, env)
    identity = probe["identity"]
    args = [
        *command,
        "--mode",
        "rpc",
        "--offline",
        "--no-session",
        "--provider",
        identity["provider_id"],
        "--model",
        identity["resolved_model"],
    ]
    if probe["selected"] is not None:
        args.extend(["--thinking", probe["selected"]])
    for flag in probe.get("pi_discovery_flags", []):
        args.append(flag)
    with ControlProcess(args, cwd, env) as process:
        process.send({"id": "metadata-state", "type": "get_state"})
        while True:
            response = json.loads(process.line())
            if response.get("type") in {"agent_start", "message_start"}:
                raise Drift("unexpected Pi model activity during startup inspection")
            if response.get("id") == "metadata-state":
                if response.get("success") is not True:
                    raise Drift("published Pi startup metadata unavailable")
                state = response["data"]
                break
    model = state.get("model")
    if not isinstance(model, dict):
        raise Drift("Pi model absent from actual published consumer")
    expected = probe["capture"]["model"]
    if model.get("headers"):
        raise Drift("Pi model-header routing was not captured")
    for key in (
        "id",
        "name",
        "provider",
        "api",
        "baseUrl",
        "reasoning",
        "thinkingLevelMap",
        "maxTokens",
        "contextWindow",
        "input",
        "cost",
        "samplingParams",
        "compat",
    ):
        left = model.get(key, {} if key == "compat" else None)
        right = expected.get(key, {} if key == "compat" else None)
        if json.dumps(left, sort_keys=True) != json.dumps(right, sort_keys=True):
            raise Drift("Pi native model/catalog metadata drift")
    return {
        "model": model["id"],
        "provider_id": model["provider"],
        "protocol": model["api"],
        "endpoint": model["baseUrl"],
        "choices": None,
        "selected": state.get("thinkingLevel"),
        "selection_source": "published pi RPC get_state; no prompt submitted",
    }


def validate(probe: dict[str, Any], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    originals = verify_files(probe)
    command = probe["command"]
    identity = probe["identity"]
    version(command, cwd, env, identity["harness_version"])
    verified = ["native_version", "configuration_files", "sealed_resources"]
    unknown = []
    observation = None
    probe = {
        **probe,
        "metadata_allowed": probe.get("metadata_allowed", True)
        and passive_configuration(probe, cwd, env),
    }
    if identity["harness"] == "pi" and not probe["metadata_allowed"]:
        raise Drift(
            "published Pi startup verification unavailable for executable config"
        )
    if probe.get("metadata_allowed", True):
        try:
            observation = {
                "opencode": opencode,
                "codex": codex,
                "claude-code": claude,
                "pi": pi,
            }[identity["harness"]](probe, command, cwd, env)
        except CredentialCompletionError:
            raise Drift("native credential consumer did not exit gracefully") from None
        except Drift:
            raise
        except Exception:
            if (
                env.get(CUSTODY_ENV) == "required"
                or probe.get("refreshable_auth")
                or identity["harness"] == "pi"
            ):
                raise Drift(
                    "native authenticated metadata completion uncertain"
                ) from None
            unknown.extend(["native_model", "native_choices", "endpoint", "protocol"])
    else:
        unknown.extend(["native_model", "native_choices", "endpoint", "protocol"])
    if observation is not None:
        if observation["model"] != identity["resolved_model"]:
            raise Drift("native model drift")
        verified.append("native_model")
        if (
            observation["provider_id"] == "unknown"
            or identity["provider_id"] == "unknown"
        ):
            unknown.append("native_provider")
        elif observation["provider_id"] != identity["provider_id"]:
            raise Drift("native provider route drift")
        else:
            verified.append("native_provider")
        selected = probe["selected"]
        choices = observation["choices"]
        if selected is not None:
            if identity["harness"] == "pi":
                if observation["selected"] != selected:
                    raise Drift("published Pi thinking level clamped or changed")
                verified.append("native_selection")
            elif choices is None:
                unknown.append("native_choices")
            elif selected not in choices:
                raise Drift("selected native choice disappeared")
            else:
                if observation["selected"] is None:
                    unknown.append("effective_selection")
                elif observation["selected"] != selected:
                    raise Drift("native selected option differs")
                if (
                    isinstance(choices, dict)
                    and choices[selected] != probe["native_value"]
                ):
                    raise Drift("native variant meaning changed")
                verified.append("native_selection")
        for field, expected in (
            ("endpoint", identity["endpoints"]),
            ("protocol", identity["protocol"]),
        ):
            actual = observation[field]
            if (
                identity["harness"] == "opencode"
                and field == "endpoint"
                and bool(actual) != bool(expected)
            ):
                # Adding/removing an explicit endpoint changes the native route,
                # including a previously undisclosed built-in SDK default.
                raise Drift("native route drift")
            if (
                not actual
                or not expected
                or actual in {"unknown", "codex-native", "claude-native"}
            ):
                unknown.append(field)
            elif (
                actual not in expected
                if isinstance(expected, list)
                else actual != expected
            ):
                raise Drift("native route drift")
            else:
                verified.append(field)
    if unknown and probe["policy"] == "strict-startup":
        raise Drift("strict startup metadata remains unverified")
    after = verify_files(probe, originals)
    return {
        "schema_version": 1,
        "scope": "native-startup",
        "policy": probe["policy"],
        "snapshot_sha256": probe["snapshot_sha256"],
        "verified_fields": verified,
        "unverified_fields": unknown,
        "selected": probe["selected"],
        "observed_selection": observation["selected"] if observation else None,
        "native_settings_effort": observation.get("settings_effort")
        if observation
        else None,
        **(
            {"claude_metadata": observation["claude_metadata"]}
            if observation and "claude_metadata" in observation
            else {}
        ),
        "native_normalizations": ["opencode_schema_annotation"]
        if after != originals
        else [],
        "selection_source": observation["selection_source"]
        if observation
        else "captured native evidence only",
        "metadata_attempted": probe.get("metadata_allowed", True),
        "metadata_complete": observation is not None,
        "route_provenance": observation.get("route_provenance")
        if observation
        else None,
        "inference_validated": False,
        "future_dispatch_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("--files-only", action="store_true")
    parser.add_argument("--isolated-network", action="store_true")
    args = parser.parse_args()
    try:
        if args.isolated_network:
            import fcntl
            import socket
            import struct

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                fcntl.ioctl(sock, 0x8914, struct.pack("16sH14s", b"lo", 1, b""))
        probe = json.loads(read_bounded(args.request))
        if args.files_only:
            probe["metadata_allowed"] = False
        result = validate(probe, Path.cwd(), dict(os.environ))
        if os.environ.get(CUSTODY_ENV) == "required":
            result["auth_custody"] = auth_custody_report()
        print(json.dumps(result))
    except Exception as error:
        text = (
            str(error)
            if isinstance(error, Drift)
            else "native startup capability drift or unavailable strict evidence"
        )
        failure: dict[str, Any] = {"error": text}
        if os.environ.get(CUSTODY_ENV) == "required":
            failure["auth_custody"] = auth_custody_report()
        print(json.dumps(failure))
        raise SystemExit(78) from None


if __name__ == "__main__":
    main()
