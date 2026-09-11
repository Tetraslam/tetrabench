"""Private control-only collector subprocess, normally in a new PID/net namespace.

No user/session prompt messages are implemented. The parent owns authorization,
input sealing, namespace lifetime, and the sole secret-free result channel.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import signal
import socket
import struct
import sys
from pathlib import Path
from typing import Any

import tomlkit

from tetrabench.capabilities import MetadataError, metadata_text, parse_metadata
from tetrabench.native_control import ControlProcess, select_model_info

MAX_NATIVE_BYTES = 16 * 1024 * 1024


def _loopback_up() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        interface = struct.pack("16sH14s", b"lo", 1, b"")
        fcntl.ioctl(sock, 0x8914, interface)  # SIOCSIFFLAGS, IFF_UP.


def _http_json(port: int, path: str) -> dict[str, Any]:
    if path not in {"/provider", "/config/providers"}:
        raise MetadataError("non-metadata HTTP path rejected")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        data = response.read(MAX_NATIVE_BYTES + 1)
        if response.status != 200 or len(data) > MAX_NATIVE_BYTES:
            raise MetadataError("native metadata HTTP failed or exceeded limit")
        # The global native catalog can exceed the immutable snapshot limit.
        # Only the selected row is returned and passed through the bounded parser.
        return json.loads(data)
    finally:
        connection.close()


def _opencode(
    request: dict[str, Any], root: Path, env: dict[str, str]
) -> dict[str, Any]:
    # Execution registers the main config globally, below OPENCODE_CONFIG_DIR.
    # CONFIG_CONTENT would instead override resource layers at native priority.
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    directory = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "opencode.json").write_text(metadata_text(request["native"]))
    env["OPENCODE_FAKE_VCS"] = "git"
    if request.get("discovery") == "isolated":
        env.update(
            OPENCODE_DISABLE_PROJECT_CONFIG="1",
            OPENCODE_DISABLE_CLAUDE_CODE="1",
            OPENCODE_DISABLE_EXTERNAL_SKILLS="1",
        )
    if request.get("config_directory"):
        env["OPENCODE_CONFIG_DIR"] = request["config_directory"]
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    # Built-ins are required for real auth routes, but may execute auth loaders.
    if not request["authenticated"]:
        env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
    if request.get("cache"):
        env["OPENCODE_MODELS_PATH"] = request["cache"]
    with ControlProcess(
        [*request["command"], "serve", "--hostname", "127.0.0.1", "--port", "0"],
        root,
        env,
    ) as process:
        import re

        while True:
            match = re.search(r"http://127\.0\.0\.1:(\d+)", process.line())
            if match:
                port = int(match[1])
                break
        response = _http_json(port, "/provider")
        provider = next(p for p in response["all"] if p["id"] == request["provider"])
        model = provider["models"][request["model"]]
        base_url = provider.get("options", {}).get("baseURL") or model["api"]["url"]
        projection = {
            key: model[key]
            for key in ("id", "providerID", "api", "capabilities", "variants")
            if key in model
        }
        return {
            "payload": {
                "all": [
                    {
                        "id": provider["id"],
                        "options": {"baseURL": base_url},
                        "models": {
                            request["model"]: projection,
                        },
                    }
                ]
            },
            "route": {
                "model": model["api"]["id"],
                "provider": provider["id"],
                "protocol": model["api"]["npm"],
                "endpoint": base_url,
            },
            "method": "OpenCode serve GET /provider; no session created",
        }


def _codex(request: dict[str, Any], root: Path, env: dict[str, str]) -> dict[str, Any]:
    home = Path(env["CODEX_HOME"])
    home.mkdir(exist_ok=True)
    native = dict(request["native"])
    native["model"] = request["model"]
    (home / "config.toml").write_text(tomlkit.dumps(native))
    if request.get("cache"):
        (home / "models_cache.json").write_bytes(Path(request["cache"]).read_bytes())
    with ControlProcess([*request["command"], "app-server"], root, env) as process:
        initialized = process.rpc(
            "initialize",
            {
                "clientInfo": {"name": "tetrabench-discovery", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
            1,
        )
        process.send({"method": "initialized", "params": {}})
        config = process.rpc("config/read", {"includeLayers": False}, 2)["config"]
        rows: list[Any] = []
        cursor = None
        seen = set()
        for page in range(32):
            data = process.rpc(
                "model/list",
                {
                    "cursor": cursor,
                    "limit": 100,
                    "includeHidden": True,
                },
                page + 3,
            )
            rows.extend(data["data"])
            cursor = data["nextCursor"]
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise MetadataError("native model/list repeated cursor")
            seen.add(cursor)
        else:
            raise MetadataError("native model/list page limit exceeded")
        selected = next(row for row in rows if row["model"] == request["model"])
        provider_id = config.get("model_provider") or "unknown"
        provider = (config.get("model_providers") or {}).get(provider_id, {})
        # Native config/read does not always expose built-in endpoint definitions.
        return {
            "payload": {"data": [selected], "nextCursor": None},
            "route": {
                "model": selected["model"],
                "provider": provider_id,
                "protocol": provider.get("wire_api", "codex-native"),
                "endpoint": provider.get("base_url", ""),
            },
            "method": "Codex initialize + config/read + model/list; EOF shutdown",
            "native_server": initialized.get("userAgent", ""),
        }


def _claude(request: dict[str, Any], root: Path, env: dict[str, str]) -> dict[str, Any]:
    native = dict(request["native"])
    if not request["allow_config_execution"]:
        native["disableAllHooks"] = True
    settings = root / "settings.json"
    settings.write_text(metadata_text(native))
    command = [
        *request["command"],
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "",
        "--settings",
        str(settings),
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--model",
        request["model"],
    ]
    with ControlProcess(command, root, env) as process:
        # SDK 0.3.267 supportedModels() reads this initialization response.
        # No user message, empty-string prompt, or agent initialPrompt is sent.
        process.send(
            {
                "type": "control_request",
                "request_id": "metadata-init",
                "request": {
                    "subtype": "initialize",
                    "hooks": {},
                    "sdkMcpServers": [],
                    "agents": {},
                    "plugins": [],
                },
            }
        )
        while True:
            message = parse_metadata(process.line())
            if message.get("type") != "control_response":
                if message.get("type") in {"assistant", "result"}:
                    raise MetadataError(
                        "unexpected model/run output during metadata discovery"
                    )
                continue
            response = message["response"]
            if response.get("request_id") == "metadata-init":
                if response.get("subtype") != "success":
                    raise MetadataError("Claude native initialization unavailable")
                data = response["response"]
                break
        selected = select_model_info(data["models"], request["model"], request["model"])
        projection = {
            key: selected[key]
            for key in (
                "value",
                "resolvedModel",
                "supportsEffort",
                "supportedEffortLevels",
                "supportsAdaptiveThinking",
            )
            if key in selected
        }
        return {
            "payload": [projection],
            "route": {
                "model": selected.get("resolvedModel", request["model"]),
                "provider": data.get("account", {}).get("apiProvider", "unknown"),
                "protocol": "claude-native",
                "endpoint": env.get("ANTHROPIC_BASE_URL", ""),
            },
            "method": "Claude SDK control initialize.models; no input; EOF shutdown",
        }


def _pi(request: dict[str, Any], root: Path, env: dict[str, str]) -> dict[str, Any]:
    models = request["native"].get("models", {"providers": {}})
    path = root / "models.json"
    path.write_text(metadata_text(models))
    helper = Path(__file__).with_name("native_reasoning.mjs")
    with ControlProcess(
        [
            request["node"],
            "--experimental-import-meta-resolve",
            str(helper),
            "collect-pi",
        ],
        root,
        env,
    ) as process:
        process.send({**request, "models_path": str(path)})
        result = parse_metadata(process.line())
        if "unavailable" in result:
            raise MetadataError(result["unavailable"])
        model = result["model"]
        return {
            "payload": result,
            "route": {
                "model": model["id"],
                "provider": model["provider"],
                "protocol": model["api"],
                "endpoint": model["baseUrl"],
            },
            "method": "Pi ModelRuntime.create(refreshOnCreate=false), cache restore, "
            "getModel + SDK getSupportedThinkingLevels/clampThinkingLevel",
        }


def main() -> None:
    # PID 1 ignores SIGTERM by default; restore explicit bounded parent shutdown.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        request = parse_metadata(sys.stdin.buffer.read(2 * 1024 * 1024 + 1))
        if request["network_isolated"]:
            _loopback_up()
        root = Path(request["work"])
        env = dict(os.environ)
        collector = {
            "opencode": _opencode,
            "codex": _codex,
            "claude-code": _claude,
            "pi": _pi,
        }[request["harness"]]
        result = collector(request, root, env)
        print(metadata_text(result), flush=True)
    except Exception as error:
        # Raw native messages can contain credentials. Emit no upstream exception.
        known = {
            "Pi metadata-only initialization failed at " + stage
            for stage in (
                "input",
                "package-pin",
                "import-sdk",
                "create-runtime",
                "native-cache-restore",
                "model-lookup",
            )
        }
        code = (
            str(error)
            if isinstance(error, MetadataError) and str(error) in known
            else type(error).__name__
        )
        if isinstance(error, StopIteration):
            code = "selected model absent from native catalog"
        print(metadata_text({"unavailable": code}), flush=True)
        sys.exit(143)


if __name__ == "__main__":
    main()
