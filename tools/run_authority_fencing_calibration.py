#!/usr/bin/env python3
"""Run the credential-brokered authority-fencing OpenCode calibration."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import signal
import socket
import ssl
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
import urllib.parse
import warnings
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

BROKER_CHILD_MODE = len(sys.argv) > 1 and sys.argv[1] == "__broker"
if not BROKER_CHILD_MODE:
    from harbor.models.job.result import JobResult
    from harbor.models.task.task import Task
    from harbor.models.trial.result import TrialResult
    from harbor.publisher.packager import Packager

    from tools.run_authority_fencing_admission import (
        GATES,
        ROOT,
        TASK,
        CommandResult,
        InstalledCLI,
        NativeSnapshot,
        ProofOutputAuthority,
        SourceSnapshot,
        _bounded_command,
        _copy_verified_tree,
        _native_run_record,
        create_clean_source_snapshot,
        create_debug_source_snapshot,
        evidence_argv,
        install_snapshot_cli,
        manifest_digest,
        open_proof_output_authority,
        safe_error,
        snapshot_native_output,
        source_manifest,
        tree_digest,
        tree_manifest,
        write_exclusive_proof,
    )
else:
    ROOT = REPOSITORY_ROOT
    TASK = ROOT / "benchmarks/tasks/systems-design/authority-fencing"
    GATES = ()


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def strict_json(data: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("JSON contains duplicate keys")
            result[key] = item
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error


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
HEARTBEAT_INTERVAL_SECONDS = 0.5
HEARTBEAT_LEASE_SECONDS = 2.0
PARENT_DEATH_BOUND_SECONDS = 5.0
CALIBRATION_OWNER_LABEL = "org.tetrabench.calibration.owner"
CALIBRATION_OWNER = "authority-fencing-calibration"
BROKER_IMAGE = (
    "python:3.12.11-slim-bookworm@sha256:"
    "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
)
MAX_BODY_BYTES = 512 << 10
MAX_HEADER_BYTES = 16 << 10
MAX_RESPONSE_BYTES = 64 << 20
MODEL_INFO_MAX_RESPONSE_BYTES = 4 << 20
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
class TaskOverlay:
    task: Path
    candidate_manifest: list[dict[str, Any]]
    candidate_manifest_sha256: str
    overlay_manifest: list[dict[str, Any]]
    overlay_manifest_sha256: str
    compose_sha256: str


@dataclasses.dataclass(frozen=True, slots=True)
class DockerAttemptNames:
    run_id: str
    ordinal: int
    network: str
    broker: str
    probe: str
    alias: str

    @property
    def labels(self) -> dict[str, str]:
        return {
            CALIBRATION_OWNER_LABEL: CALIBRATION_OWNER,
            "org.tetrabench.calibration.run": self.run_id,
            "org.tetrabench.calibration.attempt": str(self.ordinal),
        }

    def role_labels(self, role: str) -> dict[str, str]:
        return {**self.labels, "org.tetrabench.calibration.role": role}


@dataclasses.dataclass(frozen=True, slots=True)
class Reservation:
    identifier: int
    amount: Decimal


@dataclasses.dataclass(slots=True)
class ForwardingAdmission:
    started: bool = False


class SpendLedger:
    """Atomically own actual spend plus exact in-flight worst-case reservations."""

    def __init__(self, max_total: Decimal = MAX_TOTAL_COST) -> None:
        if not max_total.is_finite() or max_total <= 0 or max_total > MAX_TOTAL_COST:
            raise ValueError("calibration ledger cap is invalid")
        self._lock = threading.Lock()
        self._max_total = max_total
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
            if projected > self._max_total:
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
            if self._cost + self._reserved + cost > self._max_total:
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
        token: str | None,
        probe_token: str | None,
        model: str,
        max_input_tokens: int,
        deadline: float,
        ledger: SpendLedger,
        upstream_url: str = UPSTREAM_URL,
        evidence_path: Path | None = None,
        fake_response_cost: Decimal | None = None,
        pricing_sha256: str | None = None,
        shutdown_event: threading.Event | None = None,
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
        self.evidence_path = evidence_path
        self.evidence_lock = threading.Lock()
        self.fake_response_cost = fake_response_cost
        self.pricing_sha256 = pricing_sha256
        self.shutdown_event = shutdown_event
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)
        self.request_count = 0
        self.locked_endpoint: str | None = None
        self.active = token is not None
        self.activated = token is not None
        self.last_heartbeat: float | None = (
            time.monotonic() if token is not None else None
        )
        self.lease_channel_identity: tuple[int, int] | None = None
        self.records: list[RequestRecord] = []
        self.upstreams: set[http.client.HTTPConnection] = set()

    def write_evidence(self) -> None:
        path = self.evidence_path
        if path is None:
            return
        with self.evidence_lock:
            with self.lock:
                document = {
                    "active": self.active,
                    "activated": self.activated,
                    "fatal": self.ledger.fatal,
                    "known_actual_cost_usd": str(self.ledger.cost),
                    "locked_endpoint": self.locked_endpoint,
                    "pricing_sha256": self.pricing_sha256,
                    "probe_consumed": self._probe_consumed,
                    "request_count": self.request_count,
                    "requests": [dataclasses.asdict(item) for item in self.records],
                    "retained_unknown_reservation_usd": str(self.ledger.reserved),
                    "schema_version": 1,
                    "lease": {
                        "channel": "anonymous-stdin-pipe",
                        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
                        "lease_seconds": HEARTBEAT_LEASE_SECONDS,
                    },
                }
            encoded = (canonical(document) + "\n").encode()
            if len(encoded) > 1 << 20:
                raise RuntimeError("broker evidence exceeds limit")
            temporary = path.with_name(f".{path.name}.tmp")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
                0o600,
            )
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("broker evidence write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            directory = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _expired_locked(self, now: float) -> bool:
        return now >= self.deadline or bool(
            self.activated
            and (
                self.last_heartbeat is None
                or now - self.last_heartbeat >= HEARTBEAT_LEASE_SECONDS
            )
        )

    def _invalidate_locked(self) -> None:
        self.active = False
        self._token = None
        self._probe_token = None
        self.parent_key = ""
        if self.shutdown_event is not None:
            self.shutdown_event.set()
        upstreams = tuple(self.upstreams)
        self.upstreams.clear()
        for upstream in upstreams:
            upstream.close()

    def authorize(self, headers: Any) -> None:
        values = headers.get_all("Authorization", [])
        expired = False
        with self.lock:
            if self._expired_locked(time.monotonic()):
                self._invalidate_locked()
                expired = True
                accepted = False
            else:
                token = self._token
                accepted = bool(
                    self.active
                    and token is not None
                    and len(values) == 1
                    and secrets.compare_digest(values[0], f"Bearer {token}")
                )
        if expired:
            self.write_evidence()
        if not accepted:
            raise PermissionError("broker authorization rejected")

    def consume_probe(self, headers: Any) -> None:
        values = headers.get_all("Authorization", [])
        expired = False
        with self.lock:
            if self._expired_locked(time.monotonic()):
                self._invalidate_locked()
                expired = True
                accepted = False
            else:
                token = self._probe_token
                accepted = bool(
                    token is not None
                    and len(values) == 1
                    and secrets.compare_digest(values[0], f"Bearer {token}")
                )
                if accepted:
                    self._probe_token = None
                    self._probe_consumed = True
        if expired:
            self.write_evidence()
        if not accepted:
            raise PermissionError("broker probe authorization rejected")
        self.write_evidence()

    def activate(self, token: str, channel_identity: tuple[int, int]) -> None:
        if len(token) < 32:
            raise ValueError("broker activation token rejected")
        expired = False
        with self.lock:
            now = time.monotonic()
            if self._expired_locked(now):
                self._invalidate_locked()
                expired = True
            elif self.activated or self.active or not self._probe_consumed:
                raise RuntimeError("broker activation ordering rejected")
            else:
                self._token = token
                self.active = True
                self.activated = True
                self.last_heartbeat = now
                self.lease_channel_identity = channel_identity
        if expired:
            self.write_evidence()
            raise PermissionError("broker activation expired")
        self.write_evidence()

    def heartbeat(self, channel_identity: tuple[int, int]) -> None:
        expired = False
        with self.lock:
            now = time.monotonic()
            if self._expired_locked(now):
                self._invalidate_locked()
                expired = True
                accepted = False
            else:
                accepted = bool(
                    self.active
                    and self.activated
                    and self.lease_channel_identity == channel_identity
                )
                if accepted:
                    self.last_heartbeat = now
        if expired:
            self.write_evidence()
        if not accepted:
            raise PermissionError("broker heartbeat rejected")

    def lease_expired(self) -> bool:
        with self.lock:
            return (self.activated and not self.active) or self._expired_locked(
                time.monotonic()
            )

    @property
    def probe_consumed(self) -> bool:
        with self.lock:
            return self._probe_consumed

    def begin_request(
        self, endpoint: str, reservation_amount: Decimal
    ) -> tuple[int, Reservation]:
        if not self.semaphore.acquire(blocking=False):
            raise BlockingIOError("broker concurrency limit reached")
        expired = False
        try:
            with self.lock:
                if self._expired_locked(time.monotonic()):
                    self._invalidate_locked()
                    expired = True
                    raise PermissionError("broker attempt expired")
                if not self.active:
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
            if expired:
                self.write_evidence()
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
            self._invalidate_locked()
        self.write_evidence()

    def send_connected_request(
        self,
        upstream: http.client.HTTPConnection,
        child_headers: Any,
        endpoint: str,
        body: bytes,
        forwarded_headers: dict[str, str],
        admission: ForwardingAdmission,
    ) -> None:
        expired = False
        with self.lock:
            if upstream.sock is None:
                raise RuntimeError("upstream is not connected")
            upstream.auto_open = 0
            self.upstreams.add(upstream)
            if self._expired_locked(time.monotonic()):
                self._invalidate_locked()
                expired = True
                accepted = False
            else:
                values = child_headers.get_all("Authorization", [])
                token = self._token
                accepted = bool(
                    self.active
                    and token is not None
                    and len(values) == 1
                    and secrets.compare_digest(values[0], f"Bearer {token}")
                )
            if accepted:
                parent_key = self.parent_key
                admission.started = True
                upstream.request(
                    "POST",
                    endpoint,
                    body=body,
                    headers={
                        **forwarded_headers,
                        "Authorization": f"Bearer {parent_key}",
                        "Content-Length": str(len(body)),
                    },
                )
            else:
                self.upstreams.discard(upstream)
                upstream.close()
        if expired:
            self.write_evidence()
        if not accepted:
            raise PermissionError("broker attempt expired")

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
        content_lengths = response.headers.get_all("Content-Length", [])
        transfer_encodings = response.headers.get_all("Transfer-Encoding", [])
        if (
            len(content_lengths) > 1
            or (content_lengths and not content_lengths[0].isdigit())
            or (content_lengths and transfer_encodings)
            or len(transfer_encodings) > 1
            or (
                transfer_encodings
                and transfer_encodings[0].strip().lower() != "chunked"
            )
        ):
            raise ValueError("gateway model-info response framing is ambiguous")
        declared_length = int(content_lengths[0]) if content_lengths else None
        if (
            declared_length is not None
            and declared_length > MODEL_INFO_MAX_RESPONSE_BYTES
        ):
            raise ValueError("gateway model-info response exceeds limit")
        try:
            body = response.read(MODEL_INFO_MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise ValueError("gateway model-info response is truncated") from error
        if len(body) > MODEL_INFO_MAX_RESPONSE_BYTES:
            raise ValueError("gateway model-info response exceeds limit")
        if declared_length is not None and len(body) != declared_length:
            raise ValueError("gateway model-info response is truncated")
        if response.status != HTTPStatus.OK:
            raise ValueError("gateway model-info request failed")
        return _validate_pricing_document(strict_json(body))
    finally:
        connection.close()


DOCKER_NAME = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,62}\Z")


def _safe_docker_name(value: str, field: str) -> str:
    if not DOCKER_NAME.fullmatch(value):
        raise ValueError(f"unsafe Docker {field}")
    return value


def _docker_names(run_id: str, ordinal: int) -> DockerAttemptNames:
    _safe_docker_name(run_id, "run id")
    if ordinal <= 0:
        raise ValueError("Docker attempt ordinal must be positive")
    nonce = secrets.token_hex(8)
    return DockerAttemptNames(
        run_id=run_id,
        ordinal=ordinal,
        network=_safe_docker_name(f"tb-cal-{ordinal}-{nonce}", "network name"),
        broker=_safe_docker_name(f"tb-broker-{ordinal}-{nonce}", "container name"),
        probe=_safe_docker_name(f"tb-probe-{ordinal}-{nonce}", "container name"),
        alias=_safe_docker_name(f"broker-{nonce}", "network alias"),
    )


def _docker(
    argv: list[str], *, check: bool = True, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    environment.pop(PARENT_KEY_ENV, None)
    return subprocess.run(  # nosec B603 B607
        ["docker", *argv],
        input=input_bytes,
        check=check,
        capture_output=True,
        env=environment,
        timeout=120,
    )


def _inspect_one(kind: str, name: str) -> dict[str, Any]:
    result = _docker([kind, "inspect", name])
    value = strict_json(result.stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError(f"Docker {kind} inspect schema rejected")
    return value[0]


def _resource_labels(kind: str, inspected: dict[str, Any]) -> dict[str, Any]:
    labels = (
        inspected.get("Labels")
        if kind == "network"
        else inspected.get("Config", {}).get("Labels")
    )
    if not isinstance(labels, dict):
        raise RuntimeError(f"Docker {kind} labels schema rejected")
    return labels


def _owned_resource_exists(
    kind: str, name: str, expected_labels: Mapping[str, str]
) -> bool:
    result = _docker([kind, "inspect", name], check=False)
    if result.returncode != 0:
        return False
    inspected = strict_json(result.stdout)
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise RuntimeError(f"Docker {kind} inspect schema rejected")
    labels = _resource_labels(kind, inspected[0])
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise RuntimeError(f"refusing unrelated Docker {kind} with owned name")
    return True


def _wait_resource_absent(kind: str, name: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _docker([kind, "inspect", name], check=False).returncode != 0:
            return True
        time.sleep(0.05)
    return _docker([kind, "inspect", name], check=False).returncode != 0


def _remove_owned_attempt_containers(names: DockerAttemptNames) -> None:
    result = _docker(
        [
            "ps",
            "-aq",
            "--filter",
            f"label=org.tetrabench.calibration.run={names.run_id}",
            "--filter",
            f"label=org.tetrabench.calibration.attempt={names.ordinal}",
            "--filter",
            f"label={CALIBRATION_OWNER_LABEL}={CALIBRATION_OWNER}",
        ],
        check=False,
    )
    for container_id in result.stdout.decode().split():
        inspected = _inspect_one("container", container_id)
        labels = _resource_labels("container", inspected)
        if all(labels.get(key) == value for key, value in names.labels.items()):
            _docker(["rm", "--force", container_id], check=False)


def sweep_stale_calibration_resources() -> dict[str, int]:
    """Remove only resources carrying the exact calibration-owner label."""
    removed_containers = 0
    removed_networks = 0
    containers = _docker(
        [
            "ps",
            "-aq",
            "--filter",
            f"label={CALIBRATION_OWNER_LABEL}={CALIBRATION_OWNER}",
        ],
        check=False,
    )
    for container_id in containers.stdout.decode().split():
        inspected = _inspect_one("container", container_id)
        if (
            _resource_labels("container", inspected).get(CALIBRATION_OWNER_LABEL)
            != CALIBRATION_OWNER
        ):
            continue
        if inspected.get("State", {}).get("Running") is True:
            raise RuntimeError("another labeled calibration container is active")
        _docker(["rm", "--force", container_id], check=False)
        if not _wait_resource_absent("container", container_id):
            raise RuntimeError("stale calibration container survived startup sweep")
        removed_containers += 1
    networks = _docker(
        [
            "network",
            "ls",
            "-q",
            "--filter",
            f"label={CALIBRATION_OWNER_LABEL}={CALIBRATION_OWNER}",
        ],
        check=False,
    )
    for network_id in networks.stdout.decode().split():
        inspected = _inspect_one("network", network_id)
        if (
            _resource_labels("network", inspected).get(CALIBRATION_OWNER_LABEL)
            != CALIBRATION_OWNER
        ):
            continue
        if inspected.get("Containers"):
            raise RuntimeError("labeled calibration network is not stale")
        _docker(["network", "rm", network_id], check=False)
        if not _wait_resource_absent("network", network_id):
            raise RuntimeError("stale calibration network survived startup sweep")
        removed_networks += 1
    return {"containers": removed_containers, "networks": removed_networks}


def sweep_calibration_run(run_id: str) -> dict[str, int]:
    _safe_docker_name(run_id, "run id")
    removed_containers = 0
    removed_networks = 0
    filters = [
        "--filter",
        f"label={CALIBRATION_OWNER_LABEL}={CALIBRATION_OWNER}",
        "--filter",
        f"label=org.tetrabench.calibration.run={run_id}",
    ]
    containers = _docker(["ps", "-aq", *filters], check=False)
    for container_id in containers.stdout.decode().split():
        inspected = _inspect_one("container", container_id)
        labels = _resource_labels("container", inspected)
        if (
            labels.get(CALIBRATION_OWNER_LABEL) != CALIBRATION_OWNER
            or labels.get("org.tetrabench.calibration.run") != run_id
        ):
            raise RuntimeError("Docker run cleanup label mismatch")
        _docker(["rm", "--force", container_id], check=False)
        removed_containers += 1
    networks = _docker(["network", "ls", "-q", *filters], check=False)
    for network_id in networks.stdout.decode().split():
        inspected = _inspect_one("network", network_id)
        labels = _resource_labels("network", inspected)
        if (
            labels.get(CALIBRATION_OWNER_LABEL) != CALIBRATION_OWNER
            or labels.get("org.tetrabench.calibration.run") != run_id
        ):
            raise RuntimeError("Docker run cleanup label mismatch")
        _docker(["network", "rm", network_id], check=False)
        if not _wait_resource_absent("network", network_id):
            raise RuntimeError("Docker run network survived finalization")
        removed_networks += 1
    remaining_containers = _docker(["ps", "-aq", *filters], check=False)
    remaining_networks = _docker(["network", "ls", "-q", *filters], check=False)
    if remaining_containers.stdout.strip() or remaining_networks.stdout.strip():
        raise RuntimeError("Docker run authority/resource survived finalization")
    return {"containers": removed_containers, "networks": removed_networks}


def _compose_bytes(names: DockerAttemptNames, candidate_manifest_sha256: str) -> bytes:
    network = names.network
    _safe_docker_name(network, "network name")
    main_labels = {
        **names.role_labels("main"),
        "org.tetrabench.calibration.candidate-manifest": candidate_manifest_sha256,
    }
    labels = "\n".join(
        f"      {json.dumps(key)}: {json.dumps(value)}"
        for key, value in main_labels.items()
    )
    return (
        "services:\n"
        "  main:\n"
        "    build:\n"
        "      context: .\n"
        "      dockerfile: Dockerfile\n"
        "    labels:\n"
        f"{labels}\n"
        "    healthcheck:\n"
        "      test:\n"
        "        - CMD\n"
        "        - python\n"
        "        - -c\n"
        "        - >-\n"
        f"          import urllib.request; urllib.request.urlopen('http://{names.alias}:"
        f"{DEFAULT_BROKER_PORT}/__tetrabench_ready', timeout=1)\n"
        "      interval: 200ms\n"
        "      timeout: 2s\n"
        "      retries: 1800\n"
        "      start_period: 1s\n"
        "    networks:\n"
        "      - calibration\n"
        "networks:\n"
        "  calibration:\n"
        "    external: true\n"
        f"    name: {network}\n"
    ).encode()


def create_task_overlay(
    candidate: Path, destination: Path, names: DockerAttemptNames | str
) -> TaskOverlay:
    if isinstance(names, str):
        # Retained only for focused overlay tests. Production always binds all labels.
        names = DockerAttemptNames(
            "overlay-test", 1, names, "broker-test", "probe-test", "broker-test"
        )
    candidate_manifest = tree_manifest(candidate)
    candidate_manifest_sha256 = manifest_digest(candidate_manifest)
    _copy_verified_tree(candidate, destination)
    compose = destination / "environment/docker-compose.yaml"
    compose.write_bytes(_compose_bytes(names, candidate_manifest_sha256))
    compose.chmod(0o644)
    overlay_manifest = tree_manifest(destination)
    compose_relative = "environment/docker-compose.yaml"
    candidate_without_compose = [
        item for item in candidate_manifest if item["path"] != compose_relative
    ]
    overlay_without_compose = [
        item for item in overlay_manifest if item["path"] != compose_relative
    ]
    if candidate_without_compose != overlay_without_compose:
        raise ValueError("calibration task overlay changed candidate bytes")
    if sum(item["path"] == compose_relative for item in overlay_manifest) != 1:
        raise ValueError("calibration task overlay composition is ambiguous")
    return TaskOverlay(
        task=destination,
        candidate_manifest=candidate_manifest,
        candidate_manifest_sha256=candidate_manifest_sha256,
        overlay_manifest=overlay_manifest,
        overlay_manifest_sha256=manifest_digest(overlay_manifest),
        compose_sha256=hashlib.sha256(compose.read_bytes()).hexdigest(),
    )


def _is_max_token_spelling(field: str) -> bool:
    normalized = "".join(
        character for character in field.lower() if character.isalnum()
    )
    return "max" in normalized and "token" in normalized


def _normalized_field(field: str) -> str:
    return "".join(character for character in field.lower() if character.isalnum())


MULTIPLICITY_FIELDS = frozenset(
    {
        "beamwidth",
        "bestof",
        "candidatecount",
        "candidates",
        "completioncount",
        "numberofcandidates",
        "numcandidates",
        "numcompletions",
        "numgenerations",
        "numoutputs",
        "numresponses",
        "numreturnsequences",
        "numsamples",
        "parallelsamples",
        "responsecount",
        "returnn",
        "samplecount",
    }
)
FORBIDDEN_MEDIA_FIELDS = frozenset(
    {
        "audio",
        "audioid",
        "blob",
        "blobid",
        "dataurl",
        "file",
        "filedata",
        "fileid",
        "filename",
        "image",
        "imageurl",
        "inputaudio",
        "inputfile",
        "inputimage",
        "media",
        "modalities",
        "prediction",
        "remoteurl",
        "urlreference",
    }
)
TEXT_CONTENT_TYPES = frozenset({"text", "input_text", "output_text"})
TOOL_CONTENT_TYPES = frozenset(
    {"tool_call", "tool_result", "function_call", "function_call_output"}
)


def _reject_media_fields(value: Any, *, within_content: bool = False) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("broker content key rejected")
            normalized = _normalized_field(key)
            if within_content and normalized in FORBIDDEN_MEDIA_FIELDS:
                raise ValueError("broker non-text content field rejected")
            _reject_media_fields(item, within_content=within_content)
    elif isinstance(value, list):
        for item in value:
            _reject_media_fields(item, within_content=within_content)


def _validate_content_blocks(content: Any) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise ValueError("broker content shape rejected")
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("broker content block rejected")
        block_type = block.get("type")
        if block_type not in TEXT_CONTENT_TYPES | TOOL_CONTENT_TYPES:
            raise ValueError("broker non-text content type rejected")
        _reject_media_fields(block, within_content=True)
        if block_type in TEXT_CONTENT_TYPES and not isinstance(block.get("text"), str):
            raise ValueError("broker text content rejected")


def _validate_chat_content(document: dict[str, Any]) -> None:
    messages = document.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("broker chat messages rejected")
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("broker chat message rejected")
        content = message.get("content")
        if content is None:
            tool_calls = message.get("tool_calls")
            if (
                not isinstance(tool_calls, list)
                or not tool_calls
                or not all(isinstance(call, dict) for call in tool_calls)
            ):
                raise ValueError("broker chat content rejected")
        else:
            _validate_content_blocks(content)
        _reject_media_fields(message, within_content=True)


def _validate_responses_content(document: dict[str, Any]) -> None:
    value = document.get("input")
    if isinstance(value, str):
        return
    if not isinstance(value, list) or not value:
        raise ValueError("broker responses input rejected")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("broker responses item rejected")
        item_type = item.get("type", "message")
        if item_type == "message":
            _validate_content_blocks(item.get("content"))
        elif item_type not in TOOL_CONTENT_TYPES:
            raise ValueError("broker responses content type rejected")
        _reject_media_fields(item, within_content=True)


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
    normalized_fields = {_normalized_field(field): field for field in document}
    forbidden_multiplicity = MULTIPLICITY_FIELDS & set(normalized_fields)
    if forbidden_multiplicity:
        raise ValueError("broker multiplicity field rejected")
    if any(normalized in FORBIDDEN_MEDIA_FIELDS for normalized in normalized_fields):
        raise ValueError("broker non-text modality rejected")
    stream = document.get("stream", False)
    if type(stream) is not bool:
        raise ValueError("broker stream mode rejected")
    if parsed.path == "/v1/responses":
        allowed_limit_fields = {"max_output_tokens"}
        injected_field = "max_output_tokens"
        if "n" in document or "background" in document:
            raise ValueError("broker responses multiplicity/background rejected")
        _validate_responses_content(document)
    else:
        allowed_limit_fields = {"max_completion_tokens", "max_tokens"}
        injected_field = "max_completion_tokens"
        n = document.get("n", 1)
        if type(n) is not int or n != 1:
            raise ValueError("broker requires exactly one completion")
        document["n"] = 1
        if "background" in document:
            raise ValueError("broker background mode rejected")
        _validate_chat_content(document)
    present = [field for field in allowed_limit_fields if field in document]
    if len(present) > 1:
        raise ValueError("broker output-token limit is duplicated")
    for field in document:
        normalized = _normalized_field(field)
        output_alias = "output" in normalized and any(
            marker in normalized for marker in ("token", "length", "limit")
        )
        token_alias = _is_max_token_spelling(field)
        if (token_alias or output_alias) and field not in allowed_limit_fields:
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


def _forward_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    headers = {
        "Content-Type": handler.headers.get("Content-Type", "application/json"),
        "Accept": handler.headers.get("Accept", "application/json"),
    }
    if value := handler.headers.get("User-Agent"):
        headers["User-Agent"] = value[:256]
    return headers


def _upstream_connection(
    parsed: urllib.parse.SplitResult,
) -> http.client.HTTPConnection:
    if parsed.hostname is None:
        raise ValueError("upstream host is missing")
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=BACKPRESSURE_TIMEOUT_SECONDS,
            context=ssl.create_default_context(),
        )
    if parsed.scheme == "http":
        return http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=BACKPRESSURE_TIMEOUT_SECONDS,
        )
    raise ValueError("upstream scheme rejected")


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
        if self.path == "/__tetrabench_ready":
            with self.state.lock:
                ready = self.state.active and self.state.activated
            if not ready:
                self._reject(HTTPStatus.SERVICE_UNAVAILABLE, "broker inactive")
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
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
        forwarding = ForwardingAdmission()
        try:
            if state.fake_response_cost is not None:
                response_body = b'{"ok":true}'
                status = HTTPStatus.OK
                cost = state.fake_response_cost
                settlement = "known_fatal"
                state.finish_request(reservation, cost)
                settlement = "settled"
                settlement_finalized = True
            else:
                parsed = urllib.parse.urlsplit(state.upstream_url)
                upstream = _upstream_connection(parsed)
                upstream.connect()
                state.send_connected_request(
                    upstream,
                    self.headers,
                    endpoint,
                    body,
                    _forward_headers(self),
                    forwarding,
                )
                potentially_paid = forwarding.started
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
                settlement = "known_fatal"
                state.finish_request(reservation, cost)
                settlement = "settled"
                settlement_finalized = True
            response_bytes = len(response_body)
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
            potentially_paid = potentially_paid or forwarding.started
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
                state.write_evidence()


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
        token: str | None = None,
        evidence_path: Path | None = None,
        fake_response_cost: Decimal | None = None,
        pricing_sha256: str | None = None,
        inactive: bool = False,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(48)
        self.state = BrokerState(
            parent_key=parent_key,
            token=None if inactive else self.token,
            probe_token=probe_token,
            model=model,
            max_input_tokens=max_input_tokens,
            deadline=time.monotonic() + timeout,
            ledger=ledger,
            upstream_url=upstream_url,
            evidence_path=evidence_path,
            fake_response_cost=fake_response_cost,
            pricing_sha256=pricing_sha256,
            shutdown_event=shutdown_event,
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

    def stop(self, *, max_wait: float = BACKPRESSURE_TIMEOUT_SECONDS + 5) -> None:
        deadline = time.monotonic() + max_wait
        self.state.invalidate()
        server, thread = self.server, self.thread
        if server is not None:
            server.shutdown()
            workers = server.abort_connections()
            server.server_close()
            for worker in workers:
                worker.join(timeout=max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    raise RuntimeError("broker request thread survived shutdown")
        if thread is not None:
            thread.join(timeout=max(0, deadline - time.monotonic()))
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


def _validate_broker_pricing_binding(
    models: Any, digest: str, model: str, max_input_tokens: int
) -> None:
    if not isinstance(models, list):
        raise ValueError("broker pricing snapshot schema rejected")
    pricing_digest = hashlib.sha256(canonical(models).encode()).hexdigest()
    if not secrets.compare_digest(pricing_digest, digest):
        raise ValueError("broker pricing snapshot digest mismatch")
    if not all(isinstance(item, dict) for item in models):
        raise ValueError("broker pricing snapshot schema rejected")
    selected = [item for item in models if item.get("model_group") == model]
    if len(selected) != 1 or selected[0].get("max_input_tokens") != max_input_tokens:
        raise ValueError("broker pricing model binding mismatch")


def _broker_child_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="calibration-broker")
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-input-tokens", type=int, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--budget-cap", required=True)
    parser.add_argument("--pricing-json", required=True)
    parser.add_argument("--pricing-sha256", required=True)
    parser.add_argument("--fake-response-cost")
    args = parser.parse_args(argv)
    models = strict_json(args.pricing_json.encode())
    _validate_broker_pricing_binding(
        models, args.pricing_sha256, args.model, args.max_input_tokens
    )
    raw = bytearray(sys.stdin.buffer.readline(65537))
    if len(raw) > 65536:
        raise ValueError("broker credential payload exceeds limit")
    try:
        credentials = strict_json(bytes(raw))
    finally:
        raw[:] = b"\0" * len(raw)
    if not isinstance(credentials, dict) or set(credentials) != {
        "parent_key",
        "probe_token",
    }:
        raise ValueError("broker credential payload schema rejected")
    if not all(
        isinstance(credentials[name], str) and len(credentials[name]) >= 32
        for name in credentials
    ):
        raise ValueError("broker credential payload rejected")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_args: stop.set())
    broker = CalibrationBroker(
        host="0.0.0.0",  # nosec B104 - isolated, un-published Docker network
        port=DEFAULT_BROKER_PORT,
        parent_key=credentials["parent_key"],
        token=None,
        probe_token=credentials["probe_token"],
        model=args.model,
        ledger=SpendLedger(Decimal(args.budget_cap)),
        max_input_tokens=args.max_input_tokens,
        timeout=args.timeout,
        upstream_url=args.upstream_url,
        evidence_path=Path("/evidence/ledger.json"),
        fake_response_cost=(
            Decimal(args.fake_response_cost)
            if args.fake_response_cost is not None
            else None
        ),
        pricing_sha256=args.pricing_sha256,
        inactive=True,
        shutdown_event=stop,
    )
    credentials["parent_key"] = ""  # nosec B105 - best-effort replacement
    credentials["probe_token"] = ""  # nosec B105 - best-effort replacement
    broker.start()
    broker.state.write_evidence()
    stdin_stat = os.fstat(sys.stdin.fileno())
    channel_identity = (stdin_stat.st_dev, stdin_stat.st_ino)

    def control_reader() -> None:
        try:
            for line in sys.stdin.buffer:
                current = os.fstat(sys.stdin.fileno())
                if (current.st_dev, current.st_ino) != channel_identity:
                    raise RuntimeError("broker control pipe identity changed")
                message = strict_json(line)
                if not isinstance(message, dict) or not isinstance(
                    message.get("op"), str
                ):
                    raise ValueError("broker control message rejected")
                if message["op"] == "activate" and set(message) == {"op", "token"}:
                    token = message.get("token")
                    if not isinstance(token, str):
                        raise ValueError("broker activation token rejected")
                    broker.state.activate(token, channel_identity)
                elif message["op"] == "heartbeat" and set(message) == {
                    "op",
                    "sequence",
                }:
                    sequence = message.get("sequence")
                    if type(sequence) is not int or sequence < 0:
                        raise ValueError("broker heartbeat sequence rejected")
                    broker.state.heartbeat(channel_identity)
                else:
                    raise ValueError("broker control message rejected")
        except BaseException:
            broker.state.invalidate()
        finally:
            stop.set()

    control_thread = threading.Thread(
        target=control_reader, name="calibration-broker-control"
    )
    control_thread.start()
    try:
        while not stop.wait(0.1):
            if broker.state.lease_expired():
                broker.state.invalidate()
                stop.set()
            if broker.state.ledger.fatal is not None:
                continue
    finally:
        broker.stop(max_wait=2.0)
        control_thread.join(timeout=PARENT_DEATH_BOUND_SECONDS)
        if control_thread.is_alive():
            raise RuntimeError("broker control thread survived shutdown")
    return 0


class DockerBrokerSidecar:
    def __init__(
        self,
        *,
        names: DockerAttemptNames,
        snapshot_root: Path,
        evidence_root: Path,
        parent_key: str,
        attempt_token: str,
        probe_token: str,
        model: str,
        pricing: PricingSnapshot,
        max_input_tokens: int,
        budget_cap: Decimal,
        upstream_url: str = UPSTREAM_URL,
        fake_response_cost: Decimal | None = None,
    ) -> None:
        self.names = names
        self.snapshot_root = snapshot_root
        self.evidence_root = evidence_root
        self.parent_key = parent_key
        self.attempt_token = attempt_token
        self.probe_token = probe_token
        self.model = model
        self.pricing = pricing
        self.max_input_tokens = max_input_tokens
        self.budget_cap = budget_cap
        self.upstream_url = upstream_url
        self.fake_response_cost = fake_response_cost
        self.process: subprocess.Popen[bytes] | None = None
        self.created_network = False
        self.created_container = False
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread: threading.Thread | None = None
        self.control_pipe_identity: tuple[int, int] | None = None
        self.activated = False

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("broker sidecar already started")
        network_label_args = [
            value
            for key, value in self.names.role_labels("network").items()
            for value in ("--label", f"{key}={value}")
        ]
        _docker(
            [
                "network",
                "create",
                "--driver",
                "bridge",
                *network_label_args,
                self.names.network,
            ]
        )
        self.created_network = True
        broker_label_args = [
            value
            for key, value in self.names.role_labels("broker").items()
            for value in ("--label", f"{key}={value}")
        ]
        argv = [
            "create",
            "--rm",
            "--name",
            self.names.broker,
            "--network",
            self.names.network,
            "--network-alias",
            self.names.alias,
            *broker_label_args,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",  # nosec B108
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--mount",
            f"type=bind,src={self.snapshot_root},dst=/source,readonly",
            "--mount",
            f"type=bind,src={self.evidence_root},dst=/evidence",
            "--interactive",
            BROKER_IMAGE,
            "python",
            "/source/tools/run_authority_fencing_calibration.py",
            "__broker",
            "--model",
            self.model,
            "--max-input-tokens",
            str(self.max_input_tokens),
            "--timeout",
            str(MAX_ATTEMPT_SECONDS),
            "--upstream-url",
            self.upstream_url,
            "--budget-cap",
            str(self.budget_cap),
            "--pricing-json",
            canonical(list(self.pricing.models)),
            "--pricing-sha256",
            self.pricing.sha256,
        ]
        if self.fake_response_cost is not None:
            argv.extend(["--fake-response-cost", str(self.fake_response_cost)])
        _docker(argv)
        self.created_container = True
        self.process = subprocess.Popen(  # nosec B603 B607
            ["docker", "start", "--attach", "--interactive", self.names.broker],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                key: value for key, value in os.environ.items() if key != PARENT_KEY_ENV
            },
        )
        if self.process.stdin is None:
            raise RuntimeError("broker sidecar stdin pipe unavailable")
        pipe_stat = os.fstat(self.process.stdin.fileno())
        self.control_pipe_identity = (pipe_stat.st_dev, pipe_stat.st_ino)
        self._wait_for_container()
        payload = (
            canonical(
                {
                    "parent_key": self.parent_key,
                    "probe_token": self.probe_token,
                }
            )
            + "\n"
        ).encode()
        self.process.stdin.write(payload)
        self.process.stdin.flush()
        self.inspect_secret_boundary()
        self.read_ledger()

    def _write_control(self, document: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.stdin.closed:
            raise RuntimeError("broker control pipe is unavailable")
        current = os.fstat(process.stdin.fileno())
        if (current.st_dev, current.st_ino) != self.control_pipe_identity:
            raise RuntimeError("broker control pipe identity changed")
        process.stdin.write((canonical(document) + "\n").encode())
        process.stdin.flush()

    def activate(self) -> None:
        if self.activated:
            raise RuntimeError("broker sidecar already activated")
        self._write_control({"op": "activate", "token": self.attempt_token})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ledger = self.read_ledger()
            if ledger.get("active") is True and ledger.get("activated") is True:
                self.activated = True
                break
            time.sleep(0.05)
        if not self.activated:
            raise RuntimeError("broker activation did not complete")

        def heartbeat() -> None:
            sequence = 0
            try:
                while not self.heartbeat_stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                    sequence += 1
                    self._write_control({"op": "heartbeat", "sequence": sequence})
            except (BrokenPipeError, OSError, RuntimeError):
                self.heartbeat_stop.set()

        self.heartbeat_thread = threading.Thread(
            target=heartbeat, name="calibration-broker-heartbeat"
        )
        self.heartbeat_thread.start()

    def _wait_for_container(self) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = _docker(["container", "inspect", self.names.broker], check=False)
            if result.returncode == 0:
                value = strict_json(result.stdout)
                if value[0].get("State", {}).get("Running") is True:
                    return
                if value[0].get("State", {}).get("Status") == "exited":
                    break
            time.sleep(0.1)
        raise RuntimeError("broker sidecar failed to start")

    def inspect_secret_boundary(self) -> dict[str, Any]:
        inspected = _inspect_one("container", self.names.broker)
        network = _inspect_one("network", self.names.network)
        serialized = canonical(inspected).encode()
        logs = _docker(["logs", self.names.broker], check=False).stdout
        parent = self.parent_key.encode()
        if parent in serialized or parent in logs:
            raise RuntimeError("parent key appeared in Docker evidence")
        mounts = inspected.get("Mounts")
        if not isinstance(mounts, list) or len(mounts) != 2:
            raise RuntimeError("broker mount boundary changed")
        by_destination = {item.get("Destination"): item for item in mounts}
        if set(by_destination) != {"/source", "/evidence"}:
            raise RuntimeError("broker mount boundary changed")
        if by_destination["/source"].get("RW") is not False:
            raise RuntimeError("broker source mount is writable")
        if by_destination["/evidence"].get("RW") is not True:
            raise RuntimeError("broker evidence mount is not writable")
        if b"docker.sock" in serialized:
            raise RuntimeError("broker received Docker socket")
        attached = inspected.get("NetworkSettings", {}).get("Networks")
        if not isinstance(attached, dict) or set(attached) != {self.names.network}:
            raise RuntimeError("broker network attachment changed")
        if (
            network.get("Driver") != "bridge"
            or network.get("Internal") is not False
            or network.get("Labels") != self.names.role_labels("network")
        ):
            raise RuntimeError("calibration network contract changed")
        return {
            "config_parent_key_absent": True,
            "logs_parent_key_absent": True,
            "mounts": {"evidence_rw": True, "source_ro": True},
            "network": {
                "driver": "bridge",
                "gateway": (
                    (network.get("IPAM", {}).get("Config") or [{}])[0].get("Gateway")
                ),
                "internal": network.get("Internal"),
                "labels": self.names.role_labels("network"),
                "broker_attached_only_to_attempt_network": True,
            },
        }

    def probe(self) -> None:
        script = (
            "import sys,time,urllib.request;"
            "token=sys.stdin.read().strip();"
            "url='http://'+sys.argv[1]+':62017/__tetrabench_probe';"
            "deadline=time.monotonic()+15;"
            "status=None;"
            "\nwhile time.monotonic()<deadline:\n"
            " try:\n"
            "  request=urllib.request.Request(url,headers="
            "{'Authorization':'Bearer '+token});"
            "  status=urllib.request.urlopen(request,timeout=2).status;break\n"
            " except Exception:\n  time.sleep(.1)\n"
            "\nprint(status);sys.exit(0 if status==204 else 1)"
        )
        result = _docker(
            [
                "run",
                "--rm",
                "-i",
                "--name",
                self.names.probe,
                *[
                    value
                    for key, value in self.names.role_labels("probe").items()
                    for value in ("--label", f"{key}={value}")
                ],
                "--network",
                self.names.network,
                "--read-only",
                "--cap-drop",
                "ALL",
                BROKER_IMAGE,
                "python",
                "-c",
                script,
                self.names.alias,
            ],
            check=False,
            input_bytes=(self.probe_token + "\n").encode(),
        )
        if result.returncode != 0 or result.stdout.strip() != b"204":
            raise RuntimeError("Docker sidecar broker probe failed")
        if (
            _docker(["container", "inspect", self.names.probe], check=False).returncode
            == 0
        ):
            raise RuntimeError("Docker sidecar probe container survived")
        ledger = self.read_ledger()
        if (
            ledger.get("probe_consumed") is not True
            or ledger.get("request_count") != 0
            or ledger.get("requests") != []
        ):
            raise RuntimeError("Docker sidecar probe reached model forwarding")

    def read_ledger(self, *, minimum_requests: int = 0) -> dict[str, Any]:
        path = self.evidence_root / "ledger.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                document = strict_json(path.read_bytes())
                if (
                    not isinstance(document, dict)
                    or document.get("schema_version") != 1
                ):
                    raise ValueError("broker evidence schema rejected")
                if document.get("pricing_sha256") != self.pricing.sha256:
                    raise ValueError("broker pricing evidence mismatch")
                request_count = document.get("request_count")
                if type(request_count) is int and request_count >= minimum_requests:
                    return document
            time.sleep(0.05)
        raise RuntimeError("broker evidence did not reach expected request count")

    def cleanup(self) -> dict[str, Any]:
        errors: list[str] = []
        self.heartbeat_stop.set()
        if self.heartbeat_thread is not None:
            self.heartbeat_thread.join(timeout=2)
            if self.heartbeat_thread.is_alive():
                errors.append("heartbeat thread survived")
        if self.process is not None and self.process.stdin is not None:
            with suppress(OSError):
                self.process.stdin.close()
        if _owned_resource_exists("container", self.names.broker, self.names.labels):
            stopped = _docker(["stop", "--time", "5", self.names.broker], check=False)
            if stopped.returncode != 0:
                _docker(["kill", self.names.broker], check=False)
            _docker(["rm", "--force", self.names.broker], check=False)
        for name in (self.names.probe,):
            if _owned_resource_exists("container", name, self.names.labels):
                _docker(["rm", "--force", name], check=False)
        if self.process is not None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.poll() is None:
                errors.append("Docker attach client survived")
        if _owned_resource_exists("network", self.names.network, self.names.labels):
            _remove_owned_attempt_containers(self.names)
            _docker(["network", "rm", self.names.network], check=False)
            if not _wait_resource_absent("network", self.names.network):
                errors.append("network removal failed")
        container_absent = _wait_resource_absent("container", self.names.broker)
        network_absent = _wait_resource_absent("network", self.names.network)
        if not container_absent or not network_absent:
            errors.append("Docker cleanup absence proof failed")
        if errors:
            raise RuntimeError("; ".join(errors))
        return {
            "broker_absent": True,
            "network_absent": True,
            "attach_client_reaped": True,
            "stdin_closed": True,
            "tokens_expired": True,
        }


class DockerMainAuthority:
    """Admit one immutable Harbor main config, then activate the broker once."""

    _ALLOWED_MOUNT_TARGETS = frozenset(
        {"/logs/agent", "/logs/verifier", "/logs/artifacts"}
    )

    def __init__(
        self,
        names: DockerAttemptNames,
        parent_key: str,
        output_root: Path,
        sidecar: DockerBrokerSidecar,
        candidate_manifest_sha256: str,
    ) -> None:
        self.names = names
        self.parent_key = parent_key
        self.output_root = output_root.resolve()
        self.sidecar = sidecar
        self.candidate_manifest_sha256 = candidate_manifest_sha256

    def wait_inspect_activate(self, command: Future[CommandResult]) -> dict[str, Any]:
        deadline = time.monotonic() + 300
        main: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if command.done():
                command.result()
                raise RuntimeError("Harbor command ended before main activation")
            listed = _docker(
                [
                    "ps",
                    "-q",
                    "--filter",
                    f"label={CALIBRATION_OWNER_LABEL}={CALIBRATION_OWNER}",
                    "--filter",
                    f"label=org.tetrabench.calibration.run={self.names.run_id}",
                    "--filter",
                    f"label=org.tetrabench.calibration.attempt={self.names.ordinal}",
                    "--filter",
                    "label=org.tetrabench.calibration.role=main",
                ],
                check=False,
            )
            candidates = [
                _inspect_one("container", container_id)
                for container_id in listed.stdout.decode().split()
            ]
            if candidates:
                if len(candidates) != 1:
                    raise RuntimeError("Harbor main identity is ambiguous")
                main = candidates[0]
                break
            time.sleep(0.1)
        if main is None:
            raise RuntimeError("Harbor main was not discoverable before activation")
        evidence = self._validate_main(main)
        self.sidecar.activate()
        ledger = self.sidecar.read_ledger()
        if ledger.get("active") is not True or ledger.get("activated") is not True:
            raise RuntimeError("broker activation evidence is incomplete")
        evidence["activation"] = {
            "completed_before_harbor_healthcheck": True,
            "control_channel": "anonymous-stdin-pipe",
            "main_has_control_channel": False,
        }
        return evidence

    def _validate_main(self, inspected: dict[str, Any]) -> dict[str, Any]:
        labels = _resource_labels("container", inspected)
        expected = self.names.role_labels("main")
        if any(labels.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Harbor main labels rejected")
        if labels.get("com.docker.compose.service") != "main":
            raise RuntimeError("Harbor main Compose identity rejected")
        if labels.get("org.tetrabench.calibration.candidate-manifest") != (
            self.candidate_manifest_sha256
        ):
            raise RuntimeError("Harbor main candidate identity rejected")
        config = inspected.get("Config")
        host = inspected.get("HostConfig")
        mounts = inspected.get("Mounts")
        network_settings = inspected.get("NetworkSettings")
        if (
            not isinstance(config, dict)
            or not isinstance(host, dict)
            or not isinstance(mounts, list)
            or not isinstance(network_settings, dict)
        ):
            raise RuntimeError("Harbor main immutable config schema rejected")
        if (
            config.get("ExposedPorts") not in (None, {})
            or host.get("PortBindings") not in (None, {})
            or host.get("PublishAllPorts") is not False
            or network_settings.get("Ports") not in (None, {})
        ):
            raise RuntimeError("Harbor main published port rejected")
        immutable_boundary = {
            "Config": config,
            "HostConfig": host,
            "Image": inspected.get("Image"),
            "Mounts": mounts,
            "NetworkSettingsPorts": network_settings.get("Ports"),
        }
        serialized = canonical(immutable_boundary).encode()
        if self.parent_key.encode() in serialized:
            raise RuntimeError("parent key appeared in Harbor main config")
        if host.get("Privileged") is not False:
            raise RuntimeError("privileged Harbor main rejected")
        if host.get("PidMode") not in (None, "") or host.get("IpcMode") not in (
            None,
            "",
            "private",
        ):
            raise RuntimeError("Harbor main host namespace mode rejected")
        if host.get("NetworkMode") not in {"default", self.names.network}:
            raise RuntimeError("Harbor main host network mode rejected")
        if host.get("CapAdd") not in (None, []):
            raise RuntimeError("Harbor main added capabilities rejected")
        if host.get("Devices") not in (None, []) or host.get("DeviceRequests") not in (
            None,
            [],
        ):
            raise RuntimeError("Harbor main device access rejected")
        security_opts = host.get("SecurityOpt") or []
        if not isinstance(security_opts, list) or any(
            option not in {"no-new-privileges:true", "no-new-privileges"}
            for option in security_opts
        ):
            raise RuntimeError("Harbor main security option rejected")
        approved_mounts: dict[str, str] = {}
        for mount in mounts:
            if not isinstance(mount, dict) or mount.get("Type") != "bind":
                raise RuntimeError("Harbor main unapproved mount rejected")
            source = mount.get("Source")
            destination = mount.get("Destination")
            if (
                not isinstance(source, str)
                or destination not in self._ALLOWED_MOUNT_TARGETS
            ):
                raise RuntimeError("Harbor main unapproved mount rejected")
            resolved = Path(source).resolve()
            if (
                resolved != self.output_root
                and self.output_root not in resolved.parents
            ):
                raise RuntimeError("Harbor main host bind escaped attempt output")
            approved_mounts[destination] = source
            if any(
                socket_name in source or socket_name in destination
                for socket_name in ("docker.sock", "containerd.sock")
            ):
                raise RuntimeError("Harbor main runtime socket rejected")
        if set(approved_mounts) != self._ALLOWED_MOUNT_TARGETS:
            raise RuntimeError("Harbor main expected mounts missing")
        attached = network_settings.get("Networks")
        if not isinstance(attached, dict) or set(attached) != {self.names.network}:
            raise RuntimeError("Harbor main network attachment rejected")
        image_id = inspected.get("Image")
        image_name = config.get("Image")
        if (
            not isinstance(image_id, str)
            or not image_id.startswith("sha256:")
            or not isinstance(image_name, str)
        ):
            raise RuntimeError("Harbor main image identity rejected")
        image = _inspect_one("image", image_id)
        if image.get("Id") != image_id:
            raise RuntimeError("Harbor main image digest mismatch")
        return {
            "boundary": "immutable-docker-config-before-attempt-token-activation",
            "config_sha256": hashlib.sha256(serialized).hexdigest(),
            "image": {"config_reference": image_name, "id": image_id},
            "main_container_id": inspected.get("Id"),
            "mount_targets": sorted(approved_mounts),
            "parent_key_absent": True,
            "published_ports_absent": True,
            "runtime_sockets_absent": True,
        }


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
    snapshot: SourceSnapshot,
    private_root: Path,
    run_id: str,
    parent_key: str,
    pricing: PricingSnapshot,
    model_pricing: dict[str, Any],
    remaining_budget: Decimal,
    attempt_record: dict[str, Any],
) -> dict[str, Any]:
    model = model_group
    harbor_model = f"openai/{model_group}"
    attempt_root = private_root / f"attempt-{ordinal}"
    attempt_root.mkdir(mode=0o700)
    names = _docker_names(run_id, ordinal)
    overlay = create_task_overlay(snapshot.task, attempt_root / "task-overlay", names)
    project, config_root = _write_project(attempt_root / "execution", overlay.task)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        expected_task_checksum = Task(project / "tasks/authority-fencing").checksum
    expected_task_digest = (
        "sha256:"
        + Packager.compute_content_hash(project / "tasks/authority-fencing")[0]
    )
    home = attempt_root / "home"
    home.mkdir(mode=0o700)
    for name in ("tmp", "cache", "data", "state"):
        (home / name).mkdir(mode=0o700)
    output = attempt_root / "output"
    broker_evidence_root = attempt_root / "broker-evidence"
    broker_evidence_root.mkdir(mode=0o700)
    started = time.monotonic()
    attempt_token = secrets.token_urlsafe(48)
    probe_token = secrets.token_urlsafe(48)
    sidecar = DockerBrokerSidecar(
        names=names,
        snapshot_root=snapshot.root,
        evidence_root=broker_evidence_root,
        parent_key=parent_key,
        attempt_token=attempt_token,
        probe_token=probe_token,
        model=model,
        pricing=pricing,
        max_input_tokens=model_pricing["max_input_tokens"],
        budget_cap=remaining_budget,
    )
    authority = DockerMainAuthority(
        names,
        parent_key,
        output,
        sidecar,
        overlay.candidate_manifest_sha256,
    )
    cleanup_evidence: dict[str, Any] | None = None
    network_evidence: dict[str, Any] | None = None
    ledger_document: dict[str, Any] | None = None
    security_evidence: dict[str, Any] | None = None
    try:
        try:
            sidecar.start()
            sidecar.probe()
            environment = child_environment(
                config_root=config_root,
                private_home=home,
                token=attempt_token,
                base_url=f"http://{names.alias}:{DEFAULT_BROKER_PORT}/v1",
            )
            executor = ThreadPoolExecutor(max_workers=1)
            command_future: Future[CommandResult] | None = None
            try:
                command_future = executor.submit(
                    _bounded_command,
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
                network_evidence = authority.wait_inspect_activate(command_future)
                result = command_future.result(timeout=MAX_ATTEMPT_SECONDS)
            except BaseException:
                _remove_owned_attempt_containers(names)
                cleanup_evidence = sidecar.cleanup()
                if command_future is not None:
                    with suppress(BaseException):
                        command_future.result(timeout=30)
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
            security_evidence = sidecar.inspect_secret_boundary()
            ledger_document = sidecar.read_ledger(minimum_requests=1)
        finally:
            cleanup_evidence = sidecar.cleanup()
    except BaseException as error:
        if ledger_document is None:
            with suppress(BaseException):
                ledger_document = sidecar.read_ledger()
        if ledger_document is not None:
            attempt_record["broker"] = {
                "locked_endpoint": ledger_document.get("locked_endpoint"),
                "request_count": ledger_document.get("request_count", 0),
                "requests": ledger_document.get("requests", []),
            }
            attempt_record["spend"] = {
                "known_actual_cost_usd": ledger_document.get(
                    "known_actual_cost_usd", "0"
                ),
                "retained_unknown_reservation_usd": ledger_document.get(
                    "retained_unknown_reservation_usd", "0"
                ),
                "total_exposure_usd": str(
                    Decimal(ledger_document.get("known_actual_cost_usd", "0"))
                    + Decimal(
                        ledger_document.get("retained_unknown_reservation_usd", "0")
                    )
                ),
            }
        _mark_attempt_failed(attempt_record, error)
        raise
    if ledger_document is None or cleanup_evidence is None:
        raise RuntimeError("broker lifecycle evidence is incomplete")
    requests = ledger_document["requests"]
    broker_evidence = {
        "locked_endpoint": ledger_document["locked_endpoint"],
        "pricing_sha256": ledger_document["pricing_sha256"],
        "probe_consumed": ledger_document["probe_consumed"],
        "request_count": ledger_document["request_count"],
        "requests": requests,
        "security": security_evidence,
        "shutdown": cleanup_evidence,
    }
    known = Decimal(ledger_document["known_actual_cost_usd"])
    retained = Decimal(ledger_document["retained_unknown_reservation_usd"])
    spend = {
        "known_actual_cost_usd": str(known),
        "retained_unknown_reservation_usd": str(retained),
        "total_exposure_usd": str(known + retained),
    }
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
    if not requests:
        raise ValueError("completed calibration attempt made no model request")
    if ledger_document["locked_endpoint"] is None or any(
        item["endpoint"] != ledger_document["locked_endpoint"] for item in requests
    ):
        raise ValueError("calibration attempt endpoint lock evidence is invalid")
    if ledger_document["fatal"] is not None:
        raise ValueError(ledger_document["fatal"])
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
        "task_identity": {
            "candidate_manifest": overlay.candidate_manifest,
            "candidate_manifest_sha256": overlay.candidate_manifest_sha256,
            "native_task_digest": expected_task_digest,
            "overlay_compose_sha256": overlay.compose_sha256,
            "overlay_manifest": overlay.overlay_manifest,
            "overlay_manifest_sha256": overlay.overlay_manifest_sha256,
            "transport_overlay_in_native_digest": True,
        },
        "topology": {
            "alias": names.alias,
            "broker_container": names.broker,
            "labels": names.labels,
            "network": names.network,
            "runtime_inspect": network_evidence,
        },
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
    return args


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv and effective_argv[0] == "__broker":
        return _broker_child_main(effective_argv[1:])
    args = parse_arguments(effective_argv)
    authority: ProofOutputAuthority | None = None
    attempts: list[dict[str, Any]] = []
    pricing: PricingSnapshot | None = None
    run_id: str | None = None
    try:
        if args.output is not None:
            authority = open_proof_output_authority(args.output)
        parent_key = os.environ.pop(PARENT_KEY_ENV, None)
        if not parent_key:
            raise RuntimeError("calibration gateway key environment is required")
        with tempfile.TemporaryDirectory(prefix="authority-calibration-") as directory:
            private_root = Path(directory)
            snapshot: SourceSnapshot = (
                create_debug_source_snapshot(private_root)
                if args.debug
                else create_clean_source_snapshot(private_root)
            )
            sweep_stale_calibration_resources()
            pricing = query_model_pricing(parent_key=parent_key)
            pricing_by_model = {item["model_group"]: item for item in pricing.models}
            manifest = source_manifest(
                tuple(snapshot.root / path for path in SOURCE_RELATIVE_PATHS),
                root=snapshot.root,
            )
            installed_cli = install_snapshot_cli(snapshot, private_root)
            execution_root = private_root / "execution"
            execution_root.mkdir(mode=0o700)
            run_id = _safe_docker_name(
                f"cal-{snapshot.revision[:12]}-{secrets.token_hex(6)}", "run id"
            )
            total_cost = Decimal(0)
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
                            snapshot=snapshot,
                            private_root=execution_root,
                            run_id=run_id,
                            parent_key=parent_key,
                            pricing=pricing,
                            model_pricing=pricing_by_model[model_group],
                            remaining_budget=MAX_TOTAL_COST - total_cost,
                            attempt_record=attempt_record,
                        )
                        total_cost += Decimal(
                            attempt_record["spend"]["known_actual_cost_usd"]
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
                and total_cost <= MAX_TOTAL_COST
                and recorded_cost == total_cost
                and actual_within_reservations
            )
            installed = {
                item["name"].lower(): item["version"]
                for item in installed_cli.attestation["distribution"][
                    "installed_distributions"
                ]
            }
            final_cleanup = sweep_calibration_run(run_id)
            transport: dict[str, Any] = {
                "broker_image": BROKER_IMAGE,
                "broker_port": DEFAULT_BROKER_PORT,
                "host_binding": False,
                "network_per_attempt": True,
                "parent_key_transport": "anonymous-stdin-pipe",
                "final_cleanup": {
                    **final_cleanup,
                    "active_authority_absent": True,
                    "owned_resources_absent": True,
                },
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
                    "total_authoritative_cost_usd": str(total_cost),
                    "total_cap_usd": str(MAX_TOTAL_COST),
                },
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
                "parent_gateway_key": {
                    "agent_exposed": False,
                    "docker_serialized": False,
                    "forwarding_authority": "broker-process-memory",
                    "transport": "anonymous-stdin-pipe-after-container-start",
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
                "transport": transport,
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
        if run_id is not None:
            try:
                sweep_calibration_run(run_id)
            except BaseException as cleanup_error:
                error = RuntimeError(
                    f"{safe_error(error)}; final Docker cleanup failed: "
                    f"{safe_error(cleanup_error)}"
                )
        _emit(
            _failure(
                args,
                error,
                attempts=attempts,
                pricing=pricing,
            )
        )
        return 1
    finally:
        if authority is not None:
            authority.close()


if __name__ == "__main__":
    raise SystemExit(main())
