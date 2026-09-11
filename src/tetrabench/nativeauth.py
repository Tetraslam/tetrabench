"""Thin adapters to pinned native auth commands and credential formats.

Sources (exact tags): openai/codex rust-v0.154.0 login/src/auth/storage.rs;
anomalyco/opencode v1.18.30 auth/index.ts, cli/cmd/providers.ts and
plugin/openai/codex.ts; earendil-works/pi v0.85.1 core/auth-storage.ts and
core/model-runtime.ts. Claude setup-token is documented at
https://code.claude.com/docs/en/authentication#generate-a-long-lived-token.
No OAuth exchange, refresh HTTP request, or cross-harness credential translation
belongs here. Native output is private and never returned verbatim.
"""

from __future__ import annotations

import base64
import os
import re
import selectors
import signal
import subprocess  # nosec B404
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tetrabench.auth_config import (
    NATIVE_AUTH_PINS,
    AuthMode,
    authentication_environment_names,
    credential_env_name,
)
from tetrabench.auth_sessions import (
    MAX_AUTH_BYTES,
    AuthError,
    AuthStateError,
    private_directory,
    private_json,
)

MAX_NATIVE_OUTPUT = 64 * 1024


@dataclass(frozen=True)
class NativeAuthMetadata:
    harness: str
    mode: Literal["api_key", "chatgpt_oauth", "claude_setup_token", "none", "unknown"]
    source: Literal["native_status", "native_store", "user_reference"]
    expires_at_ms: int | None = None
    expiry_source: Literal["native_store", "unverified_token_claim", "unknown"] = (
        "unknown"
    )
    validity: Literal["present", "expired", "unknown", "absent"] = "unknown"


def _expiry(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _jwt_expiry(token: str) -> int | None:
    # Metadata only, NOT signature validation or proof of account authorization.
    try:
        encoded = token.split(".")[1]
        claims = private_json(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        seconds = _expiry(claims.get("exp"))
        return _expiry(seconds * 1000) if seconds is not None else None
    except (IndexError, ValueError, AuthStateError):
        return None


def inspect_native_store(
    harness: str, data: bytes, *, now_ms: int | None = None
) -> NativeAuthMetadata:
    """Read only the declared native shape; no commands, login, or refresh."""
    if len(data) > MAX_AUTH_BYTES:
        raise AuthStateError("native credential state exceeds size limit")
    value = private_json(data)
    expires: int | None = None
    expiry_source: Literal["native_store", "unverified_token_claim", "unknown"] = (
        "unknown"
    )
    if harness == "codex":
        tokens = value.get("tokens")
        if (
            value.get("auth_mode") not in (None, "chatgpt")
            or value.get("OPENAI_API_KEY") is not None
            or any(
                value.get(k) is not None
                for k in (
                    "agent_identity",
                    "personal_access_token",
                    "bedrock_api_key",
                    "bedrock_access_keys",
                )
            )
            or not isinstance(tokens, dict)
            or not all(
                isinstance(tokens.get(k), str) and tokens[k]
                for k in ("access_token", "refresh_token", "id_token")
            )
        ):
            raise AuthStateError("Codex session must contain native ChatGPT OAuth only")
        expires = _jwt_expiry(tokens["access_token"])
        expiry_source = "unverified_token_claim" if expires is not None else "unknown"
    elif harness in {"opencode", "pi"}:
        provider = "openai" if harness == "opencode" else "openai-codex"
        token = value.get(provider)
        if (
            set(value) != {provider}
            or not isinstance(token, dict)
            or token.get("type") != "oauth"
            or not all(
                isinstance(token.get(k), str) and token[k]
                for k in ("access", "refresh")
            )
            or _expiry(token.get("expires")) is None
            or "key" in token
        ):
            raise AuthStateError(
                "native session must contain only the selected Codex OAuth provider"
            )
        expires = token["expires"]
        expiry_source = "native_store"
    else:
        raise AuthStateError("this harness has no supported refreshable native store")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    return NativeAuthMetadata(
        harness,
        "chatgpt_oauth",
        "native_store",
        expires,
        expiry_source,
        "expired" if expires is not None and expires <= now else "present",
    )


def assert_auth_outside_artifacts(
    auth_root: Path, artifact_roots: Sequence[Path]
) -> None:
    """Reject overlapping collection roots, including symlink aliases."""
    auth = auth_root.resolve()
    for root in artifact_roots:
        other = root.resolve()
        if auth == other or auth.is_relative_to(other) or other.is_relative_to(auth):
            raise AuthStateError(
                "private auth authority/runtime overlaps collected artifacts",
                reason="auth-output-overlap",
            )


def isolated_auth_environment(
    harness: str, root: Path, *, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Allowlist process infrastructure, never inherit ambient auth or config."""
    private_directory(root)
    allowed = {"PATH", "LANG", "LC_ALL", "TERM", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    source = base or {}
    environment = {key: value for key, value in source.items() if key in allowed}
    environment.setdefault("PATH", os.defpath)
    for key, relative in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("TMPDIR", "tmp"),
    ):
        environment[key] = str(private_directory(root / relative, create=True))
    native = private_directory(root / "native", create=True)
    if harness == "codex":
        environment["CODEX_HOME"] = str(native)
    elif harness == "claude-code":
        environment["CLAUDE_CONFIG_DIR"] = str(native)
        environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    elif harness == "opencode":
        private_directory(root / "data" / "opencode", create=True)
        environment["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        environment["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    elif harness == "pi":
        environment["PI_CODING_AGENT_DIR"] = str(native)
        environment["PI_OFFLINE"] = "1"
    else:
        raise AuthStateError("unsupported native auth harness")
    from tetrabench.native_execution import native_process_environment

    return native_process_environment(environment)


def refuse_ambient_conflicts(
    harness: str,
    mode: AuthMode,
    environment: Mapping[str, str],
    *,
    source_name: str | None,
    model: str | None = None,
) -> None:
    """Reject competing inputs before isolation; env presence is not auth status."""
    destination = (
        credential_env_name(harness, mode, model=model)
        if mode != "chatgpt_oauth"
        else None
    )
    present = {
        name for name in authentication_environment_names() if environment.get(name)
    }
    permitted = {source_name} if source_name else set()
    # A destination different from the reference is a second authority, even if
    # it happens to have the same current bytes.
    if present - permitted or (destination in present and destination != source_name):
        raise AuthError(
            "mixed authentication inputs; unset competing native credentials/config"
        )


def native_auth_path(harness: str, root: Path) -> Path:
    if harness == "opencode":
        return root / "data" / "opencode" / "auth.json"
    if harness in {"codex", "pi"}:
        return root / "native" / "auth.json"
    raise AuthStateError(
        "Claude setup-token is an environment reference, not a copied keychain"
    )


@dataclass(frozen=True, repr=False)
class NativeResult:
    returncode: int
    output: bytes = field(default=b"", repr=False)
    stdout: bytes | None = field(default=None, repr=False)
    stderr: bytes | None = field(default=None, repr=False)


def run_native(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
    stdin: bytes | None = None,
    interactive: bool = False,
    timeout: float = 60,
) -> NativeResult:
    """Run without a shell, bound private output, reap the process group.

    Login interaction goes to a controlling terminal, never an artifact/log
    capture. Cancellation interrupts native work before state is handed back.
    Uncatchable controller death leaves the durable claim blocked.
    """
    if (
        not argv
        or timeout <= 0
        or (interactive and stdin is not None)
        or (stdin is not None and len(stdin) > MAX_AUTH_BYTES)
    ):
        raise AuthError("invalid native auth command")
    terminal = None
    process = None
    from tetrabench.auth_operations import current_cli_operation

    operation = current_cli_operation()
    try:
        if interactive:
            terminal = open("/dev/tty", "r+b", buffering=0)
        if operation is not None:
            operation.launching()
        process = subprocess.Popen(  # nosec B603
            list(argv),
            env=dict(environment),
            cwd=cwd,
            stdin=terminal if interactive else subprocess.PIPE,
            stdout=terminal if interactive else subprocess.PIPE,
            stderr=terminal if interactive else subprocess.PIPE,
            start_new_session=True,
            umask=0o077,
        )
        if operation is not None:
            operation.started(process.pid)
        output = bytearray()
        stdout = bytearray()
        stderr = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            pending = memoryview(stdin or b"")
            if not interactive:
                if process.stdin is None:
                    raise AuthError("native credential input pipe is unavailable")
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                else:
                    process.stdin.close()
            if process.stdout:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            if process.stderr:
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while process.poll() is None or selector.get_map():
                if time.monotonic() >= deadline:
                    raise AuthError(
                        "native auth command timed out; profile may require reseed"
                    )
                if not selector.get_map():
                    time.sleep(0.02)
                    continue
                for key, _ in selector.select(timeout=0.05):
                    if key.events == selectors.EVENT_WRITE:
                        try:
                            written = os.write(key.fd, pending[:4096])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(key.fileobj)
                            if process.stdin is None:
                                raise AuthError(
                                    "native credential input pipe disappeared"
                                )
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        (stdout if key.data == "stdout" else stderr).extend(chunk)
                        if len(output) > MAX_NATIVE_OUTPUT:
                            raise AuthError(
                                "native auth output exceeded private capture limit"
                            )
        return NativeResult(process.wait(), bytes(output), bytes(stdout), bytes(stderr))
    except OSError:
        raise AuthError("native auth executable/terminal unavailable") from None
    finally:
        if process is not None:
            # Reap any same-session descendants even when the leader exited.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            if process.stdin:
                process.stdin.close()
            if operation is not None:
                operation.reaped(process.pid, process.returncode)
        if terminal is not None:
            terminal.close()


# Imported from the installed, exact-version coding-agent package. ModelRuntime
# owns login, refresh locking and AuthStorage writeback. No provider HTTP here.
PI_AUTH_BRIDGE = r"""
import {pathToFileURL} from "node:url";
import {createInterface} from "node:readline/promises";
try {
  const moduleUrl = pathToFileURL(process.env.TETRABENCH_PI_MODULE);
  const {readStoredCredential, ModelRuntime} = await import(moduleUrl);
  const authPath = process.env.PI_CODING_AGENT_DIR + "/auth.json";
  const runtime = await ModelRuntime.create({authPath, modelsPath: null,
    allowModelNetwork: false, refreshOnCreate: false});
  const action = process.argv[1];
  const provider = process.env.TETRABENCH_AUTH_PROVIDER ?? "openai-codex";
  if (action === "login") {
    const rl = createInterface({input: process.stdin, output: process.stdout});
    try {
      await runtime.login(provider, "oauth", {
        prompt: async p => {
          if (p.type === "select") {
            for (const o of p.options) console.log(o.id + ": " + o.label);
          }
          return await rl.question(p.message + " ", {signal: p.signal});
        },
        notify: e => {
          if (e.type === "auth_url") console.log(e.url, e.instructions ?? "");
          else if (e.type === "device_code") console.log(e.verificationUri, e.userCode);
          else console.log(e.message);
        }
      });
    } finally {rl.close();}
  } else if (action === "logout") {
    await runtime.logout(provider);
  } else if (action === "refresh") {
    const current = readStoredCredential(provider, authPath);
    if (current?.type !== "oauth") throw new Error();
    await runtime.getAuth(provider);
  } else if (action !== "status") throw new Error();
  const status = await runtime.checkAuth(provider);
  console.log(JSON.stringify({type: status?.type ?? "none"}));
} catch { console.error("native Pi auth failed"); process.exitCode = 1; }
"""


def native_auth_command(
    harness: str,
    action: Literal["login", "status", "logout", "refresh"],
    *,
    executable: str,
    mode: AuthMode = "chatgpt_oauth",
    device_auth: bool = True,
) -> list[str]:
    if mode == "claude_setup_token" and harness != "claude-code":
        raise AuthError("Claude subscription auth is restricted to Claude Code")
    if mode == "api_key" and harness in {"opencode", "pi"} and action != "status":
        raise AuthError("API keys are user-managed environment references")
    if mode == "chatgpt_oauth" and harness == "claude-code":
        raise AuthError("Claude Code does not accept Codex OAuth")
    if harness == "codex":
        prefix = [executable, "-c", 'cli_auth_credentials_store="file"']
        if action == "login":
            return (
                prefix
                + ["login"]
                + (
                    ["--with-api-key"]
                    if mode == "api_key"
                    else ["--device-auth"]
                    if device_auth
                    else []
                )
            )
        if action == "status":
            return [*prefix, "login", "status"]
        if action == "logout":
            return [*prefix, "logout"]
        raise AuthError(
            "Codex refresh is native request-time behavior, not a separate command"
        )
    if harness == "claude-code":
        if action == "status":
            return [executable, "auth", "status", "--json"]
        if action == "login" and mode == "claude_setup_token":
            return [executable, "setup-token"]
        if action == "logout":
            return [executable, "auth", "logout"]
        raise AuthError(
            "Claude API keys/setup tokens are user-managed environment references"
        )
    if harness == "opencode":
        if action == "login":
            method = "headless" if device_auth else "browser"
            return [
                executable,
                "auth",
                "login",
                "--provider",
                "openai",
                "--method",
                f"ChatGPT Pro/Plus ({method})",
            ]
        if action == "status":
            return [executable, "auth", "list"]
        if action == "logout":
            return [executable, "auth", "logout", "openai"]
        raise AuthError("OpenCode refresh is owned by its request-time Codex plugin")
    if harness == "pi":
        return [executable, "--input-type=module", "-e", PI_AUTH_BRIDGE, action]
    raise AuthError("unsupported native auth harness")


def verify_native_version(harness: str, output: bytes) -> None:
    versions = re.findall(rb"(?<![0-9.])[0-9]+\.[0-9]+\.[0-9]+(?![0-9.])", output)
    if versions != [NATIVE_AUTH_PINS[harness].encode()]:
        raise AuthError("native auth executable does not match the verified version")


_RUST_DEBUG_PATH = rb'"(?:[^"\\\x00-\x1f\x7f]|\\[ -~])*"'
_CODEX_TEMP_WARNING = re.compile(
    rb"WARNING: proceeding, even though we could not create PATH aliases: "
    rb"Refusing to create helper binaries under temporary dir "
    + _RUST_DEBUG_PATH
    + rb" \(codex_home: AbsolutePathBuf\("
    + _RUST_DEBUG_PATH
    + rb"\)\)"
)
_CODEX_API_STATUS = re.compile(
    rb"Logged in using an API key - (?:\*\*\*|[!-~]{8}\*\*\*[!-~]{5})"
)


def _codex_status(
    result: NativeResult,
) -> Literal["api_key", "chatgpt_oauth", "unknown"]:
    """Codex rust-v0.154.0 cli/login.rs + arg0/lib.rs output grammar.

    login status uses stderr. Legacy single-stream inputs remain parseable, but
    real executors supply both channels; stdout is never an alternate authority.
    Warning paths and safe_format_key's fragments are not retained in metadata.
    """
    if result.stdout is not None or result.stderr is not None:
        if result.stdout is None or result.stderr is None or result.stdout.strip():
            return "unknown"
        data = result.stderr
    else:
        data = result.output
    lines = [line.removesuffix(b"\r") for line in data.split(b"\n") if line]
    if len(lines) == 2 and _CODEX_TEMP_WARNING.fullmatch(lines[0]):
        lines = lines[1:]
    if len(lines) != 1:
        return "unknown"
    if lines[0] == b"Logged in using ChatGPT":
        return "chatgpt_oauth"
    if _CODEX_API_STATUS.fullmatch(lines[0]):
        return "api_key"
    return "unknown"


def parse_native_status(
    harness: str, result: NativeResult, *, model: str | None = None
) -> NativeAuthMetadata:
    """Recognize native mode, never echo key suffixes, email, or account IDs."""
    channel_bytes = len(result.stdout or b"") + len(result.stderr or b"")
    if max(len(result.output), channel_bytes) > MAX_NATIVE_OUTPUT:
        raise AuthError("native auth status exceeded output limit")
    if result.returncode != 0:
        return NativeAuthMetadata(harness, "none", "native_status", validity="absent")
    data = result.stdout if result.stdout is not None else result.output
    mode: Literal[
        "api_key", "chatgpt_oauth", "claude_setup_token", "none", "unknown"
    ] = "unknown"
    if harness == "codex":
        mode = _codex_status(result)
    elif harness == "claude-code":
        value = private_json(data)
        method = value.get("authMethod")
        if value.get("loggedIn") is True and method == "api_key":
            mode = "api_key"
        elif value.get("loggedIn") is True and method == "oauth_token":
            mode = "claude_setup_token"
    elif harness == "pi":
        native_type = private_json(data).get("type")
        if native_type == "oauth":
            mode = "chatgpt_oauth"
        elif native_type == "api_key":
            mode = "api_key"
    elif harness == "opencode":
        text = re.sub(rb"\x1b\[[0-9;]*[a-zA-Z]", b"", data)
        if re.search(rb"\bOpenAI\s+oauth\b", text):
            mode = "chatgpt_oauth"
        elif model is not None:
            expected = credential_env_name(harness, "api_key", model=model).encode()
            # The CLI enumerates env names recognized by its native providers.
            if re.search(rb"\b" + re.escape(expected) + rb"\b", text):
                mode = "api_key"
    return NativeAuthMetadata(
        harness,
        mode,
        "native_status",
        validity="present" if mode != "unknown" else "unknown",
    )
