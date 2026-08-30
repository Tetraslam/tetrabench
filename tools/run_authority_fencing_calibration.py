#!/usr/bin/env python3
"""Run the credential-brokered authority-fencing OpenCode calibration."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import http.client
import ipaddress
import math
import os
import secrets
import socket
import ssl
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
import urllib.parse
import warnings
from collections.abc import Callable, Mapping
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from harbor.models.job.result import JobResult
from harbor.models.task.task import Task
from harbor.models.trial.result import TrialResult
from harbor.publisher.packager import Packager

REPOSITORY_ROOT = Path(__file__).parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.run_authority_fencing_admission import (  # noqa: E402
    GATES,
    ROOT,
    TASK,
    InstalledCLI,
    NativeSnapshot,
    ProofOutputAuthority,
    SourceSnapshot,
    _bounded_command,
    _copy_verified_tree,
    _native_run_record,
    canonical,
    create_clean_source_snapshot,
    create_debug_source_snapshot,
    evidence_argv,
    install_snapshot_cli,
    manifest_digest,
    open_proof_output_authority,
    safe_error,
    snapshot_native_output,
    source_manifest,
    strict_json,
    tree_digest,
    tree_manifest,
    write_exclusive_proof,
)

UPSTREAM_URL = "https://litellm-proxy.taildb21e0.ts.net"
PARENT_KEY_ENV = "TETRABENCH_CALIBRATION_GATEWAY_KEY"
TASK_ID = "systems-design/authority-fencing"
MAX_ATTEMPT_SECONDS = 35 * 60
MAX_TOTAL_COST = Decimal("25")
MAX_INPUT_OR_CACHE_COST_PER_TOKEN = Decimal("10") / Decimal(1_000_000)
MAX_OUTPUT_COST_PER_TOKEN = Decimal("50") / Decimal(1_000_000)
RESERVATION_SAFETY_MARGIN = Decimal("0.25")
MAX_OUTPUT_TOKENS = 8192
MAX_REQUESTS = 64
MAX_CONCURRENCY = 2
MAX_WORKERS = 4
DEFAULT_BROKER_PORT = 62017
MIN_BROKER_PORT = 49152
MAX_BODY_BYTES = 512 << 10
MAX_HEADER_BYTES = 16 << 10
MAX_RESPONSE_BYTES = 64 << 20
BACKPRESSURE_TIMEOUT_SECONDS = 30
HEADER_READ_TIMEOUT_SECONDS = 2
ALLOWED_PATHS = frozenset({"/v1/chat/completions", "/v1/responses"})
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
PROVIDER_ENV_MARKERS = (
    "API_KEY",
    "ACCESS_KEY",
    "SECRET",
    "TOKEN",
    "CREDENTIAL",
    "OPENAI_",
    "ANTHROPIC_",
    "AWS_",
    "TIGRIS_",
)
PROFILES = (
    ("target", "openai/gpt-5.6-sol"),
    ("alternate", "anthropic/claude-sonnet-5"),
)
CALIBRATION_TOOL = ROOT / "tools/run_authority_fencing_calibration.py"
CALIBRATION_TEST = ROOT / "tests/test_authority_fencing_calibration.py"
SOURCE_RELATIVE_PATHS = (
    Path("src/tetrabench"),
    Path("pyproject.toml"),
    Path("uv.lock"),
    TASK.relative_to(ROOT),
    Path("tools/run_authority_fencing_admission.py"),
    CALIBRATION_TOOL.relative_to(ROOT),
    Path("tests/conftest.py"),
    Path("tests/test_authority_fencing_task.py"),
    CALIBRATION_TEST.relative_to(ROOT),
)


@dataclasses.dataclass(frozen=True, slots=True)
class RequestRecord:
    ordinal: int
    endpoint: str
    model: str
    status: int
    worst_case_reservation_usd: str
    cost: str | None
    usage: dict[str, int]
    call_id: str | None
    request_id: str | None
    response_bytes: int
    disconnected: bool
    settlement: str
    retained_unknown_reservation_usd: str


@dataclasses.dataclass(frozen=True, slots=True)
class PricingSnapshot:
    models: tuple[dict[str, Any], ...]
    sha256: str


@dataclasses.dataclass(frozen=True, slots=True)
class DockerBridgeTopology:
    gateway: str
    interface: str
    subnet: str


@dataclasses.dataclass(frozen=True, slots=True)
class Reservation:
    identifier: int
    amount: Decimal


class SpendLedger:
    """Atomically own actual spend plus exact in-flight worst-case reservations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cost = Decimal(0)
        self._reserved = Decimal(0)
        self._reservations: dict[int, Decimal] = {}
        self._next_identifier = 0
        self._fatal: str | None = None

    def reserve(self, amount: Decimal) -> Reservation:
        if not amount.is_finite() or amount <= 0:
            raise ValueError("calibration reservation must be finite positive")
        with self._lock:
            if self._fatal is not None:
                raise RuntimeError("calibration spend ledger is invalid")
            projected = self._cost + self._reserved + amount
            if projected > MAX_TOTAL_COST:
                raise RuntimeError(
                    "calibration remaining budget cannot cover reservation"
                )
            self._next_identifier += 1
            identifier = self._next_identifier
            self._reservations[identifier] = amount
            self._reserved += amount
            return Reservation(identifier=identifier, amount=amount)

    def settle(self, reservation: Reservation, cost: Decimal) -> None:
        with self._lock:
            amount = self._reservations.get(reservation.identifier)
            if amount is None or amount != reservation.amount:
                raise RuntimeError("calibration spend reservation mismatch")
            if not cost.is_finite() or cost < 0:
                self._fatal = "authoritative cost is not finite nonnegative"
                raise RuntimeError(self._fatal)
            self._reservations.pop(reservation.identifier)
            self._reserved -= amount
            if cost > amount:
                self._fatal = "authoritative cost exceeded exact reservation"
                raise RuntimeError(self._fatal)
            if self._cost + self._reserved + cost > MAX_TOTAL_COST:
                self._fatal = "authoritative settlement would exceed calibration cap"
                raise RuntimeError(self._fatal)
            self._cost += cost

    def release_unforwarded(self, reservation: Reservation) -> None:
        with self._lock:
            amount = self._reservations.pop(reservation.identifier, None)
            if amount is None or amount != reservation.amount:
                raise RuntimeError("calibration spend reservation mismatch")
            self._reserved -= amount

    def retain_unknown(self, reservation: Reservation, reason: str) -> None:
        with self._lock:
            amount = self._reservations.get(reservation.identifier)
            if amount is None or amount != reservation.amount:
                raise RuntimeError("calibration spend reservation mismatch")
            self._fatal = reason

    def fail(self, reason: str) -> None:
        with self._lock:
            self._fatal = reason

    @property
    def cost(self) -> Decimal:
        with self._lock:
            return self._cost

    @property
    def reserved(self) -> Decimal:
        with self._lock:
            return self._reserved

    @property
    def fatal(self) -> str | None:
        with self._lock:
            return self._fatal


class BrokerState:
    def __init__(
        self,
        *,
        parent_key: str,
        token: str,
        probe_token: str | None,
        model: str,
        max_input_tokens: int,
        deadline: float,
        ledger: SpendLedger,
        upstream_url: str = UPSTREAM_URL,
    ) -> None:
        self.parent_key = parent_key
        self._token: str | None = token
        self._probe_token: str | None = probe_token
        self._probe_consumed = False
        self.model = model
        self.max_input_tokens = max_input_tokens
        self.deadline = deadline
        self.ledger = ledger
        self.upstream_url = upstream_url.rstrip("/")
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)
        self.request_count = 0
        self.locked_endpoint: str | None = None
        self.active = True
        self.records: list[RequestRecord] = []
        self.upstreams: set[http.client.HTTPConnection] = set()

    def authorize(self, headers: Any) -> None:
        values = headers.get_all("Authorization", [])
        with self.lock:
            token = self._token
            active = self.active
        if (
            not active
            or token is None
            or time.monotonic() >= self.deadline
            or len(values) != 1
            or not secrets.compare_digest(values[0], f"Bearer {token}")
        ):
            raise PermissionError("broker authorization rejected")

    def consume_probe(self, headers: Any) -> None:
        values = headers.get_all("Authorization", [])
        with self.lock:
            if time.monotonic() >= self.deadline:
                self.active = False
                self._token = None
                self._probe_token = None
                raise PermissionError("broker probe authorization rejected")
            token = self._probe_token
            if (
                not self.active
                or token is None
                or len(values) != 1
                or not secrets.compare_digest(values[0], f"Bearer {token}")
            ):
                raise PermissionError("broker probe authorization rejected")
            self._probe_token = None
            self._probe_consumed = True

    @property
    def probe_consumed(self) -> bool:
        with self.lock:
            return self._probe_consumed

    def begin_request(
        self, endpoint: str, reservation_amount: Decimal
    ) -> tuple[int, Reservation]:
        if not self.semaphore.acquire(blocking=False):
            raise BlockingIOError("broker concurrency limit reached")
        try:
            with self.lock:
                if not self.active or time.monotonic() >= self.deadline:
                    raise PermissionError("broker attempt expired")
                if self.request_count >= MAX_REQUESTS:
                    raise RuntimeError("broker request limit reached")
                if (
                    self.locked_endpoint is not None
                    and self.locked_endpoint != endpoint
                ):
                    raise ValueError("broker endpoint changed within attempt")
                reservation = self.ledger.reserve(reservation_amount)
                if self.locked_endpoint is None:
                    self.locked_endpoint = endpoint
                self.request_count += 1
                ordinal = self.request_count
            return ordinal, reservation
        except BaseException:
            self.semaphore.release()
            raise

    def finish_request(self, reservation: Reservation, cost: Decimal) -> None:
        try:
            self.ledger.settle(reservation, cost)
        finally:
            self.semaphore.release()

    def release_unforwarded(self, reservation: Reservation) -> None:
        try:
            self.ledger.release_unforwarded(reservation)
        finally:
            self.semaphore.release()

    def retain_unknown(self, reservation: Reservation, reason: str) -> None:
        try:
            self.ledger.retain_unknown(reservation, reason)
        finally:
            self.semaphore.release()

    def invalidate(self) -> None:
        with self.lock:
            self.active = False
            self._token = None
            self._probe_token = None
            self.parent_key = ""
            upstreams = tuple(self.upstreams)
        for upstream in upstreams:
            upstream.close()

    def register_upstream(self, upstream: http.client.HTTPConnection) -> None:
        with self.lock:
            if not self.active:
                raise PermissionError("broker attempt expired")
            self.upstreams.add(upstream)

    def unregister_upstream(self, upstream: http.client.HTTPConnection) -> None:
        with self.lock:
            self.upstreams.discard(upstream)


class CalibrationHTTPServer(ThreadingHTTPServer):
    state: BrokerState

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._worker_lock = threading.Lock()
        self._worker_slots = threading.BoundedSemaphore(MAX_WORKERS)
        self._worker_threads: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("calibration broker received a non-socket request")
        request.settimeout(HEADER_READ_TIMEOUT_SECONDS)
        if not self._worker_slots.acquire(blocking=False):
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)
            request.close()
            return

        def run() -> None:
            try:
                self.process_request_thread(request, client_address)
            finally:
                with self._worker_lock:
                    self._connections.discard(request)
                    self._worker_threads.discard(threading.current_thread())
                self._worker_slots.release()

        thread = threading.Thread(target=run, name="calibration-broker-request")
        with self._worker_lock:
            self._connections.add(request)
            self._worker_threads.add(thread)
        thread.start()

    @property
    def worker_count(self) -> int:
        with self._worker_lock:
            return len(self._worker_threads)

    def abort_connections(self) -> tuple[threading.Thread, ...]:
        with self._worker_lock:
            connections = tuple(self._connections)
            threads = tuple(self._worker_threads)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()
        return threads


def _parse_cost(headers: http.client.HTTPMessage) -> Decimal:
    values = headers.get_all("X-Litellm-Response-Cost", [])
    if not values:
        raise ValueError("model response omitted authoritative cost")
    if len(values) != 1:
        raise ValueError("model response cost header is ambiguous")
    try:
        value = Decimal(values[0])
    except InvalidOperation as error:
        raise ValueError("model response cost header is malformed") from error
    if not value.is_finite() or value < 0:
        raise ValueError("model response cost header is not finite nonnegative")
    return value


def _bounded_usage(headers: http.client.HTTPMessage) -> dict[str, int]:
    names = {
        "input_tokens": "X-Litellm-Prompt-Tokens",
        "output_tokens": "X-Litellm-Completion-Tokens",
        "total_tokens": "X-Litellm-Total-Tokens",
    }
    usage: dict[str, int] = {}
    for key, name in names.items():
        values = headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("model response token header is ambiguous")
        if not values:
            continue
        if not values[0].isdigit():
            raise ValueError("model response token header is malformed")
        value = int(values[0])
        if value > 1_000_000_000:
            raise ValueError("model response token header exceeds limit")
        usage[key] = value
    return usage


def _single_header(headers: http.client.HTTPMessage, *names: str) -> str | None:
    found: list[str] = []
    for name in names:
        found.extend(headers.get_all(name, []))
    if len(found) > 1:
        raise ValueError("model response identifier header is ambiguous")
    if not found:
        return None
    value = found[0]
    if len(value) > 256 or any(character in value for character in "\r\n\0"):
        raise ValueError("model response identifier header is malformed")
    return value


def _positive_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"model pricing {field} is not numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"model pricing {field} is malformed") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"model pricing {field} must be finite positive")
    return parsed


def _positive_limit(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"model limit {field} must be a positive integer")
    return value


def _validate_pricing_document(document: Any) -> PricingSnapshot:
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise ValueError("gateway model-info response schema rejected")
    expected = {model_group for _profile, model_group in PROFILES}
    selected: dict[str, dict[str, Any]] = {}
    rate_fields = (
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
    )
    for row in document["data"]:
        if not isinstance(row, dict) or row.get("model_name") not in expected:
            continue
        model_name = row["model_name"]
        if model_name in selected:
            raise ValueError("gateway model-info returned an ambiguous model group")
        info = row.get("model_info")
        if not isinstance(info, dict):
            raise ValueError("gateway model-info pricing is missing")
        rates = {
            field: _positive_decimal(info.get(field), field) for field in rate_fields
        }
        if (
            rates["input_cost_per_token"] > MAX_INPUT_OR_CACHE_COST_PER_TOKEN
            or rates["cache_read_input_token_cost"] > MAX_INPUT_OR_CACHE_COST_PER_TOKEN
            or rates["cache_creation_input_token_cost"]
            > MAX_INPUT_OR_CACHE_COST_PER_TOKEN
            or rates["output_cost_per_token"] > MAX_OUTPUT_COST_PER_TOKEN
        ):
            raise ValueError("gateway model pricing exceeds calibration hard ceiling")
        max_input = _positive_limit(info.get("max_input_tokens"), "max_input_tokens")
        max_output = _positive_limit(info.get("max_output_tokens"), "max_output_tokens")
        if max_output < MAX_OUTPUT_TOKENS:
            raise ValueError("gateway model output limit is below calibration cap")
        selected[model_name] = {
            "cache_creation_input_token_cost": str(
                rates["cache_creation_input_token_cost"]
            ),
            "cache_read_input_token_cost": str(rates["cache_read_input_token_cost"]),
            "input_cost_per_token": str(rates["input_cost_per_token"]),
            "max_input_tokens": max_input,
            "max_output_tokens": max_output,
            "model_group": model_name,
            "output_cost_per_token": str(rates["output_cost_per_token"]),
        }
    if set(selected) != expected:
        raise ValueError("gateway model-info omitted a required model group")
    models = tuple(selected[model_group] for _profile, model_group in PROFILES)
    encoded = canonical(list(models)).encode()
    return PricingSnapshot(models=models, sha256=hashlib.sha256(encoded).hexdigest())


def query_model_pricing(
    *, parent_key: str, upstream_url: str = UPSTREAM_URL
) -> PricingSnapshot:
    parsed = urllib.parse.urlsplit(upstream_url)
    if parsed.hostname is None or parsed.scheme not in {"http", "https"}:
        raise ValueError("gateway URL rejected")
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    kwargs: dict[str, Any] = {"timeout": BACKPRESSURE_TIMEOUT_SECONDS}
    if parsed.scheme == "https":
        kwargs["context"] = ssl.create_default_context()
    connection = connection_type(parsed.hostname, parsed.port, **kwargs)
    try:
        connection.request(
            "GET",
            f"{parsed.path.rstrip('/')}/model/info",
            headers={
                "Authorization": f"Bearer {parent_key}",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read((1 << 20) + 1)
        if response.status != HTTPStatus.OK or len(body) > 1 << 20:
            raise ValueError("gateway model-info request failed")
        return _validate_pricing_document(strict_json(body))
    finally:
        connection.close()


def discover_docker_bridge_topology() -> DockerBridgeTopology:
    info = subprocess.run(  # nosec B603 B607
        ["docker", "info", "--format", "{{json .SecurityOptions}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    security_options = strict_json(info.stdout.encode())
    if not isinstance(security_options, list) or any(
        isinstance(item, str) and "rootless" in item.lower()
        for item in security_options
    ):
        raise RuntimeError("rootless Docker is unsupported for calibration proof")
    inspected = subprocess.run(  # nosec B603 B607
        ["docker", "network", "inspect", "bridge"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    networks = strict_json(inspected.stdout)
    if (
        not isinstance(networks, list)
        or len(networks) != 1
        or not isinstance(networks[0], dict)
        or networks[0].get("Name") != "bridge"
        or networks[0].get("Driver") != "bridge"
    ):
        raise RuntimeError("Docker default bridge discovery failed")
    network = networks[0]
    options = network.get("Options")
    interface = (
        options.get("com.docker.network.bridge.name", "docker0")
        if isinstance(options, dict)
        else "docker0"
    )
    configs = (
        network.get("IPAM", {}).get("Config")
        if isinstance(network.get("IPAM"), dict)
        else None
    )
    ipv4_configs = (
        [
            item
            for item in configs
            if isinstance(item, dict)
            and isinstance(item.get("Gateway"), str)
            and isinstance(item.get("Subnet"), str)
            and ipaddress.ip_address(item["Gateway"]).version == 4
            and ipaddress.ip_network(item["Subnet"]).version == 4
        ]
        if isinstance(configs, list)
        else []
    )
    if len(ipv4_configs) != 1 or not isinstance(interface, str) or not interface:
        raise RuntimeError("Docker default bridge gateway is ambiguous")
    address = subprocess.run(  # nosec B603 B607
        ["ip", "-j", "address", "show", "dev", interface],
        check=True,
        capture_output=True,
        timeout=30,
    )
    interfaces = strict_json(address.stdout)
    locals_found = (
        {
            item.get("local")
            for entry in interfaces
            if isinstance(entry, dict)
            for item in entry.get("addr_info", [])
            if isinstance(item, dict) and item.get("family") == "inet"
        }
        if isinstance(interfaces, list)
        else set()
    )
    gateway = ipv4_configs[0]["Gateway"]
    if gateway not in locals_found:
        raise RuntimeError("Docker bridge gateway is not a bindable host interface")
    return DockerBridgeTopology(
        gateway=gateway,
        interface=interface,
        subnet=str(ipaddress.ip_network(ipv4_configs[0]["Subnet"])),
    )


def discover_docker_bridge_gateway() -> str:
    return discover_docker_bridge_topology().gateway


def _topology_evidence(
    topology: DockerBridgeTopology, *, port: int, reachable: bool
) -> dict[str, Any]:
    if not 1 <= len(topology.interface) <= 15 or any(
        not (character.isascii() and (character.isalnum() or character in "_.:-"))
        for character in topology.interface
    ):
        raise ValueError("Docker bridge interface is unsafe for operator commands")
    network = ipaddress.ip_network(topology.subnet)
    gateway = ipaddress.ip_address(topology.gateway)
    if gateway.version != 4 or network.version != 4 or gateway not in network:
        raise ValueError("Docker bridge topology is inconsistent")
    rule = (
        f"allow in on {topology.interface} from {network} to {gateway} "
        f"port {port} proto tcp"
    )
    return {
        "gateway": str(gateway),
        "interface": topology.interface,
        "port": port,
        "reachable": reachable,
        "subnet": str(network),
        "temporary_ufw_commands": {
            "add": f"sudo ufw {rule}",
            "delete": f"sudo ufw delete {rule}",
            "removal_required_in_finally": True,
        },
    }


def _is_max_token_spelling(field: str) -> bool:
    normalized = "".join(
        character for character in field.lower() if character.isalnum()
    )
    return "max" in normalized and "token" in normalized


def _reservation_for_body(body: bytes) -> Decimal:
    """Reserve from UTF-8 bytes: tokenizers byte-fallback to at most one token/byte."""
    return (
        Decimal(len(body)) * MAX_INPUT_OR_CACHE_COST_PER_TOKEN
        + Decimal(MAX_OUTPUT_TOKENS) * MAX_OUTPUT_COST_PER_TOKEN
        + RESERVATION_SAFETY_MARGIN
    )


def _validate_request(
    handler: BaseHTTPRequestHandler, state: BrokerState
) -> tuple[str, bytes, Decimal, str]:
    if handler.command != "POST":
        raise ValueError("broker accepts POST only")
    parsed = urllib.parse.urlsplit(handler.path)
    if parsed.query or parsed.fragment or parsed.path not in ALLOWED_PATHS:
        raise ValueError("broker route rejected")
    if ".." in parsed.path.split("/"):
        raise ValueError("broker traversal rejected")
    header_size = sum(
        len(key) + len(value) + 4 for key, value in handler.headers.items()
    )
    if header_size > MAX_HEADER_BYTES:
        raise ValueError("broker headers exceed limit")
    if handler.headers.get_all("Transfer-Encoding", []):
        raise ValueError("broker transfer encoding rejected")
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not lengths[0].isdigit():
        raise ValueError("broker requires one unambiguous content length")
    length = int(lengths[0])
    if length <= 0 or length > MAX_BODY_BYTES:
        raise ValueError("broker body size rejected")
    data = handler.rfile.read(length)
    if len(data) != length:
        raise ValueError("broker body framing mismatch")
    document = strict_json(data)
    if not isinstance(document, dict) or document.get("model") != state.model:
        raise ValueError("broker model rejected")
    stream = document.get("stream", False)
    if type(stream) is not bool:
        raise ValueError("broker stream mode rejected")
    if parsed.path == "/v1/responses":
        allowed_limit_fields = {"max_output_tokens"}
        injected_field = "max_output_tokens"
    else:
        allowed_limit_fields = {"max_completion_tokens", "max_tokens"}
        injected_field = "max_completion_tokens"
    present = [field for field in allowed_limit_fields if field in document]
    if len(present) > 1:
        raise ValueError("broker output-token limit is duplicated")
    for field in document:
        if _is_max_token_spelling(field) and field not in allowed_limit_fields:
            raise ValueError("broker output-token field spelling rejected")
    if present:
        field = present[0]
        value = document[field]
        if type(value) is not int or value <= 0:
            raise ValueError("broker output-token limit rejected")
        document[field] = min(value, MAX_OUTPUT_TOKENS)
    else:
        document[injected_field] = MAX_OUTPUT_TOKENS
    data = canonical(document).encode()
    if len(data) > MAX_BODY_BYTES:
        raise ValueError("broker bounded body exceeds limit")
    if len(data) > state.max_input_tokens:
        raise ValueError("broker conservative input-token bound exceeds model limit")
    content_type = "text/event-stream" if stream else "application/json"
    return parsed.path, data, _reservation_for_body(data), content_type


def _forward_headers(
    handler: BaseHTTPRequestHandler, parent_key: str
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {parent_key}",
        "Content-Type": handler.headers.get("Content-Type", "application/json"),
        "Accept": handler.headers.get("Accept", "application/json"),
    }
    if value := handler.headers.get("User-Agent"):
        headers["User-Agent"] = value[:256]
    return headers


class CalibrationBrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tetrabench-calibration"
    sys_version = ""

    @property
    def state(self) -> BrokerState:
        server = self.server
        if not isinstance(server, CalibrationHTTPServer):
            raise RuntimeError("calibration broker server type mismatch")
        return server.state

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HEADER_READ_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args
        return

    def _reject(self, status: HTTPStatus, message: str) -> None:
        body = canonical({"error": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)
        self.close_connection = True

    def do_CONNECT(self) -> None:
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "method rejected")

    def do_GET(self) -> None:
        if self.path != "/__tetrabench_probe":
            self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "method rejected")
            return
        try:
            self.state.consume_probe(self.headers)
        except PermissionError:
            self._reject(HTTPStatus.UNAUTHORIZED, "authorization rejected")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_DELETE(self) -> None:
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "method rejected")

    def do_POST(self) -> None:
        state = self.state
        reservation: Reservation | None = None
        try:
            state.authorize(self.headers)
            endpoint, body, reservation_amount, content_type = _validate_request(
                self, state
            )
            ordinal, reservation = state.begin_request(endpoint, reservation_amount)
        except PermissionError:
            self._reject(HTTPStatus.UNAUTHORIZED, "authorization rejected")
            return
        except BlockingIOError:
            self._reject(HTTPStatus.TOO_MANY_REQUESTS, "concurrency rejected")
            return
        except (RuntimeError, ValueError):
            self._reject(HTTPStatus.BAD_REQUEST, "request rejected")
            return

        cost: Decimal | None = None
        upstream: http.client.HTTPConnection | None = None
        response_bytes = 0
        disconnected = False
        status = 0
        usage: dict[str, int] = {}
        call_id: str | None = None
        request_id: str | None = None
        potentially_paid = False
        settlement_finalized = False
        settlement = "unforwarded"
        response: http.client.HTTPResponse | None = None
        try:
            parsed = urllib.parse.urlsplit(state.upstream_url)
            if parsed.hostname is None:
                raise ValueError("upstream host is missing")
            if parsed.scheme == "https":
                upstream = http.client.HTTPSConnection(
                    parsed.hostname,
                    parsed.port,
                    timeout=BACKPRESSURE_TIMEOUT_SECONDS,
                    context=ssl.create_default_context(),
                )
            elif parsed.scheme == "http":
                upstream = http.client.HTTPConnection(
                    parsed.hostname,
                    parsed.port,
                    timeout=BACKPRESSURE_TIMEOUT_SECONDS,
                )
            else:
                raise ValueError("upstream scheme rejected")
            state.register_upstream(upstream)
            potentially_paid = True
            upstream.request(
                "POST",
                endpoint,
                body=body,
                headers={
                    **_forward_headers(self, state.parent_key),
                    "Content-Length": str(len(body)),
                },
            )
            response = upstream.getresponse()
            status = response.status
            cost = _parse_cost(response.headers)
            usage = _bounded_usage(response.headers)
            call_id = _single_header(response.headers, "X-Litellm-Call-Id")
            request_id = _single_header(
                response.headers, "X-Litellm-Request-Id", "X-Request-Id"
            )
            content_lengths = response.headers.get_all("Content-Length", [])
            if len(content_lengths) > 1 or (
                content_lengths and not content_lengths[0].isdigit()
            ):
                raise ValueError("model response framing is ambiguous")
            if content_lengths and int(content_lengths[0]) > MAX_RESPONSE_BYTES:
                raise ValueError("model response exceeds limit")
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise ValueError("model response exceeds limit")
            if content_lengths and len(response_body) != int(content_lengths[0]):
                raise ValueError("model response framing mismatch")
            response_bytes = len(response_body)
            try:
                settlement = "known_fatal"
                state.finish_request(reservation, cost)
                settlement = "settled"
            finally:
                settlement_finalized = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.connection.settimeout(BACKPRESSURE_TIMEOUT_SECONDS)
            try:
                self.wfile.write(response_body)
                self.wfile.flush()
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                disconnected = True
        except (OSError, http.client.HTTPException, RuntimeError, ValueError):
            if reservation is not None and not settlement_finalized:
                try:
                    if cost is not None:
                        settlement = "known_fatal"
                        state.finish_request(reservation, cost)
                        settlement = "settled"
                    elif potentially_paid:
                        state.retain_unknown(
                            reservation, "authoritative settlement unavailable"
                        )
                        settlement = "retained_unknown"
                    else:
                        state.release_unforwarded(reservation)
                        settlement = "released_unforwarded"
                finally:
                    settlement_finalized = True
            if state.ledger.fatal is None:
                state.ledger.fail("broker upstream response invalid")
            self._reject(HTTPStatus.BAD_GATEWAY, "upstream rejected")
        finally:
            if response is not None:
                response.close()
            if upstream is not None:
                state.unregister_upstream(upstream)
                upstream.close()
            if reservation is not None:
                retained = (
                    reservation.amount
                    if settlement == "retained_unknown"
                    else Decimal(0)
                )
                with state.lock:
                    state.records.append(
                        RequestRecord(
                            ordinal=ordinal,
                            endpoint=endpoint,
                            model=state.model,
                            status=status,
                            worst_case_reservation_usd=str(reservation.amount),
                            cost=str(cost) if cost is not None else None,
                            usage=usage,
                            call_id=call_id,
                            request_id=request_id,
                            response_bytes=response_bytes,
                            disconnected=disconnected,
                            settlement=settlement,
                            retained_unknown_reservation_usd=str(retained),
                        )
                    )


class CalibrationBroker:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        parent_key: str,
        model: str,
        ledger: SpendLedger,
        max_input_tokens: int = MAX_BODY_BYTES,
        timeout: int = MAX_ATTEMPT_SECONDS,
        upstream_url: str = UPSTREAM_URL,
        probe_token: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(48)
        self.state = BrokerState(
            parent_key=parent_key,
            token=self.token,
            probe_token=probe_token,
            model=model,
            max_input_tokens=max_input_tokens,
            deadline=time.monotonic() + timeout,
            ledger=ledger,
            upstream_url=upstream_url,
        )
        self.server: CalibrationHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.server is not None:
            raise RuntimeError("broker already started")
        server = CalibrationHTTPServer((self.host, self.port), CalibrationBrokerHandler)
        server.daemon_threads = False
        server.block_on_close = False
        server.state = self.state
        self.server = server
        self.port = int(server.server_address[1])
        thread = threading.Thread(
            target=server.serve_forever, name="calibration-broker"
        )
        self.thread = thread
        thread.start()

    def stop(self) -> None:
        self.state.invalidate()
        server, thread = self.server, self.thread
        if server is not None:
            server.shutdown()
            workers = server.abort_connections()
            server.server_close()
            deadline = time.monotonic() + BACKPRESSURE_TIMEOUT_SECONDS + 5
            for worker in workers:
                worker.join(timeout=max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    raise RuntimeError("broker request thread survived shutdown")
        if thread is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("broker listener thread survived shutdown")
        self.server = None
        self.thread = None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((self.host, self.port)) == 0:
                raise RuntimeError("broker listener survived shutdown")

    def __enter__(self) -> CalibrationBroker:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()


ProbeConnector = Callable[[str, int, str], subprocess.CompletedProcess[str]]


def _docker_probe_connector(
    host: str, port: int, token: str
) -> subprocess.CompletedProcess[str]:
    script = (
        "import sys,urllib.request;"
        "request=urllib.request.Request(sys.argv[1],headers={"
        "'Authorization':'Bearer '+sys.argv[2]});"
        "print(urllib.request.urlopen(request,timeout=5).status)"
    )
    return subprocess.run(  # nosec B603 B607
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge",
            "python:3.12-alpine",
            "python",
            "-c",
            script,
            f"http://{host}:{port}/__tetrabench_probe",
            token,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def probe_docker_bridge_reachability(
    *,
    topology: DockerBridgeTopology,
    port: int,
    connector: ProbeConnector = _docker_probe_connector,
) -> dict[str, Any]:
    probe_token = secrets.token_urlsafe(48)
    broker = CalibrationBroker(
        host=topology.gateway,
        port=port,
        parent_key="",
        model="probe-only",
        ledger=SpendLedger(),
        timeout=120,
        upstream_url="http://127.0.0.1:1",
        probe_token=probe_token,
    )
    started = False
    try:
        broker.start()
        started = True
        result = connector(topology.gateway, port, probe_token)
        if result.returncode != 0 or result.stdout.strip() != "204":
            raise RuntimeError("Docker bridge broker reachability probe failed")
        if (
            not broker.state.probe_consumed
            or broker.state.request_count != 0
            or broker.state.records
        ):
            raise RuntimeError("Docker bridge probe reached model forwarding")
        return _topology_evidence(topology, port=port, reachable=True)
    finally:
        if started:
            broker.stop()
        else:
            broker.state.invalidate()


def child_environment(
    *,
    config_root: Path,
    private_home: Path,
    token: str,
    base_url: str,
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if ambient is None else ambient
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(private_home),
        "TMPDIR": str(private_home / "tmp"),
        "XDG_CACHE_HOME": str(private_home / "cache"),
        "XDG_CONFIG_HOME": str(config_root),
        "XDG_DATA_HOME": str(private_home / "data"),
        "XDG_STATE_HOME": str(private_home / "state"),
        "OPENAI_API_KEY": token,
        "OPENAI_BASE_URL": base_url,
    }
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "LANG", "LC_ALL", "TZ"):
        if value := source.get(name):
            environment[name] = value
    for name in environment:
        upper = name.upper()
        if name not in {"OPENAI_API_KEY", "OPENAI_BASE_URL"} and any(
            marker in upper for marker in PROVIDER_ENV_MARKERS
        ):
            raise ValueError("child environment allowlist admitted a credential")
    return environment


def _write_project(root: Path, task_source: Path) -> tuple[Path, Path]:
    project = root / "project"
    task = project / "tasks/authority-fencing"
    task.parent.mkdir(parents=True)
    _copy_verified_tree(task_source, task)
    benchmarks = project / "benchmarks"
    benchmarks.mkdir()
    (benchmarks / "systems.md").write_text("# systems-design\n")
    (benchmarks / "github.md").write_text("# github-workflow\n")
    (benchmarks / "catalog.toml").write_text(
        "schema_version = 1\n"
        "[sections.systems-design]\n"
        'readme = "systems.md"\n'
        'tasks = [{ id = "authority-fencing", '
        'harbor_task = "tasks/authority-fencing", '
        'reward_policy = "binary" }]\n'
        "[sections.github-workflow]\n"
        'readme = "github.md"\n'
        "tasks = []\n"
    )
    (project / "tetrabench.toml").write_text(
        """schema_version = 1
catalog_path = "benchmarks/catalog.toml"
[controller]
kind = "modal"
[execution]
kind = "modal"
"""
    )
    config_root = root / "config"
    config = config_root / "tetrabench/config.toml"
    config.parent.mkdir(parents=True)
    lines = ["schema_version = 1"]
    for profile, model_group in PROFILES:
        model = f"openai/{model_group}"
        lines.extend(
            [
                f"[profiles.{profile}.controller]",
                'kind = "local"',
                f"[profiles.{profile}.execution]",
                'kind = "docker"',
                f"[profiles.{profile}.selection]",
                'include = ["authority-fencing"]',
                f"[profiles.{profile}.harbor]",
                'agent_name = "opencode"',
                f'model_name = "{model}"',
                "attempts = 1",
                "concurrency = 1",
            ]
        )
    config.write_text("\n".join(lines) + "\n")
    return project, config_root


def _validate_metrics(record: dict[str, Any]) -> dict[str, Any]:
    trajectory = record.get("trajectory")
    if not isinstance(trajectory, dict):
        raise ValueError("OpenCode ATIF trajectory is missing")
    metrics = trajectory.get("final_metrics")
    if not isinstance(metrics, dict):
        raise ValueError("OpenCode ATIF token metrics are missing")
    retained: dict[str, Any] = {}
    for name in (
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cached_tokens",
    ):
        value = metrics.get(name)
        if type(value) is not int or value < 0:
            raise ValueError("OpenCode ATIF metrics are invalid")
        retained[name] = value
    prompt = retained["total_prompt_tokens"]
    completion = retained["total_completion_tokens"]
    cached = retained["total_cached_tokens"]
    if cached > prompt or prompt <= 0 or completion <= 0:
        raise ValueError("OpenCode ATIF token metrics are incoherent or empty")
    steps = metrics.get("total_steps")
    if (
        type(steps) is not int
        or steps <= 0
        or steps != record["trajectory"]["step_count"]
    ):
        raise ValueError("OpenCode ATIF step metrics are incoherent")
    retained["total_steps"] = steps
    retained["total_tokens"] = prompt + completion
    cost = metrics.get("total_cost_usd")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError("OpenCode ATIF cost metric is invalid")
    retained["reported_cost_usd"] = str(cost) if cost is not None else None
    return retained


def _validate_harbor_metrics(
    native: NativeSnapshot,
    *,
    trial_name: str,
    trajectory_metrics: dict[str, Any],
) -> dict[str, Any]:
    job = JobResult.model_validate_json(native.read("harbor-job/result.json"))
    trial = TrialResult.model_validate_json(
        native.read(f"harbor-job/{trial_name}/result.json")
    )
    context = trial.agent_result
    if context is None:
        raise ValueError("native Harbor result omitted OpenCode metrics")
    harbor_metrics = {
        "cache_tokens": context.n_cache_tokens,
        "input_tokens": context.n_input_tokens,
        "output_tokens": context.n_output_tokens,
        "reported_cost_usd": (
            str(context.cost_usd) if context.cost_usd is not None else None
        ),
    }
    if harbor_metrics != {
        "cache_tokens": trajectory_metrics["total_cached_tokens"],
        "input_tokens": trajectory_metrics["total_prompt_tokens"],
        "output_tokens": trajectory_metrics["total_completion_tokens"],
        "reported_cost_usd": trajectory_metrics["reported_cost_usd"],
    }:
        raise ValueError("native Harbor and ATIF metrics disagree")
    if (
        job.stats.n_cache_tokens != context.n_cache_tokens
        or job.stats.n_input_tokens != context.n_input_tokens
        or job.stats.n_output_tokens != context.n_output_tokens
        or job.stats.cost_usd != context.cost_usd
    ):
        raise ValueError("native Harbor job and trial metrics disagree")
    return harbor_metrics


def _started_attempt(*, ordinal: int, profile: str, model_group: str) -> dict[str, Any]:
    return {
        "admissible": False,
        "broker": {
            "locked_endpoint": None,
            "request_count": 0,
            "requests": [],
        },
        "model": model_group,
        "model_group": model_group,
        "ordinal": ordinal,
        "outcome": "started",
        "profile": profile,
        "spend": {
            "known_actual_cost_usd": "0",
            "retained_unknown_reservation_usd": "0",
            "total_exposure_usd": "0",
        },
    }


def _broker_attempt_evidence(
    broker: CalibrationBroker,
) -> tuple[dict[str, Any], dict[str, str]]:
    with broker.state.lock:
        endpoint = broker.state.locked_endpoint
        request_count = broker.state.request_count
        records = tuple(broker.state.records)
    known = sum(
        (Decimal(item.cost) for item in records if item.cost is not None),
        start=Decimal(0),
    )
    retained = sum(
        (Decimal(item.retained_unknown_reservation_usd) for item in records),
        start=Decimal(0),
    )
    return (
        {
            "locked_endpoint": endpoint,
            "request_count": request_count,
            "requests": [dataclasses.asdict(item) for item in records],
            "shutdown": {
                "ephemeral_authority_revoked": not broker.state.active,
                "listener": broker.server is not None,
                "threads": 0 if broker.server is None else broker.server.worker_count,
            },
        },
        {
            "known_actual_cost_usd": str(known),
            "retained_unknown_reservation_usd": str(retained),
            "total_exposure_usd": str(known + retained),
        },
    )


def _mark_attempt_failed(
    attempt: dict[str, Any],
    error: BaseException,
    broker: CalibrationBroker | None = None,
) -> None:
    if broker is not None:
        broker_evidence, spend = _broker_attempt_evidence(broker)
        attempt["broker"] = broker_evidence
        attempt["spend"] = spend
    attempt["admissible"] = False
    attempt["error"] = safe_error(error)
    attempt["outcome"] = "failed"


def _run_attempt(
    *,
    ordinal: int,
    profile: str,
    model_group: str,
    installed_cli: InstalledCLI,
    project: Path,
    config_root: Path,
    private_root: Path,
    host: str,
    port: int,
    parent_key: str,
    ledger: SpendLedger,
    model_pricing: dict[str, Any],
    expected_task_checksum: str,
    expected_task_digest: str,
    attempt_record: dict[str, Any],
) -> dict[str, Any]:
    model = model_group
    harbor_model = f"openai/{model_group}"
    attempt_root = private_root / f"attempt-{ordinal}"
    attempt_root.mkdir(mode=0o700)
    home = attempt_root / "home"
    home.mkdir(mode=0o700)
    for name in ("tmp", "cache", "data", "state"):
        (home / name).mkdir(mode=0o700)
    output = attempt_root / "output"
    started = time.monotonic()
    broker = CalibrationBroker(
        host=host,
        port=port,
        parent_key=parent_key,
        model=model,
        ledger=ledger,
        max_input_tokens=model_pricing["max_input_tokens"],
    )
    try:
        try:
            broker.start()
            environment = child_environment(
                config_root=config_root,
                private_home=home,
                token=broker.token,
                base_url=f"http://{host}:{broker.port}/v1",
            )
            result = _bounded_command(
                [
                    str(installed_cli.executable),
                    "run",
                    "systems-design",
                    "--profile",
                    profile,
                    "--output",
                    str(output),
                    "--json",
                ],
                cwd=project,
                env=environment,
                timeout=MAX_ATTEMPT_SECONDS,
            )
        finally:
            broker.stop()
    except BaseException as error:
        _mark_attempt_failed(attempt_record, error, broker)
        raise
    broker_evidence, spend = _broker_attempt_evidence(broker)
    attempt_record["broker"] = broker_evidence
    attempt_record["spend"] = spend
    attempt_record["outcome"] = "failed"
    duration = time.monotonic() - started
    if result.stderr:
        raise ValueError("production calibration CLI stderr was not empty")
    document = strict_json(result.stdout)
    if (
        not isinstance(document, dict)
        or result.stdout != (canonical(document) + "\n").encode()
    ):
        raise ValueError("production calibration CLI output was not canonical JSON")
    if set(document) != {
        "job_directory",
        "outcome",
        "reward",
        "schema_version",
        "summary",
    }:
        raise ValueError("production calibration CLI JSON schema changed")
    if document["outcome"] != "succeeded" or document["reward"] not in {"0", "1"}:
        raise ValueError("production calibration did not complete normally")
    reward = int(document["reward"])
    native: NativeSnapshot = snapshot_native_output(output)
    record = _native_run_record(
        native,
        document,
        ordinal=ordinal,
        expected_task_checksum=expected_task_checksum,
        expected_task_digest=expected_task_digest,
        expected_agent_name="opencode",
        expected_model_name=harbor_model,
        expected_reward=reward,
        require_atif=True,
    )
    if record["native"]["trial"]["agent"]["name"] != "opencode":
        raise ValueError("native OpenCode agent identity mismatch")
    if record["trajectory"]["agent"]["model_name"] != harbor_model:
        raise ValueError("OpenCode ATIF model identity mismatch")
    trial_name = record["native"]["trial"]["trial_name"]
    diagnostics_path = f"harbor-job/{trial_name}/verifier/diagnostics.json"
    diagnostics = strict_json(native.read(diagnostics_path))
    gates = diagnostics.get("gates") if isinstance(diagnostics, dict) else None
    if (
        not isinstance(gates, dict)
        or set(gates) != set(GATES)
        or any(
            type(value) is not int or value not in {0, 1} for value in gates.values()
        )
        or diagnostics.get("mandatory_gate_count") != len(GATES)
        or diagnostics.get("mandatory_gate_pass_count") != sum(gates.values())
        or diagnostics.get("ok") is not (reward == 1)
        or diagnostics.get("schema_version") != 1
        or diagnostics.get("task_id") != TASK_ID
    ):
        raise ValueError("trusted verifier gate diagnostics mismatch")
    if not broker.state.records:
        raise ValueError("completed calibration attempt made no model request")
    if broker.state.locked_endpoint is None or any(
        item.endpoint != broker.state.locked_endpoint for item in broker.state.records
    ):
        raise ValueError("calibration attempt endpoint lock evidence is invalid")
    if ledger.fatal is not None:
        raise ValueError(ledger.fatal)
    trajectory_metrics = _validate_metrics(record)
    completed = {
        "broker": broker_evidence,
        "containment": result.containment,
        "duration_seconds": str(round(duration, 3)),
        "gates": gates,
        "metrics": {
            "atif": trajectory_metrics,
            "harbor": _validate_harbor_metrics(
                native,
                trial_name=trial_name,
                trajectory_metrics=trajectory_metrics,
            ),
        },
        "native": record["native"],
        "native_output_manifest_sha256": record["output_snapshot"]["manifest_sha256"],
        "ordinal": ordinal,
        "profile": profile,
        "reward": reward,
        "model": model_group,
        "model_group": model_group,
        "outcome": "succeeded",
        "trajectory": {
            "agent": record["trajectory"]["agent"],
            "schema_version": record["trajectory"]["schema_version"],
            "sha256": record["trajectory"]["sha256"],
            "step_count": record["trajectory"]["step_count"],
        },
        "verifier": {
            "artifact_manifest_sha256": record["artifact_manifest"]["sha256"],
            "environment_mode": "separate",
            "summary": record["cli"]["summary"],
        },
        "spend": spend,
    }
    attempt_record.clear()
    attempt_record.update(completed)
    return attempt_record


def _failure(
    args: argparse.Namespace,
    error: BaseException,
    *,
    attempts: list[dict[str, Any]] | None = None,
    pricing: PricingSnapshot | None = None,
    topology: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed = attempts or []
    known = sum(
        (
            Decimal(item.get("spend", {}).get("known_actual_cost_usd", "0"))
            for item in completed
        ),
        start=Decimal(0),
    )
    retained = sum(
        (
            Decimal(item.get("spend", {}).get("retained_unknown_reservation_usd", "0"))
            for item in completed
        ),
        start=Decimal(0),
    )
    return {
        "admissible": False,
        "attempt_count": len(completed),
        "attempts": completed,
        "attempts_per_profile": args.attempts_per_profile,
        "error": safe_error(error),
        "ok": False,
        "schema_version": 1,
        "spend_exposure": {
            "known_actual_cost_usd": str(known),
            "retained_unknown_reservation_usd": str(retained),
            "total_usd": str(known + retained),
        },
        "task_id": TASK_ID,
        "topology": topology,
        "total_authoritative_cost_usd": str(known),
        "pricing": (
            {"models": list(pricing.models), "sha256": pricing.sha256}
            if pricing is not None
            else None
        ),
    }


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write((canonical(value) + "\n").encode())
    sys.stdout.buffer.flush()


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-address")
    parser.add_argument("--broker-port", type=int, default=DEFAULT_BROKER_PORT)
    parser.add_argument("--attempts-per-profile", type=int)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    expected_attempts = 1 if args.debug else 2
    if args.attempts_per_profile is None:
        args.attempts_per_profile = expected_attempts
    elif args.attempts_per_profile != expected_attempts:
        parser.error(
            "debug requires exactly one attempt per profile; proof requires exactly two"
        )
    if args.output is not None and (args.debug or args.attempts_per_profile != 2):
        parser.error("--output requires a clean exact-four calibration")
    if not MIN_BROKER_PORT <= args.broker_port <= 65535:
        parser.error("--broker-port must be a fixed high port from 49152 through 65535")
    if not args.debug and args.host_address is not None:
        parser.error("proof mode discovers the Docker default bridge gateway")
    if args.debug:
        args.host_address = args.host_address or "127.0.0.1"
        try:
            address = ipaddress.ip_address(args.host_address)
        except ValueError:
            parser.error("debug host address must be an explicit loopback IP")
        if address.version != 4 or not address.is_loopback:
            parser.error("debug host address must be IPv4 loopback")
    return args


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_arguments(effective_argv)
    authority: ProofOutputAuthority | None = None
    attempts: list[dict[str, Any]] = []
    ledger: SpendLedger | None = None
    pricing: PricingSnapshot | None = None
    topology_evidence: dict[str, Any] | None = None
    try:
        if args.output is not None:
            authority = open_proof_output_authority(args.output)
        parent_key = os.environ.get(PARENT_KEY_ENV)
        if not parent_key:
            raise RuntimeError("calibration gateway key environment is required")
        if args.debug:
            host = args.host_address
        else:
            topology = discover_docker_bridge_topology()
            host = topology.gateway
            topology_evidence = _topology_evidence(
                topology, port=args.broker_port, reachable=False
            )
            topology_evidence = probe_docker_bridge_reachability(
                topology=topology,
                port=args.broker_port,
            )
        pricing = query_model_pricing(parent_key=parent_key)
        pricing_by_model = {item["model_group"]: item for item in pricing.models}
        with tempfile.TemporaryDirectory(prefix="authority-calibration-") as directory:
            private_root = Path(directory)
            snapshot: SourceSnapshot = (
                create_debug_source_snapshot(private_root)
                if args.debug
                else create_clean_source_snapshot(private_root)
            )
            manifest = source_manifest(
                tuple(snapshot.root / path for path in SOURCE_RELATIVE_PATHS),
                root=snapshot.root,
            )
            installed_cli = install_snapshot_cli(snapshot, private_root)
            execution_root = private_root / "execution"
            execution_root.mkdir(mode=0o700)
            project, config_root = _write_project(execution_root, snapshot.task)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                checksum = Task(project / "tasks/authority-fencing").checksum
            digest = (
                "sha256:"
                + Packager.compute_content_hash(project / "tasks/authority-fencing")[0]
            )
            ledger = SpendLedger()
            ordinal = 0
            for profile, model_group in PROFILES:
                for _ in range(args.attempts_per_profile):
                    ordinal += 1
                    attempt_record = _started_attempt(
                        ordinal=ordinal,
                        profile=profile,
                        model_group=model_group,
                    )
                    attempts.append(attempt_record)
                    try:
                        _run_attempt(
                            ordinal=ordinal,
                            profile=profile,
                            model_group=model_group,
                            installed_cli=installed_cli,
                            project=project,
                            config_root=config_root,
                            private_root=execution_root,
                            host=host,
                            port=args.broker_port,
                            parent_key=parent_key,
                            ledger=ledger,
                            model_pricing=pricing_by_model[model_group],
                            expected_task_checksum=checksum,
                            expected_task_digest=digest,
                            attempt_record=attempt_record,
                        )
                    except BaseException as error:
                        if attempt_record.get("outcome") != "failed":
                            _mark_attempt_failed(attempt_record, error)
                        elif "error" not in attempt_record:
                            attempt_record["error"] = safe_error(error)
                        raise
            source_unchanged = (
                source_manifest(
                    tuple(snapshot.root / path for path in SOURCE_RELATIVE_PATHS),
                    root=snapshot.root,
                )
                == manifest
            )
            exact_four = len(attempts) == 4 and args.attempts_per_profile == 2
            recorded_cost = sum(
                (
                    Decimal(request["cost"])
                    for attempt in attempts
                    for request in attempt["broker"]["requests"]
                    if request["cost"] is not None
                ),
                start=Decimal(0),
            )
            actual_within_reservations = all(
                request["cost"] is not None
                and request["settlement"] == "settled"
                and Decimal(request["cost"])
                <= Decimal(request["worst_case_reservation_usd"])
                for attempt in attempts
                for request in attempt["broker"]["requests"]
            )
            admissible = (
                not args.debug
                and snapshot.source_state == "clean"
                and exact_four
                and source_unchanged
                and ledger.fatal is None
                and ledger.cost <= MAX_TOTAL_COST
                and ledger.reserved == 0
                and recorded_cost == ledger.cost
                and actual_within_reservations
            )
            installed = {
                item["name"].lower(): item["version"]
                for item in installed_cli.attestation["distribution"][
                    "installed_distributions"
                ]
            }
            evidence = {
                "admissible": admissible,
                "attempt_count": len(attempts),
                "attempts": attempts,
                "attempts_per_profile": args.attempts_per_profile,
                "budget": {
                    "input_and_cache_ceiling_per_million_usd": "10",
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                    "output_ceiling_per_million_usd": "50",
                    "actual_within_reservations": actual_within_reservations,
                    "reservation_safety_margin_usd": str(RESERVATION_SAFETY_MARGIN),
                    "recorded_actual_cost_usd": str(recorded_cost),
                    "total_authoritative_cost_usd": str(ledger.cost),
                    "total_cap_usd": str(MAX_TOTAL_COST),
                },
                "broad_virtual_key_revoked": False,
                "broad_virtual_key_revocation_note": (
                    "not revoked because it remains in OpenCode auth; "
                    "no key file was created"
                ),
                "cli_distribution": installed_cli.attestation,
                "command": [
                    "python",
                    "tools/run_authority_fencing_calibration.py",
                    *evidence_argv(effective_argv),
                ],
                "debug": args.debug,
                "ok": admissible,
                "profiles": [
                    {
                        "harbor_model": f"openai/{model_group}",
                        "model_group": model_group,
                        "name": name,
                    }
                    for name, model_group in PROFILES
                ],
                "pricing": {
                    "models": list(pricing.models),
                    "sha256": pricing.sha256,
                },
                "schema_version": 1,
                "source_snapshot": {
                    "archive_sha256": snapshot.archive_sha256,
                    "manifest": manifest,
                    "manifest_sha256": manifest_digest(manifest),
                    "mode": snapshot.mode,
                    "revision": snapshot.revision,
                    "state": snapshot.source_state,
                    "task_context_sha256": tree_digest(snapshot.task),
                    "verifier_context_sha256": manifest_digest(
                        tree_manifest(snapshot.tests)
                    ),
                },
                "task_id": TASK_ID,
                "topology": topology_evidence,
                "versions": {
                    "harbor": installed["harbor"],
                    "tetrabench": installed["tetrabench"],
                    "python": installed_cli.attestation["python"]["version"],
                    "opencode": sorted(
                        {
                            attempt["trajectory"]["agent"]["version"]
                            for attempt in attempts
                        }
                    ),
                },
            }
            encoded = (canonical(evidence) + "\n").encode()
            if authority is not None and admissible:
                write_exclusive_proof(authority, encoded)
            _emit(evidence)
            return 0 if admissible else 1
    except BaseException as error:
        _emit(
            _failure(
                args,
                error,
                attempts=attempts,
                pricing=pricing,
                topology=topology_evidence,
            )
        )
        return 1
    finally:
        if authority is not None:
            authority.close()


if __name__ == "__main__":
    raise SystemExit(main())
