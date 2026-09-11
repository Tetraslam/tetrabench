"""Explicit native renewal, separate from the metadata-only control API."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from tetrabench.auth_operations import current_cli_operation
from tetrabench.auth_sessions import AuthError, private_json, read_private
from tetrabench.nativeauth import (
    NativeAuthMetadata,
    NativeStopError,
    inspect_native_store,
    native_auth_command,
    run_native,
)

if TYPE_CHECKING:
    from tetrabench.auth import NativeRuntime


def containment_command(environment: Mapping[str, str]) -> list[str]:
    unshare = shutil.which("unshare", path=environment.get("PATH", os.defpath))
    if unshare is None:
        raise AuthError("native renewal requires Linux PID namespace containment")
    return [
        unshare,
        "--user",
        "--map-root-user",
        "--pid",
        "--fork",
        "--kill-child",
        sys.executable,
        "-I",
        str(Path(__file__).with_name("native_refresh_supervisor.py")),
    ]


def contained_command(
    argv: Sequence[str], environment: Mapping[str, str], timeout: float
) -> list[str]:
    return [*containment_command(environment), "--timeout", str(timeout), "--", *argv]


def check_containment(environment: Mapping[str, str], cwd: Path) -> None:
    """Trusted credential-free probe, before declaring renewal attempted."""
    result = run_native(
        [*containment_command(environment), "--check"],
        environment={"PATH": environment.get("PATH", os.defpath)},
        cwd=cwd,
        timeout=5,
    )
    if result.returncode:
        raise AuthError("native renewal PID namespace containment is unavailable")


def _message(value: dict) -> bytes:
    return (json.dumps(value) + "\n").encode()


class CodexRefreshProtocol:
    """Pinned 0.154.0 managed-auth handshake; no caller-selected RPCs."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected = 1
        self.complete = False

    def start(self) -> bytes:
        return _message(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "tetrabench-renewal", "version": "1"},
                },
            }
        )

    def receive(self, data: bytes) -> bytes:
        self._buffer.extend(data)
        outgoing = b""
        while b"\n" in self._buffer:
            line, _, remaining = self._buffer.partition(b"\n")
            self._buffer = bytearray(remaining)
            value = private_json(bytes(line))
            if "id" not in value:
                continue  # Native notifications are private, not refresh evidence.
            if (
                self.complete
                or type(value["id"]) is not int
                or value["id"] != self._expected
                or "error" in value
                or not isinstance(value.get("result"), dict)
            ):
                raise AuthError("native renewal protocol rejected")
            if self._expected == 1:
                self._expected = 2
                outgoing += _message({"method": "initialized", "params": {}})
                outgoing += _message(
                    {
                        "id": 2,
                        "method": "account/read",
                        "params": {"refreshToken": True},
                    }
                )
            else:
                account = value["result"].get("account")
                if not isinstance(account, dict) or account.get("type") != "chatgpt":
                    raise AuthError("native renewal requires managed ChatGPT auth")
                # Codex can suppress refresh errors. This is only protocol completion;
                # the caller must separately compare the native access token.
                self.complete = True
        return outgoing


def access_token(harness: str, native: bytes) -> str:
    inspect_native_store(harness, native)
    value = private_json(native)
    if harness == "codex":
        return value["tokens"]["access_token"]
    return value["openai" if harness == "opencode" else "openai-codex"]["access"]


def require_renewed(harness: str, before: bytes, after: bytes) -> NativeAuthMetadata:
    metadata = inspect_native_store(harness, after)
    if (
        access_token(harness, before) == access_token(harness, after)
        or metadata.expires_at_ms is None
        or metadata.expires_at_ms <= int(time.time() * 1000)
    ):
        raise AuthError(
            "native renewal inconclusive: changed token and fresh expiry required"
        )
    return metadata


def refresh_native(
    runtime: NativeRuntime, *, timeout: float = 60, prompt: str | None = None
) -> NativeAuthMetadata:
    """Renew once under the existing claim. OpenCode requires an explicit prompt.

    No expiry injection here. Pi/OpenCode with fresh credentials can be a no-op;
    unchanged access tokens deliberately fail this explicit renewal operation.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise AuthError("native renewal requires a finite positive timeout")
    operation = current_cli_operation()
    if (
        runtime.claim is None
        or runtime.claim.closed
        or operation is None
        or operation.record.owner != runtime.claim.snapshot.state.owner
        or runtime.spec.mode != "chatgpt_oauth"
    ):
        raise AuthError(
            "native renewal requires the owning CLI lifetime and OAuth claim"
        )
    if runtime.harness == "codex":
        argv = [
            runtime.executable,
            "-c",
            'cli_auth_credentials_store="file"',
            "app-server",
        ]
    elif runtime.harness == "opencode":
        if not prompt or not runtime.model or len(prompt.encode()) > 4096:
            raise AuthError(
                "OpenCode renewal requires an explicit bounded model prompt"
            )
        argv = [
            runtime.executable,
            "run",
            "--model",
            runtime.model,
            "--format",
            "json",
            "--",
            prompt,
        ]
    elif runtime.harness == "pi":
        argv = native_auth_command("pi", "refresh", executable=runtime.executable)
    else:
        raise AuthError("unsupported native renewal harness")
    if not runtime._consumer_lock.acquire(blocking=False):
        raise AuthError("native credential session already has a consumer")
    try:
        if not runtime._stopped or runtime._external or runtime._ambiguous:
            raise AuthError("native credential session is not reusable")
        check_containment(runtime.environment, runtime.root)
        before = read_private(runtime.credential_path)
        # Poison before launching, including ordinary errors and caught assertions.
        # Only a proven native completion AND acknowledged checkpoint clear this.
        runtime._ambiguous = True
        runtime._stopped = False
        try:
            run_native(
                argv,
                environment=runtime.environment,
                cwd=runtime.root,
                timeout=timeout,
                require_natural_completion=True,
                codex_refresh=runtime.harness == "codex",
            )
        except NativeStopError:
            raise
        except BaseException:
            runtime._stopped = True
            raise
        else:
            runtime._stopped = True
        after = read_private(runtime.credential_path)
        metadata = require_renewed(runtime.harness, before, after)
        runtime.claim.checkpoint(after)
        runtime._ambiguous = False
        return metadata
    finally:
        runtime._consumer_lock.release()
