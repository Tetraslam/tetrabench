"""Bounded native metadata transport, also shipped into dependency-free sandboxes."""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import signal
import subprocess  # nosec B404
import time
from collections.abc import Mapping
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
    return _model_descriptor(matches, ignored)


def _model_descriptor(
    matches: list[dict[str, Any]], ignored: set[str]
) -> dict[str, Any]:
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


def _claude_base(model: str) -> str:
    # Claude 2.1.267's trailing context modifier, not a provider-ID rewrite.
    return re.sub(r"\[1m\]$", "", model, flags=re.IGNORECASE)


def claude_model_metadata(
    data: dict[str, Any],
    settings: dict[str, Any],
    *,
    requested: str,
    version: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Separate applied startup settings from picker-derived capabilities.

    CLI 2.1.267 get_settings.applied is runtime state; effective is merged config.
    A default picker row survives availableModels filtering, even for a blocked
    explicit selection. Never use that row to bypass an explicit allowlist.
    Neither applied settings nor picker presence establishes account entitlement.
    """
    if version != "2.1.267":
        raise ControlError("unsupported Claude applied-settings contract")
    applied, effective = settings.get("applied"), settings.get("effective")
    if not isinstance(applied, dict) or not isinstance(effective, dict):
        raise ControlError("native applied Claude settings unavailable")
    model, effort = applied.get("model"), applied.get("effort")
    if (
        not isinstance(model, str)
        or not model
        or "effort" not in applied
        or (effort is not None and not isinstance(effort, str))
        or settings.get("errors")
    ):
        raise ControlError("invalid native applied Claude settings")
    rows = data.get("models")
    unavailable = data.get("unavailable_models", [])
    if not isinstance(rows, list) or not isinstance(unavailable, list):
        raise ControlError("invalid native Claude model catalog")
    if any(
        len(items) > 10000
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("value"), str)
            or not isinstance(row.get("resolvedModel"), str)
            for row in items
        )
        for items in (rows, unavailable)
    ):
        raise ControlError("invalid native Claude model catalog")
    base = _claude_base(model)
    if any(
        row["value"] == requested or _claude_base(row["resolvedModel"]) == base
        for row in [*unavailable, *(row for row in rows if row.get("disabled"))]
    ):
        raise ControlError("native Claude model is unavailable")
    allowlist = effective.get("availableModels")
    if allowlist is not None:
        if not isinstance(allowlist, list) or any(
            not isinstance(value, str) for value in allowlist
        ):
            raise ControlError("invalid native Claude model restrictions")
        if requested != "default":
            rows = [row for row in rows if row["value"] != "default"]
    # Full IDs must agree with applied state; aliases need a native row linking
    # the requested spelling to that state. Do not invent alias-to-model tables.
    if _claude_base(requested) != base and not any(
        _claude_base(row["value"]) == _claude_base(requested)
        and _claude_base(row["resolvedModel"]) == base
        for row in rows
    ):
        raise ControlError("native applied Claude model differs from request")
    matches = [row for row in rows if row["value"] == requested]
    source = "exact-selector"
    ignored: set[str] = set()
    if not matches:
        source = "resolved-model"
        ignored = {"value", "displayName", "description"}
        matches = [row for row in rows if row["resolvedModel"] == model]
    if not matches:
        source = "context-modifier-capability-fallback"
        matches = [row for row in rows if _claude_base(row["resolvedModel"]) == base]
        ignored.add("resolvedModel")
    if not matches:
        raise ControlError("native Claude model missing or restricted")
    row = _model_descriptor(matches, ignored)
    if _claude_base(row["resolvedModel"]) != base:
        raise ControlError("native Claude catalog differs from applied model")
    projection = {
        key: row[key]
        for key in (
            "value",
            "resolvedModel",
            "supportsEffort",
            "supportedEffortLevels",
            "supportsAdaptiveThinking",
            "disabled",
        )
        if key in row
    }
    settings_env = effective.get("env", {})
    if not isinstance(settings_env, dict):
        raise ControlError("invalid native Claude environment settings")
    disabled = settings_env.get(
        "CLAUDE_CODE_DISABLE_1M_CONTEXT",
        environment.get("CLAUDE_CODE_DISABLE_1M_CONTEXT", ""),
    )
    return {
        "models": [projection],
        "applied": {"model": model, "effort": effort},
        "settings_effort": effective.get("effortLevel"),
        "selection_source": source,
        "restrictions": {
            "availableModels": allowlist,
            "context_1m_disabled": str(disabled).lower() in {"1", "true", "yes", "on"},
        },
        "entitlement": "unknown",
    }


def claude_control_metadata(
    process: ControlProcess,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read startup state without a prompt, model switch or settings write."""
    results = []
    for subtype in ("initialize", "get_settings"):
        request: dict[str, Any] = {"subtype": subtype}
        if subtype == "initialize":
            request.update(hooks={}, agents={}, sdkMcpServers=[], plugins=[])
        process.send(
            {
                "type": "control_request",
                "request_id": "metadata-" + subtype,
                "request": request,
            }
        )
        while True:
            message = json.loads(process.line())
            if message.get("type") in {"assistant", "result"}:
                raise ControlError("unexpected model turn during Claude metadata read")
            response = message.get("response", {})
            if response.get("request_id") == "metadata-" + subtype:
                if response.get("subtype") != "success" or not isinstance(
                    response.get("response"), dict
                ):
                    raise ControlError("native Claude metadata unavailable")
                results.append(response["response"])
                break
    return results[0], results[1]


# rust-v0.154.0: config/mod.rs selects "openai" when model_provider is absent;
# ModelProviderInfo::create_openai_provider and ::to_api_provider own its defaults.
# This is a harness compatibility contract, not a model or provider catalog.
_CODEX_ROUTE_SOURCE = "https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/"


def codex_route(
    config: Mapping[str, Any],
    *,
    version: str,
    requested_provider: str,
    observed_auth_mode: str | None,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Project config/read; fill only an unoverridden API-key built-in route.

    Callers must verify the installed version and native auth mode separately.
    Missing custom-provider fields are not Codex built-in OpenAI defaults.
    No native configuration or credential state is changed here.
    """
    provider_id = config.get("model_provider") or "unknown"
    providers = config.get("model_providers") or {}
    definition = providers.get(provider_id, {})
    protocol = definition.get("wire_api") or "unknown"
    endpoint = definition.get("base_url") or ""
    provenance: dict[str, Any] = {
        "kind": "native-config-read",
        "method": "config/read",
        "native_version": version,
        "default_fields": [],
    }
    builtin = provider_id == "openai" or (
        config.get("model_provider") is None and requested_provider == "openai"
    )
    # OpenAI entries in model_providers are ignored by the pinned native merge,
    # not effective overrides. Do not mistake those raw config/read fields for
    # runtime routing or silently substitute defaults over user intent.
    if builtin:
        protocol, endpoint = "unknown", ""
        if (
            version == "0.154.0"
            and requested_provider == "openai"
            and observed_auth_mode == "api_key"
            and "openai" not in providers
            and not config.get("openai_base_url")
            and not environment.get("OPENAI_BASE_URL")
            and not config.get("profile")
            and not config.get("profiles")
        ):
            default_fields = ["protocol", "endpoint"]
            if provider_id == "unknown":
                default_fields.insert(0, "provider")
            provider_id, protocol, endpoint = (
                "openai",
                "responses",
                "https://api.openai.com/v1",
            )
            provenance.update(
                kind="native-version-default",
                default_fields=default_fields,
                source_urls=[
                    _CODEX_ROUTE_SOURCE + "core/src/config/mod.rs",
                    _CODEX_ROUTE_SOURCE + "model-provider-info/src/lib.rs",
                ],
            )
    return {
        "provider": provider_id,
        "protocol": protocol,
        "endpoint": endpoint,
        "status": "bound"
        if provider_id != "unknown" and protocol != "unknown" and endpoint
        else "unknown",
        "provenance": provenance,
    }


def _control_json(process: ControlProcess, limit: int) -> dict[str, Any]:
    text = ""
    while len(text.encode()) <= limit:
        text += process.line() + "\n"
        if len(text.encode()) > limit:
            break
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            raise ControlError("native metadata object expected")
        return value
    raise ControlError("native metadata object exceeds limit")


def opencode_model_metadata(
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    *,
    provider: str,
    model_id: str,
    version: str,
) -> dict[str, Any]:
    """Read the pinned registry/config through naturally exiting native commands.

    v1.18.30 cli/cmd/models.ts prints Provider.list() models, but not provider
    options. cli/cmd/debug/config.ts exposes Config.get()'s merged layers; retain
    its baseURL override just as the /provider collector does. Neither command
    creates a session. Keep cwd/env unchanged so native discovery owns precedence.
    """
    if version != "1.18.30":
        raise ControlError("unverified OpenCode metadata CLI version")
    with ControlProcess(
        [*command, "models", provider, "--verbose"], cwd, environment
    ) as process:
        for _ in range(10000):
            if process.line() == provider + "/" + model_id:
                model = _control_json(process, 128 * 1024)
                break
        else:
            raise ControlError("selected native model unavailable")
    if not process.graceful:
        raise ControlError("native models CLI completion is unproven")
    if model.get("providerID") != provider:
        raise ControlError("native model provider differs")
    with ControlProcess([*command, "debug", "config"], cwd, environment) as process:
        config = _control_json(process, 2 * 1024 * 1024)
    if not process.graceful:
        raise ControlError("native config CLI completion is unproven")
    options = config.get("provider", {}).get(provider, {}).get("options", {})
    endpoint = options.get("baseURL") or model["api"]["url"]
    return {
        "id": provider,
        "options": {"baseURL": endpoint},
        "models": {model_id: model},
    }


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
        if method not in {"initialize", "model/list", "config/read", "account/read"}:
            raise ControlError("non-metadata RPC rejected")
        if method == "account/read" and (
            set(params) != {"refreshToken"} or params["refreshToken"] is not False
        ):
            raise ControlError("metadata account/read must disable token refresh")
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
