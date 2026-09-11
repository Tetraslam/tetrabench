"""Bounded native metadata transport, also shipped into dependency-free sandboxes."""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import Any

CUSTODY_ENV = "TETRABENCH_NATIVE_AUTH_CUSTODY"
_credential_processes = 0
_graceful_credential_processes = 0


def auth_custody_report() -> dict[str, Any]:
    """Process-local completion evidence; never contains argv or native output."""
    return {
        "schema_version": 1,
        "credential_processes": _credential_processes,
        "graceful_credential_processes": _graceful_credential_processes,
    }


class ControlError(ValueError):
    pass


class CredentialCompletionError(ControlError):
    pass


def select_model_info(
    rows: list[dict[str, Any]], requested: str, resolved: str
) -> dict[str, Any]:
    """Prefer the native selector; equal resolved aliases need not be ambiguous."""
    exact = [row for row in rows if row.get("value") == requested]
    matches = exact or [row for row in rows if row.get("resolvedModel") == resolved]
    if not matches:
        raise ControlError("native model missing")
    ignored = set() if exact else {"value", "displayName", "description"}
    descriptors = {
        json.dumps(
            {key: value for key, value in row.items() if key not in ignored},
            sort_keys=True,
            allow_nan=False,
        )
        for row in matches
    }
    if len(descriptors) != 1:
        raise ControlError("conflicting native model descriptors")
    return matches[0]


class ControlProcess:
    def __init__(
        self,
        argv: list[str],
        root: Path,
        environment: dict[str, str],
        *,
        credential_consumer: bool = True,
    ):
        global _credential_processes

        self.credential_consumer = (
            credential_consumer and environment.get(CUSTODY_ENV) == "required"
        )
        self.return_code: int | None = None
        self.forced_termination = False
        self.shutdown_requested = False
        self.graceful = False
        self._closed = False
        if self.credential_consumer:
            # Register before spawn: a failed launch cannot look like a complete
            # credential operation to the outer metadata helper.
            _credential_processes += 1
        self.process = subprocess.Popen(  # nosec B603
            argv,
            cwd=root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        if self.process.stdout is None:
            raise ControlError("native stdout pipe absent")
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.read_bytes = 0
        self.deadline = time.monotonic() + 25

    def send(self, value: dict[str, Any]) -> None:
        if self._closed:
            raise ControlError("native control process is closed")
        if self.process.stdin is None:
            raise ControlError("native control pipe absent")
        data = (json.dumps(value, allow_nan=False) + "\n").encode()
        if len(data) > 128 * 1024:
            raise ControlError("native control request exceeds limit")
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def line(self) -> str:
        if self._closed:
            raise ControlError("native control process is closed")
        while b"\n" not in self.buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not (events := self.selector.select(remaining)):
                raise ControlError("native metadata control timed out")
            data = os.read(events[0][0].fd, 65536)
            if not data:
                raise ControlError("native metadata control exited before response")
            self.read_bytes += len(data)
            if self.read_bytes > 16 * 1024 * 1024:
                raise ControlError("native metadata output exceeds limit")
            self.buffer.extend(data)
        line, _, remaining_bytes = self.buffer.partition(b"\n")
        self.buffer = bytearray(remaining_bytes)
        return line.decode("utf-8")

    def rpc(self, method: str, params: dict[str, Any], request_id: int) -> Any:
        if method not in {"initialize", "model/list", "config/read"}:
            raise ControlError("non-metadata RPC rejected")
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            response = json.loads(self.line())
            if response.get("id") == request_id:
                if "error" in response:
                    raise ControlError("native metadata RPC rejected")
                return response["result"]

    def close(self) -> None:
        global _graceful_credential_processes

        if self._closed:
            if not self.graceful and (
                self.credential_consumer or not self.shutdown_requested
            ):
                error = (
                    CredentialCompletionError
                    if self.credential_consumer
                    else ControlError
                )
                raise error("native consumer completion is unproven")
            return
        self._closed = True
        stdin_closed = True
        try:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    stdin_closed = False
            try:
                # Drain, but never parse callbacks after closure. A child can
                # otherwise block on a full stdout pipe while handling stdin EOF.
                deadline = time.monotonic() + 1
                while self.process.poll() is None and time.monotonic() < deadline:
                    if not self.selector.get_map():
                        self.process.wait(
                            timeout=max(0.001, deadline - time.monotonic())
                        )
                        break
                    for key, _ in self.selector.select(
                        min(0.05, max(0, deadline - time.monotonic()))
                    ):
                        data = os.read(key.fd, 65536)
                        if not data:
                            self.selector.unregister(key.fileobj)
                            continue
                        self.read_bytes += len(data)
                        if self.read_bytes > 16 * 1024 * 1024:
                            self.forced_termination = True
                            error = (
                                CredentialCompletionError
                                if self.credential_consumer
                                else ControlError
                            )
                            raise error(
                                "native metadata output exceeds limit during close"
                            )
                self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self.forced_termination = True
                self.shutdown_requested = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=2)
            self.return_code = self.process.returncode
        finally:
            self.selector.close()
            # A surviving process group after the leader exits is also forced
            # cleanup, not independent evidence that refresh safely completed.
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                self.forced_termination = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=2)
            self.return_code = self.process.returncode
            if self.process.stdout:
                self.process.stdout.close()
            self.buffer.clear()
        self.graceful = (
            stdin_closed and not self.forced_termination and self.return_code == 0
        )
        if self.credential_consumer:
            if not self.graceful:
                raise CredentialCompletionError(
                    "native credential consumer completion is unproven"
                )
            _graceful_credential_processes += 1
        elif not self.shutdown_requested and self.return_code != 0:
            raise ControlError("native metadata process exited abnormally")

    def __enter__(self) -> ControlProcess:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
