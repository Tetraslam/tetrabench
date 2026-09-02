#!/usr/bin/env python3
"""Run the credential-brokered authority-fencing OpenCode calibration."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
import urllib.parse
import warnings
from collections.abc import Callable, Mapping, MutableMapping
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


def _strict_json_decimal(data: bytes) -> Any:
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
            parse_float=Decimal,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error


MAIN_DNS = ("1.1.1.1", "9.9.9.9")
OPENROUTER_KEY_ENV = "TETRABENCH_OPENROUTER_API_KEY"
LITELLM_KEY_ENV = "TETRABENCH_LITELLM_API_KEY"
LEGACY_LITELLM_KEY_ENV = "TETRABENCH_CALIBRATION_GATEWAY_KEY"
PARENT_KEY_ENV = LEGACY_LITELLM_KEY_ENV
TASK_ID = "systems-design/authority-fencing"
MAX_ATTEMPT_SECONDS = 35 * 60
MAX_TOTAL_COST = Decimal("25")
MAX_INPUT_OR_CACHE_COST_PER_TOKEN = Decimal("10") / Decimal(1_000_000)
MAX_OUTPUT_COST_PER_TOKEN = Decimal("50") / Decimal(1_000_000)
RESERVATION_SAFETY_MARGIN = Decimal("0.25")
MAX_OUTPUT_TOKENS = 16384
MAX_REQUESTS = 64
DENY_UPSTREAM_EXPECTED_REQUESTS = 6
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
MAX_BODY_BYTES = 384 << 10
MAX_HEADER_BYTES = 16 << 10
MAX_RESPONSE_BYTES = 64 << 20
MODEL_INFO_MAX_RESPONSE_BYTES = 4 << 20
GENERATION_MAX_RESPONSE_BYTES = 1 << 20
SETTLEMENT_WINDOW_SECONDS = 30.0
SETTLEMENT_POLL_SECONDS = 0.1
MIN_PARENT_KEY_LENGTH = 16
MAX_PARENT_KEY_LENGTH = 512
MIN_BROKER_TOKEN_LENGTH = 32
MAX_BROKER_TOKEN_LENGTH = 512
URLSAFE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
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


@dataclasses.dataclass(frozen=True, slots=True)
class BackendContract:
    name: str
    upstream_base_url: str
    credential_env: str
    deprecated_credential_env: str | None
    credential_prefix: str
    broker_dns: tuple[str, ...]
    resolver_purpose: str
    endpoint_paths: tuple[tuple[str, str], ...]
    pricing_path: str
    pricing_parser: str
    settlement: str
    generation_path: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ProfileContract:
    name: str
    child_model: str
    broker_model: str
    upstream_model: str
    harbor_model: str


OPENROUTER_BACKEND = BackendContract(
    name="openrouter",
    upstream_base_url="https://openrouter.ai/api/v1",
    credential_env=OPENROUTER_KEY_ENV,
    deprecated_credential_env=None,
    credential_prefix="sk-or-v1-",
    broker_dns=MAIN_DNS,
    resolver_purpose="public-openrouter-upstream",
    endpoint_paths=(
        ("/v1/responses", "/responses"),
        ("/v1/chat/completions", "/chat/completions"),
    ),
    pricing_path="/models",
    pricing_parser="openrouter-models",
    settlement="terminal_usage+generation",
    generation_path="/generation",
)
LITELLM_BACKEND = BackendContract(
    name="litellm",
    upstream_base_url="https://litellm-proxy.taildb21e0.ts.net",
    credential_env=LITELLM_KEY_ENV,
    deprecated_credential_env=LEGACY_LITELLM_KEY_ENV,
    credential_prefix="sk-",
    broker_dns=("100.100.100.100",),
    resolver_purpose="tailnet-upstream",
    endpoint_paths=(
        ("/v1/responses", "/v1/responses"),
        ("/v1/chat/completions", "/v1/chat/completions"),
    ),
    pricing_path="/model/info",
    pricing_parser="litellm-model-info",
    settlement="litellm-response-cost",
    generation_path=None,
)
BACKENDS = {item.name: item for item in (OPENROUTER_BACKEND, LITELLM_BACKEND)}
UPSTREAM_URL = LITELLM_BACKEND.upstream_base_url
BROKER_DNS = LITELLM_BACKEND.broker_dns
PROFILE_CONTRACTS = (
    ProfileContract(
        name="target",
        child_model="openai/gpt-5.6-sol",
        broker_model="openai/gpt-5.6-sol",
        upstream_model="openai/gpt-5.6-sol",
        harbor_model="openai/openai/gpt-5.6-sol",
    ),
    ProfileContract(
        name="alternate",
        child_model="anthropic/claude-sonnet-5",
        broker_model="anthropic/claude-sonnet-5",
        upstream_model="anthropic/claude-sonnet-5",
        harbor_model="openai/anthropic/claude-sonnet-5",
    ),
)
PROFILES = tuple((profile.name, profile.broker_model) for profile in PROFILE_CONTRACTS)
ATTEMPT_PHASES = (
    "sidecar_start",
    "topology_probe",
    "cli_spawn",
    "main_discovery",
    "main_config_validation",
    "broker_activation",
    "heartbeat_start",
    "cli_wait",
    "ledger_read",
    "native_validation",
    "cleanup",
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


def _validate_bearer(value: Any) -> str:
    if (
        type(value) is not str
        or not MIN_PARENT_KEY_LENGTH <= len(value) <= MAX_PARENT_KEY_LENGTH
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("broker credential payload rejected")
    return value


def _validate_parent_key(value: Any, backend: BackendContract = LITELLM_BACKEND) -> str:
    key = _validate_bearer(value)
    if not key.startswith(backend.credential_prefix):
        raise ValueError("broker credential payload rejected")
    return key


def _validate_broker_token(value: Any, *, error: str) -> str:
    if (
        type(value) is not str
        or not MIN_BROKER_TOKEN_LENGTH <= len(value) <= MAX_BROKER_TOKEN_LENGTH
        or URLSAFE_TOKEN_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(error)
    return value


def _validate_broker_credential_payload(
    value: Any, backend: BackendContract = LITELLM_BACKEND
) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"parent_key", "probe_token"}:
        raise ValueError("broker credential payload schema rejected")
    return (
        _validate_parent_key(value["parent_key"], backend),
        _validate_broker_token(
            value["probe_token"], error="broker credential payload rejected"
        ),
    )


def _take_backend_credential(
    backend: BackendContract, environment: MutableMapping[str, str]
) -> str:
    primary = environment.pop(backend.credential_env, None)
    fallback = (
        environment.pop(backend.deprecated_credential_env, None)
        if backend.deprecated_credential_env is not None
        else None
    )
    if primary and fallback:
        raise RuntimeError("selected backend credentials are ambiguous")
    value = primary or fallback
    if not value:
        raise RuntimeError("selected calibration backend credential is required")
    return _validate_parent_key(value, backend)


def _profile_contract(name: str) -> ProfileContract:
    matches = [profile for profile in PROFILE_CONTRACTS if profile.name == name]
    if len(matches) != 1:
        raise ValueError("calibration profile identity rejected")
    return matches[0]


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
    settlement_failure: str | None
    retained_unknown_reservation_usd: str
    upstream_opened: bool
    parent_authorization_sent: bool


@dataclasses.dataclass(frozen=True, slots=True)
class PricingSnapshot:
    models: tuple[dict[str, Any], ...]
    sha256: str
    backend: str = "litellm"
    source: str = "/model/info"


@dataclasses.dataclass(frozen=True, slots=True)
class SettlementResult:
    response_id: str
    model: str
    cost: Decimal | None
    usage: dict[str, int]


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
class DockerNetworkAllocation:
    subnet: str
    candidate_probe: int
    route_prefixes_checked: int
    docker_subnets_checked: int
    route_collision_rejections: int
    docker_collision_rejections: int
    create_overlap_retries: int


@dataclasses.dataclass(frozen=True, slots=True)
class Reservation:
    identifier: int
    amount: Decimal


@dataclasses.dataclass(slots=True)
class ForwardingAdmission:
    started: bool = False


class AttemptFailure(RuntimeError):
    def __init__(self, stage: str, failure_type: str, cause: BaseException) -> None:
        super().__init__(f"{stage}:{failure_type}")
        self.stage = stage
        self.failure_type = failure_type
        self.cause_class = type(cause).__name__

    @property
    def evidence(self) -> dict[str, str]:
        return {
            "exception_class": self.cause_class,
            "stage": self.stage,
            "type": self.failure_type,
        }


class CalibrationStageError(RuntimeError):
    """A content-free attempt-stage failure safe for retained evidence."""

    def __init__(self, phase: str, cause_class: str) -> None:
        if phase not in ATTEMPT_PHASES:
            raise ValueError("unknown calibration phase")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cause_class):
            raise ValueError("unsafe calibration cause class")
        super().__init__(phase, cause_class)
        self.phase = phase
        self.cause_class = cause_class

    @property
    def evidence(self) -> dict[str, str]:
        return {"cause_class": self.cause_class, "failed_stage": self.phase}


class EarlyCommandSuccess(RuntimeError):
    pass


class EarlyCommandNonzeroReturn(RuntimeError):
    pass


class OpenRouterSettlementError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _new_phase_evidence() -> dict[str, Any]:
    return {
        "failed_stage": None,
        "timeline": [
            {
                "completed": False,
                "failed": False,
                "phase": phase,
                "started": False,
            }
            for phase in ATTEMPT_PHASES
        ],
    }


def _phase_status(attempt: dict[str, Any], phase: str) -> dict[str, Any]:
    phases = attempt["phases"]
    for status in phases["timeline"]:
        if status["phase"] == phase:
            return status
    raise ValueError("attempt phase evidence is incomplete")


def _run_phase(attempt: dict[str, Any], phase: str, action: Callable[[], Any]) -> Any:
    status = _phase_status(attempt, phase)
    if status["started"]:
        raise CalibrationStageError(phase, "RepeatedPhase")
    status["started"] = True
    try:
        value = action()
    except CalibrationStageError as error:
        status["failed"] = True
        if attempt["phases"]["failed_stage"] is None:
            attempt["phases"]["failed_stage"] = phase
        raise CalibrationStageError(phase, error.cause_class) from None
    except AttemptFailure as error:
        status["failed"] = True
        if attempt["phases"]["failed_stage"] is None:
            attempt["phases"]["failed_stage"] = phase
        raise CalibrationStageError(phase, error.cause_class) from None
    except BaseException as error:
        status["failed"] = True
        if attempt["phases"]["failed_stage"] is None:
            attempt["phases"]["failed_stage"] = phase
        raise CalibrationStageError(phase, type(error).__name__) from None
    status["completed"] = True
    return value


class SpendLedger:
    """Atomically own actual spend plus exact in-flight worst-case reservations."""

    def __init__(
        self,
        max_total: Decimal = MAX_TOTAL_COST,
        prior_unknown_exposure: Decimal = Decimal(0),
    ) -> None:
        if not max_total.is_finite() or max_total <= 0 or max_total > MAX_TOTAL_COST:
            raise ValueError("calibration ledger cap is invalid")
        if (
            not prior_unknown_exposure.is_finite()
            or prior_unknown_exposure < 0
            or prior_unknown_exposure > max_total
        ):
            raise ValueError("prior unknown exposure is invalid")
        self._lock = threading.Lock()
        self._max_total = max_total
        self._prior_unknown_exposure = prior_unknown_exposure
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
            projected = (
                self._prior_unknown_exposure + self._cost + self._reserved + amount
            )
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
            if (
                self._prior_unknown_exposure + self._cost + self._reserved + cost
                > self._max_total
            ):
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

    @property
    def prior_unknown_exposure(self) -> Decimal:
        return self._prior_unknown_exposure


class BrokerState:
    def __init__(
        self,
        *,
        parent_key: str,
        token: str | None,
        probe_token: str | None,
        model: str,
        canonical_model: str | None = None,
        max_input_tokens: int,
        deadline: float,
        ledger: SpendLedger,
        backend: BackendContract = LITELLM_BACKEND,
        upstream_url: str | None = None,
        evidence_path: Path | None = None,
        fake_response_cost: Decimal | None = None,
        pricing_sha256: str | None = None,
        shutdown_event: threading.Event | None = None,
        debug_deny_upstream: bool = False,
    ) -> None:
        self.parent_key = parent_key
        self._token: str | None = token
        self._probe_token: str | None = probe_token
        self._probe_consumed = False
        self.model = model
        self.response_models = frozenset({model, canonical_model or model})
        self.max_input_tokens = max_input_tokens
        self.deadline = deadline
        self.ledger = ledger
        self.backend = backend
        self.upstream_url = (upstream_url or backend.upstream_base_url).rstrip("/")
        _parse_upstream_url(self.upstream_url)
        self.evidence_path = evidence_path
        self.evidence_lock = threading.Lock()
        self.fake_response_cost = fake_response_cost
        self.pricing_sha256 = pricing_sha256
        self.shutdown_event = shutdown_event
        self.debug_deny_upstream = debug_deny_upstream
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)
        self.request_count = 0
        self.locked_endpoint: str | None = None
        self.active = token is not None
        self.activated = token is not None
        self.activation_token_sha256 = (
            hashlib.sha256(token.encode()).hexdigest() if token is not None else None
        )
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
                    "activation_token_sha256": self.activation_token_sha256,
                    "activated": self.activated,
                    "backend": self.backend.name,
                    "fatal": self.ledger.fatal,
                    "known_actual_cost_usd": str(self.ledger.cost),
                    "locked_endpoint": self.locked_endpoint,
                    "pricing_sha256": self.pricing_sha256,
                    "settlement_source": self.backend.settlement,
                    "probe_consumed": self._probe_consumed,
                    "request_count": self.request_count,
                    "requests": [dataclasses.asdict(item) for item in self.records],
                    "retained_unknown_reservation_usd": str(self.ledger.reserved),
                    "schema_version": 2,
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
            upstream.auto_open = 0
            connected = upstream.sock
            upstream.sock = None
            if connected is not None:
                with suppress(OSError):
                    connected.shutdown(socket.SHUT_RDWR)
                connected.close()

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
        token = _validate_broker_token(token, error="broker activation token rejected")
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
                self.activation_token_sha256 = hashlib.sha256(
                    token.encode()
                ).hexdigest()
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

    def upstream_io_timeout(self) -> float:
        expired = False
        with self.lock:
            now = time.monotonic()
            if self._expired_locked(now):
                self._invalidate_locked()
                expired = True
                remaining = 0.0
            else:
                remaining = min(self.deadline - now, float(MAX_ATTEMPT_SECONDS))
        if expired:
            self.write_evidence()
            raise TimeoutError("broker attempt deadline expired")
        if remaining <= 0:
            raise TimeoutError("broker attempt deadline expired")
        return remaining

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
            now = time.monotonic()
            if self._expired_locked(now):
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
                if upstream.sock is None:
                    raise RuntimeError("upstream is not connected")
                upstream.sock.settimeout(
                    min(self.deadline - now, float(MAX_ATTEMPT_SECONDS))
                )
                admission.started = True
                upstream.request(
                    "POST",
                    _join_upstream_path(
                        urllib.parse.urlsplit(self.upstream_url).path,
                        _backend_endpoint_path(self.backend, endpoint),
                    ),
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

    def send_connected_settlement_request(
        self,
        upstream: http.client.HTTPConnection,
        path: str,
        *,
        io_timeout: float,
    ) -> None:
        expired = False
        with self.lock:
            if upstream.sock is None:
                raise RuntimeError("upstream is not connected")
            upstream.auto_open = 0
            self.upstreams.add(upstream)
            now = time.monotonic()
            if self._expired_locked(now):
                self._invalidate_locked()
                expired = True
                accepted = False
            else:
                accepted = self.active and bool(self.parent_key)
            if accepted:
                parent_key = self.parent_key
                if upstream.sock is None:
                    raise RuntimeError("upstream is not connected")
                upstream.sock.settimeout(
                    min(
                        self.deadline - now,
                        float(MAX_ATTEMPT_SECONDS),
                        io_timeout,
                    )
                )
                upstream.request(
                    "GET",
                    path,
                    headers={
                        "Authorization": f"Bearer {parent_key}",
                        "Accept": "application/json",
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


def _usage_token_count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("model response token usage is malformed")
    if value > 1_000_000_000:
        raise ValueError("model response token usage exceeds limit")
    return value


def _stream_cost(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ValueError("model streaming cost is malformed")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("model streaming cost is malformed") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("model streaming cost is not finite nonnegative")
    return parsed


def _validate_stream_content_type(headers: http.client.HTTPMessage) -> str:
    values = headers.get_all("Content-Type", [])
    if len(values) != 1:
        raise ValueError("model streaming content type is ambiguous")
    original = values[0]
    parts = [part.strip() for part in original.split(";")]
    if not parts or parts[0].lower() != "text/event-stream":
        raise ValueError("model streaming content type is invalid")
    parameters: dict[str, str] = {}
    for part in parts[1:]:
        if part.count("=") != 1:
            raise ValueError("model streaming content type is invalid")
        name, value = (item.strip().lower() for item in part.split("=", 1))
        if name in parameters or name != "charset" or value not in {"utf-8", '"utf-8"'}:
            raise ValueError("model streaming content type is invalid")
        parameters[name] = value
    return original


def _response_framing(
    headers: http.client.HTTPMessage, *, require_explicit: bool
) -> int | None:
    content_lengths = headers.get_all("Content-Length", [])
    transfer_encodings = headers.get_all("Transfer-Encoding", [])
    if (
        len(content_lengths) > 1
        or (content_lengths and not content_lengths[0].isdigit())
        or (content_lengths and transfer_encodings)
        or len(transfer_encodings) > 1
        or (transfer_encodings and transfer_encodings[0].strip().lower() != "chunked")
        or (require_explicit and not content_lengths and not transfer_encodings)
    ):
        raise ValueError("model response framing is ambiguous")
    declared_length = int(content_lengths[0]) if content_lengths else None
    if declared_length is not None and declared_length > MAX_RESPONSE_BYTES:
        raise ValueError("model response exceeds limit")
    return declared_length


def _read_bounded_response(
    response: http.client.HTTPResponse,
    upstream: http.client.HTTPConnection,
    state: BrokerState,
    declared_length: int | None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    io_deadline: float | None = None,
) -> bytes:
    retained = bytearray()
    while True:
        if len(retained) > max_bytes:
            raise ValueError("model response exceeds limit")
        timeout = _upstream_io_timeout(state, io_deadline=io_deadline)
        if upstream.sock is None:
            raise ConnectionError("model upstream socket closed")
        upstream.sock.settimeout(timeout)
        chunk = response.read1(min(64 << 10, max_bytes + 1 - len(retained)))
        if not chunk:
            break
        retained.extend(chunk)
    if len(retained) > max_bytes:
        raise ValueError("model response exceeds limit")
    if declared_length is not None and len(retained) != declared_length:
        raise ValueError("model response framing mismatch")
    return bytes(retained)


def _upstream_io_timeout(
    state: BrokerState, *, io_deadline: float | None = None
) -> float:
    timeout = state.upstream_io_timeout()
    if io_deadline is None:
        return timeout
    remaining = io_deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("OpenRouter generation settlement timed out")
    return min(timeout, remaining)


def _parse_responses_stream(body: bytes) -> tuple[Decimal, dict[str, int]]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("model streaming response is not UTF-8") from error
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or not normalized.endswith("\n\n"):
        raise ValueError("model streaming framing is malformed")
    blocks = normalized[:-2].split("\n\n")
    if not blocks or any(not block for block in blocks):
        raise ValueError("model streaming framing is malformed")
    terminal: tuple[Decimal, dict[str, int]] | None = None
    for index, block in enumerate(blocks):
        event_values: list[str] = []
        data_values: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_values.append(line[7:])
            elif line.startswith("data: "):
                data_values.append(line[6:])
            else:
                raise ValueError("model streaming framing is malformed")
        if len(event_values) != 1 or len(data_values) != 1:
            raise ValueError("model streaming framing is ambiguous")
        event_type = event_values[0]
        if not event_type or len(event_type) > 128:
            raise ValueError("model streaming event type is malformed")
        document = _strict_json_decimal(data_values[0].encode())
        if not isinstance(document, dict) or document.get("type") != event_type:
            raise ValueError("model streaming event is malformed")
        if event_type in {"response.failed", "response.incomplete"}:
            raise ValueError("model streaming response did not complete")
        if event_type != "response.completed":
            continue
        if terminal is not None:
            raise ValueError("model streaming terminal event is duplicated")
        if index != len(blocks) - 1:
            raise ValueError("model streaming response has trailing events")
        response_document = document.get("response")
        if (
            not isinstance(response_document, dict)
            or response_document.get("status") != "completed"
        ):
            raise ValueError("model streaming terminal event is unsuccessful")
        usage = response_document.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("model streaming terminal usage is missing")
        bounded_usage = {
            name: _usage_token_count(usage.get(name))
            for name in ("input_tokens", "output_tokens", "total_tokens")
        }
        if bounded_usage["total_tokens"] != (
            bounded_usage["input_tokens"] + bounded_usage["output_tokens"]
        ):
            raise ValueError("model streaming token usage is incoherent")
        if "cost" not in usage:
            raise ValueError("model streaming cost is missing")
        terminal = (_stream_cost(usage["cost"]), bounded_usage)
    if terminal is None:
        raise ValueError("model streaming terminal event is missing")
    return terminal


def _response_identifier(value: Any) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 256
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("model response identifier is malformed")
    return value


def _normalized_usage(
    value: Any, *, endpoint: str, cost_required: bool = True
) -> tuple[Decimal | None, dict[str, int]]:
    if not isinstance(value, dict):
        raise ValueError("model terminal usage is missing")
    if endpoint == "/v1/responses":
        input_field, output_field = "input_tokens", "output_tokens"
    elif endpoint == "/v1/chat/completions":
        input_field, output_field = "prompt_tokens", "completion_tokens"
    else:
        raise ValueError("model terminal usage endpoint is unsupported")
    usage = {
        "input_tokens": _usage_token_count(value.get(input_field)),
        "output_tokens": _usage_token_count(value.get(output_field)),
        "total_tokens": _usage_token_count(value.get("total_tokens")),
    }
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        raise ValueError("model terminal token usage is incoherent")
    if "cost" not in value and cost_required:
        raise ValueError("model terminal cost is missing")
    cost = _stream_cost(value["cost"]) if "cost" in value else None
    return cost, usage


def _parse_openrouter_nonstream(
    body: bytes, *, endpoint: str, expected_models: frozenset[str]
) -> SettlementResult:
    document = _strict_json_decimal(body)
    if not isinstance(document, dict):
        raise ValueError("OpenRouter response schema rejected")
    response_id = _response_identifier(document.get("id"))
    response_model = _response_identifier(document.get("model"))
    if response_model not in expected_models:
        raise ValueError("OpenRouter response model mismatch")
    if endpoint == "/v1/responses" and document.get("status") != "completed":
        raise ValueError("OpenRouter response did not complete")
    if endpoint == "/v1/chat/completions" and not isinstance(
        document.get("choices"), list
    ):
        raise ValueError("OpenRouter chat response is malformed")
    cost, usage = _normalized_usage(
        document.get("usage"), endpoint=endpoint, cost_required=False
    )
    return SettlementResult(
        response_id=response_id, model=response_model, cost=cost, usage=usage
    )


def _parse_openrouter_responses_stream(
    body: bytes, *, expected_models: frozenset[str]
) -> SettlementResult:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("OpenRouter stream is not UTF-8") from error
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or not normalized.endswith("\n\n"):
        raise ValueError("OpenRouter stream framing is malformed")
    blocks = normalized[:-2].split("\n\n")
    frames: list[tuple[str | None, str]] = []
    for block in blocks:
        if not block:
            continue
        event_values: list[str] = []
        data_values: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event: "):
                event_values.append(line[7:])
            elif line.startswith("data: "):
                data_values.append(line[6:])
            else:
                raise ValueError("OpenRouter stream framing is malformed")
        if not event_values and not data_values:
            continue
        if len(event_values) > 1 or len(data_values) != 1:
            raise ValueError("OpenRouter stream framing is ambiguous")
        frames.append((event_values[0] if event_values else None, data_values[0]))
    if not frames or frames[-1] != (None, "[DONE]"):
        raise ValueError("OpenRouter stream completion marker is missing")
    terminal: SettlementResult | None = None
    for index, (declared_event_type, data) in enumerate(frames[:-1]):
        if data == "[DONE]":
            raise ValueError("OpenRouter stream completion marker is ambiguous")
        document = _strict_json_decimal(data.encode())
        if not isinstance(document, dict) or type(document.get("type")) is not str:
            raise ValueError("OpenRouter stream event is malformed")
        event_type = document["type"]
        if declared_event_type is not None and declared_event_type != event_type:
            raise ValueError("OpenRouter stream event is malformed")
        if event_type in {"response.failed", "response.incomplete"}:
            raise ValueError("OpenRouter stream did not complete")
        if event_type not in {"response.completed", "response.done"}:
            continue
        if terminal is not None or index != len(frames) - 2:
            raise ValueError("OpenRouter stream terminal is ambiguous")
        response = document.get("response")
        if not isinstance(response, dict) or response.get("status") != "completed":
            raise ValueError("OpenRouter stream terminal is unsuccessful")
        response_model = _response_identifier(response.get("model"))
        if response_model not in expected_models:
            raise ValueError("OpenRouter response model mismatch")
        cost, usage = _normalized_usage(
            response.get("usage"), endpoint="/v1/responses", cost_required=False
        )
        terminal = SettlementResult(
            response_id=_response_identifier(response.get("id")),
            model=response_model,
            cost=cost,
            usage=usage,
        )
    if terminal is None:
        raise ValueError("OpenRouter stream terminal is missing")
    return terminal


def _validate_openrouter_generation(
    body: bytes,
    *,
    expected_id: str,
    expected_models: frozenset[str],
    expected_streamed: bool,
    expected_cost: Decimal | None,
    expected_usage: Mapping[str, int],
) -> Decimal:
    document = _strict_json_decimal(body)
    if not isinstance(document, dict) or not isinstance(document.get("data"), dict):
        raise OpenRouterSettlementError("generation_schema")
    generation = document["data"]
    if generation.get("id") != expected_id:
        raise OpenRouterSettlementError("generation_id_mismatch")
    try:
        generation_model = _response_identifier(generation.get("model"))
    except ValueError as error:
        raise OpenRouterSettlementError("generation_model_malformed") from error
    if generation_model not in expected_models:
        raise OpenRouterSettlementError("generation_model_mismatch")
    if generation.get("streamed") is not expected_streamed:
        raise OpenRouterSettlementError("generation_stream_mismatch")
    try:
        total_cost = _stream_cost(generation.get("total_cost"))
        usage_cost = _stream_cost(generation.get("usage"))
    except ValueError as error:
        raise OpenRouterSettlementError("generation_cost_malformed") from error
    if usage_cost != total_cost or (
        expected_cost is not None and total_cost != expected_cost
    ):
        raise OpenRouterSettlementError("generation_cost_mismatch")
    try:
        _usage_token_count(generation.get("tokens_prompt"))
        _usage_token_count(generation.get("tokens_completion"))
    except ValueError as error:
        raise OpenRouterSettlementError(
            "generation_normalized_tokens_malformed"
        ) from error
    try:
        native_prompt = _usage_token_count(generation.get("native_tokens_prompt"))
        native_completion = _usage_token_count(
            generation.get("native_tokens_completion")
        )
    except ValueError as error:
        raise OpenRouterSettlementError("generation_native_tokens_malformed") from error
    if {
        "input_tokens": native_prompt,
        "output_tokens": native_completion,
        "total_tokens": native_prompt + native_completion,
    } != dict(expected_usage):
        raise OpenRouterSettlementError("generation_native_tokens_mismatch")
    return total_cost


def _poll_openrouter_generation(
    state: BrokerState,
    *,
    settlement: SettlementResult,
    generation_id: str | None,
    streamed: bool,
) -> Decimal:
    generation_path = state.backend.generation_path
    if generation_path is None:
        raise ValueError("backend generation adapter is missing")
    parsed = _parse_upstream_url(state.upstream_url)
    deadline = min(state.deadline, time.monotonic() + SETTLEMENT_WINDOW_SECONDS)
    lookup_id = generation_id or settlement.response_id
    path = (
        _join_upstream_path(parsed.path, generation_path)
        + "?"
        + urllib.parse.urlencode({"id": lookup_id})
    )
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("OpenRouter generation settlement timed out")
        upstream = _upstream_connection(
            parsed, _upstream_io_timeout(state, io_deadline=deadline)
        )
        response: http.client.HTTPResponse | None = None
        try:
            upstream.connect()
            state.send_connected_settlement_request(
                upstream,
                path,
                io_timeout=_upstream_io_timeout(state, io_deadline=deadline),
            )
            if upstream.sock is None:
                raise ConnectionError("model upstream socket closed")
            upstream.sock.settimeout(_upstream_io_timeout(state, io_deadline=deadline))
            response = upstream.getresponse()
            declared = _response_framing(response.headers, require_explicit=False)
            if declared is not None and declared > GENERATION_MAX_RESPONSE_BYTES:
                raise ValueError("OpenRouter generation response exceeds limit")
            body = _read_bounded_response(
                response,
                upstream,
                state,
                declared,
                max_bytes=GENERATION_MAX_RESPONSE_BYTES,
                io_deadline=deadline,
            )
            if response.status == HTTPStatus.NOT_FOUND:
                if time.monotonic() + SETTLEMENT_POLL_SECONDS >= deadline:
                    raise TimeoutError("OpenRouter generation settlement timed out")
            elif response.status == HTTPStatus.OK:
                return _validate_openrouter_generation(
                    body,
                    expected_id=lookup_id,
                    expected_models=state.response_models,
                    expected_streamed=streamed,
                    expected_cost=settlement.cost,
                    expected_usage=settlement.usage,
                )
            else:
                raise ValueError("OpenRouter generation request failed")
        finally:
            if response is not None:
                response.close()
            state.unregister_upstream(upstream)
            upstream.close()
        time.sleep(SETTLEMENT_POLL_SECONDS)


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


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError(f"model pricing {field} is not numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"model pricing {field} is malformed") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"model pricing {field} must be finite nonnegative")
    return parsed


def _join_upstream_path(base_path: str, child_path: str) -> str:
    if (
        not child_path.startswith("/")
        or "?" in child_path
        or "#" in child_path
        or ".." in child_path.split("/")
    ):
        raise ValueError("upstream child path rejected")
    normalized_base = "/" + base_path.strip("/") if base_path.strip("/") else ""
    return normalized_base + child_path


def _parse_upstream_url(value: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.hostname is None
        or parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or ".." in parsed.path.split("/")
    ):
        raise ValueError("backend upstream URL rejected")
    return parsed


def _backend_endpoint_path(backend: BackendContract, child_endpoint: str) -> str:
    matches = [
        upstream
        for child, upstream in backend.endpoint_paths
        if child == child_endpoint
    ]
    if len(matches) != 1:
        raise ValueError("backend endpoint mapping rejected")
    return matches[0]


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
    return PricingSnapshot(
        models=models,
        sha256=hashlib.sha256(encoded).hexdigest(),
        backend=LITELLM_BACKEND.name,
        source=LITELLM_BACKEND.pricing_path,
    )


OPENROUTER_RATE_FIELDS = frozenset(
    {"prompt", "completion", "input_cache_read", "input_cache_write"}
)
OPENROUTER_UNSUPPORTED_PRICE_FIELDS = frozenset(
    {
        "audio",
        "image",
        "input_audio",
        "internal_reasoning",
        "output_audio",
        "request",
        "web_search",
    }
)
OPENROUTER_REACHABLE_UNRESERVED_PRICE_FIELDS = frozenset(
    {"internal_reasoning", "request"}
)
OPENROUTER_OVERRIDE_CONDITION_FIELDS = frozenset(
    {
        "context_length",
        "max_tokens",
        "min_prompt_tokens",
        "min_tokens",
        "threshold",
        "utc_days",
        "utc_end",
        "utc_start",
    }
)
OPENROUTER_UTC_DAYS = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
)


def _is_openrouter_rate_field(field: str) -> bool:
    return field in OPENROUTER_RATE_FIELDS or field.startswith("input_cache_write_")


def _validate_openrouter_override_condition(field: str, value: Any) -> None:
    if field in {
        "context_length",
        "max_tokens",
        "min_prompt_tokens",
        "min_tokens",
        "threshold",
    }:
        if type(value) is not int or value < 0:
            raise ValueError("OpenRouter pricing override condition is malformed")
        return
    if field in {"utc_start", "utc_end"}:
        if type(value) is int and 0 <= value <= 2359 and value % 100 < 60:
            return
        raise ValueError("OpenRouter pricing override condition is malformed")
    if field == "utc_days":
        if (
            not isinstance(value, list)
            or not value
            or any(
                type(day) is not str or day not in OPENROUTER_UTC_DAYS for day in value
            )
            or len(set(value)) != len(value)
        ):
            raise ValueError("OpenRouter pricing override condition is malformed")
        return
    raise ValueError("OpenRouter pricing override condition is unknown")


def _openrouter_pricing_values(pricing: Any) -> dict[str, Decimal]:
    if not isinstance(pricing, dict):
        raise ValueError("OpenRouter model pricing is missing")
    for field, value in pricing.items():
        if field == "overrides" or _is_openrouter_rate_field(field):
            continue
        if field not in OPENROUTER_UNSUPPORTED_PRICE_FIELDS:
            raise ValueError("OpenRouter unknown pricing field rejected")
        price = _nonnegative_decimal(value, field)
        if field in OPENROUTER_REACHABLE_UNRESERVED_PRICE_FIELDS and price != 0:
            raise ValueError("OpenRouter unsupported paid pricing is nonzero")
    required = ("prompt", "completion", "input_cache_read", "input_cache_write")
    values = {field: _positive_decimal(pricing.get(field), field) for field in required}
    write_tiers = [
        _positive_decimal(value, field)
        for field, value in pricing.items()
        if field.startswith("input_cache_write")
    ]
    values["input_cache_write"] = max(write_tiers)
    return values


def _openrouter_canonical_model(value: Any, *, requested_model: str) -> str:
    canonical_model = _response_identifier(value)
    if canonical_model != requested_model and not canonical_model.startswith(
        f"{requested_model}-"
    ):
        raise ValueError("OpenRouter canonical model identity mismatch")
    return canonical_model


def _validate_openrouter_pricing_document(document: Any) -> PricingSnapshot:
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise ValueError("OpenRouter models response schema rejected")
    expected = {profile.upstream_model for profile in PROFILE_CONTRACTS}
    selected: dict[str, dict[str, Any]] = {}
    for row in document["data"]:
        if not isinstance(row, dict) or row.get("id") not in expected:
            continue
        model = row["id"]
        if model in selected:
            raise ValueError("OpenRouter models returned an ambiguous model")
        base_pricing = row.get("pricing")
        rates = [_openrouter_pricing_values(base_pricing)]
        if not isinstance(base_pricing, dict):
            raise ValueError("OpenRouter model pricing is missing")
        overrides = base_pricing.get("overrides", [])
        if not isinstance(overrides, list):
            raise ValueError("OpenRouter pricing overrides are malformed")
        for override in overrides:
            if not isinstance(override, dict):
                raise ValueError("OpenRouter pricing override is malformed")
            has_utc_start = "utc_start" in override
            has_utc_end = "utc_end" in override
            if has_utc_start != has_utc_end or (
                has_utc_start and override["utc_start"] == override["utc_end"]
            ):
                raise ValueError("OpenRouter pricing override condition is malformed")
            override_pricing: dict[str, Any] = {}
            conditions = 0
            for key, value in override.items():
                if key in OPENROUTER_OVERRIDE_CONDITION_FIELDS:
                    _validate_openrouter_override_condition(key, value)
                    conditions += 1
                elif _is_openrouter_rate_field(key) or key in (
                    OPENROUTER_UNSUPPORTED_PRICE_FIELDS
                ):
                    override_pricing[key] = value
                else:
                    raise ValueError("OpenRouter pricing override condition is unknown")
            if conditions == 0 or not override_pricing:
                raise ValueError("OpenRouter pricing override is malformed")
            merged_pricing = {
                key: value for key, value in base_pricing.items() if key != "overrides"
            }
            merged_pricing.update(override_pricing)
            rates.append(_openrouter_pricing_values(merged_pricing))
        maxima = {
            field: max(rate[field] for rate in rates)
            for field in (
                "prompt",
                "completion",
                "input_cache_read",
                "input_cache_write",
            )
        }
        if (
            maxima["prompt"] > MAX_INPUT_OR_CACHE_COST_PER_TOKEN
            or maxima["input_cache_read"] > MAX_INPUT_OR_CACHE_COST_PER_TOKEN
            or maxima["input_cache_write"] > MAX_INPUT_OR_CACHE_COST_PER_TOKEN
            or maxima["completion"] > MAX_OUTPUT_COST_PER_TOKEN
        ):
            raise ValueError(
                "OpenRouter model pricing exceeds calibration hard ceiling"
            )
        context_length = _positive_limit(row.get("context_length"), "context_length")
        top_provider = row.get("top_provider")
        if not isinstance(top_provider, dict):
            raise ValueError("OpenRouter top provider is missing")
        max_output = _positive_limit(
            top_provider.get("max_completion_tokens"), "max_completion_tokens"
        )
        if max_output < MAX_OUTPUT_TOKENS:
            raise ValueError("OpenRouter model output limit is below calibration cap")
        if context_length <= MAX_OUTPUT_TOKENS:
            raise ValueError(
                "OpenRouter context limit cannot cover calibration output cap"
            )
        max_input = context_length - MAX_OUTPUT_TOKENS
        canonical_model = _openrouter_canonical_model(
            row.get("canonical_slug"), requested_model=model
        )
        selected[model] = {
            "backend": OPENROUTER_BACKEND.name,
            "canonical_model": canonical_model,
            "cache_creation_input_token_cost": str(maxima["input_cache_write"]),
            "cache_read_input_token_cost": str(maxima["input_cache_read"]),
            "input_cost_per_token": str(maxima["prompt"]),
            "max_input_tokens": max_input,
            "max_output_tokens": max_output,
            "model_group": model,
            "output_cost_per_token": str(maxima["completion"]),
            "pricing_source": OPENROUTER_BACKEND.pricing_path,
        }
    if set(selected) != expected:
        raise ValueError("OpenRouter models omitted a required model")
    models = tuple(selected[profile.upstream_model] for profile in PROFILE_CONTRACTS)
    encoded = canonical(list(models)).encode()
    return PricingSnapshot(
        models=models,
        sha256=hashlib.sha256(encoded).hexdigest(),
        backend=OPENROUTER_BACKEND.name,
        source=OPENROUTER_BACKEND.pricing_path,
    )


def query_model_pricing(
    *,
    parent_key: str,
    backend: BackendContract = LITELLM_BACKEND,
    upstream_url: str | None = None,
) -> PricingSnapshot:
    upstream_url = upstream_url or backend.upstream_base_url
    parsed = _parse_upstream_url(upstream_url)
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    kwargs: dict[str, Any] = {"timeout": BACKPRESSURE_TIMEOUT_SECONDS}
    if parsed.scheme == "https":
        kwargs["context"] = ssl.create_default_context()
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("backend upstream URL rejected")
    connection = connection_type(hostname, parsed.port, **kwargs)
    try:
        connection.request(
            "GET",
            _join_upstream_path(parsed.path, backend.pricing_path),
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
        document = strict_json(body)
        if backend.pricing_parser == "litellm-model-info":
            return _validate_pricing_document(document)
        if backend.pricing_parser == "openrouter-models":
            return _validate_openrouter_pricing_document(document)
        raise ValueError("backend pricing adapter is unsupported")
    finally:
        connection.close()


DOCKER_NAME = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,62}\Z")
CALIBRATION_NETWORK_POOL = ipaddress.IPv4Network("10.224.0.0/12")
CALIBRATION_NETWORK_PREFIX = 28
MAX_NETWORK_CANDIDATES = 256
DOCKER_POOL_OVERLAP = re.compile(
    rb"\bpool overlaps with other one on this address space\b", re.IGNORECASE
)


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


def _ip_routes() -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    environment.pop(PARENT_KEY_ENV, None)
    result = subprocess.run(  # nosec B603 B607
        ["ip", "-json", "route", "show", "table", "all"],
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("host policy route discovery failed")
    return result


def _parse_host_policy_subnets(data: bytes) -> tuple[ipaddress.IPv4Network, ...]:
    document = strict_json(data)
    if not isinstance(document, list):
        raise ValueError("host policy route schema rejected")
    networks: set[ipaddress.IPv4Network] = set()
    for route in document:
        if not isinstance(route, dict):
            raise ValueError("host policy route schema rejected")
        destination = route.get("dst")
        if destination in (None, "default"):
            continue
        if not isinstance(destination, str):
            raise ValueError("host policy route destination rejected")
        try:
            network = ipaddress.ip_network(destination, strict=False)
        except ValueError as error:
            raise ValueError("host policy route destination rejected") from error
        if isinstance(network, ipaddress.IPv4Network):
            networks.add(network)
    return tuple(
        sorted(
            networks, key=lambda value: (int(value.network_address), value.prefixlen)
        )
    )


def _host_policy_subnets() -> tuple[ipaddress.IPv4Network, ...]:
    return _parse_host_policy_subnets(_ip_routes().stdout)


def _parse_docker_network_subnets(
    document: Any,
) -> tuple[ipaddress.IPv4Network, ...]:
    if not isinstance(document, list):
        raise ValueError("Docker network IPAM schema rejected")
    networks: set[ipaddress.IPv4Network] = set()
    for inspected in document:
        if not isinstance(inspected, dict):
            raise ValueError("Docker network IPAM schema rejected")
        ipam = inspected.get("IPAM")
        if not isinstance(ipam, dict):
            raise ValueError("Docker network IPAM schema rejected")
        configs = ipam.get("Config")
        if configs is None:
            configs = []
        if not isinstance(configs, list):
            raise ValueError("Docker network IPAM schema rejected")
        for config in configs:
            if not isinstance(config, dict):
                raise ValueError("Docker network IPAM schema rejected")
            subnet = config.get("Subnet")
            if subnet is None:
                if config.get("Gateway") is not None:
                    raise ValueError("Docker network IPAM schema rejected")
                continue
            if not isinstance(subnet, str):
                raise ValueError("Docker network IPAM subnet rejected")
            try:
                network = ipaddress.ip_network(subnet, strict=False)
            except ValueError as error:
                raise ValueError("Docker network IPAM subnet rejected") from error
            if isinstance(network, ipaddress.IPv4Network):
                networks.add(network)
    return tuple(
        sorted(
            networks, key=lambda value: (int(value.network_address), value.prefixlen)
        )
    )


def _docker_network_subnets() -> tuple[ipaddress.IPv4Network, ...]:
    listed = _docker(["network", "ls", "-q"], check=False)
    if listed.returncode != 0:
        raise RuntimeError("Docker network discovery failed")
    identifiers = listed.stdout.decode("utf-8", errors="strict").split()
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Docker network discovery is ambiguous")
    if not identifiers:
        return ()
    inspected = _docker(["network", "inspect", *identifiers], check=False)
    if inspected.returncode != 0:
        raise RuntimeError("Docker network inspection failed")
    document = strict_json(inspected.stdout)
    if not isinstance(document, list) or len(document) != len(identifiers):
        raise ValueError("Docker network inspection is ambiguous")
    inspected_ids = []
    for network in document:
        if not isinstance(network, dict) or not isinstance(network.get("Id"), str):
            raise ValueError("Docker network inspection is ambiguous")
        inspected_ids.append(network["Id"])
    if len(inspected_ids) != len(set(inspected_ids)) or any(
        sum(inspected_id.startswith(identifier) for inspected_id in inspected_ids) != 1
        for identifier in identifiers
    ):
        raise ValueError("Docker network inspection is ambiguous")
    return _parse_docker_network_subnets(document)


def _network_candidates(run_id: str, ordinal: int) -> tuple[ipaddress.IPv4Network, ...]:
    _safe_docker_name(run_id, "run id")
    if ordinal <= 0:
        raise ValueError("Docker attempt ordinal must be positive")
    subnet_count = 1 << (
        CALIBRATION_NETWORK_PREFIX - CALIBRATION_NETWORK_POOL.prefixlen
    )
    digest = hashlib.sha256(f"{run_id}:{ordinal}".encode()).digest()
    start = int.from_bytes(digest[:8], "big") % subnet_count
    step = (int.from_bytes(digest[8:16], "big") % subnet_count) | 1
    block_size = 1 << (32 - CALIBRATION_NETWORK_PREFIX)
    base = int(CALIBRATION_NETWORK_POOL.network_address)
    return tuple(
        ipaddress.IPv4Network(
            (
                base + ((start + probe * step) % subnet_count) * block_size,
                CALIBRATION_NETWORK_PREFIX,
            )
        )
        for probe in range(MAX_NETWORK_CANDIDATES)
    )


def _create_attempt_network(names: DockerAttemptNames) -> DockerNetworkAllocation:
    routes = _host_policy_subnets()
    docker_subnets = _docker_network_subnets()
    labels = [
        value
        for key, value in names.role_labels("network").items()
        for value in ("--label", f"{key}={value}")
    ]
    route_rejections = 0
    docker_rejections = 0
    overlap_retries = 0
    for probe, candidate in enumerate(_network_candidates(names.run_id, names.ordinal)):
        if any(candidate.overlaps(route) for route in routes):
            route_rejections += 1
            continue
        if any(candidate.overlaps(network) for network in docker_subnets):
            docker_rejections += 1
            continue
        argv = [
            "network",
            "create",
            "--driver",
            "bridge",
            "--subnet",
            str(candidate),
            *labels,
            names.network,
        ]
        created = _docker(argv, check=False)
        if created.returncode == 0:
            return DockerNetworkAllocation(
                subnet=str(candidate),
                candidate_probe=probe,
                route_prefixes_checked=len(routes),
                docker_subnets_checked=len(docker_subnets),
                route_collision_rejections=route_rejections,
                docker_collision_rejections=docker_rejections,
                create_overlap_retries=overlap_retries,
            )
        if DOCKER_POOL_OVERLAP.search(created.stderr):
            overlap_retries += 1
            continue
        raise subprocess.CalledProcessError(
            created.returncode, ["docker", *argv], created.stdout, created.stderr
        )
    raise RuntimeError("calibration network candidate pool exhausted")


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
    dns = "".join(f"      - {resolver}\n" for resolver in MAIN_DNS)
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
        "    dns:\n"
        f"{dns}"
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
UNRESERVED_PROVIDER_FIELDS = frozenset(
    {"models", "plugins", "provider", "route", "transforms", "websearchoptions"}
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
        elif item_type == "reasoning":
            if set(item) != {"type", "encrypted_content", "summary"}:
                raise ValueError("broker responses reasoning shape rejected")
            if not isinstance(item["encrypted_content"], str):
                raise ValueError("broker responses reasoning content rejected")
            summary = item["summary"]
            if not isinstance(summary, list) or not all(
                isinstance(part, dict)
                and set(part) == {"type", "text"}
                and part["type"] == "summary_text"
                and isinstance(part["text"], str)
                for part in summary
            ):
                raise ValueError("broker responses reasoning summary rejected")
        elif item_type not in TOOL_CONTENT_TYPES:
            raise ValueError("broker responses content type rejected")
        _reject_media_fields(item, within_content=True)


def _validate_client_function_tools(document: dict[str, Any]) -> None:
    tools = document.get("tools")
    if tools is None:
        return
    if not isinstance(tools, list) or not tools:
        raise ValueError("broker tools rejected")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("broker server-side tool rejected")
    tool_choice = document.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") != "function":
        raise ValueError("broker server-side tool choice rejected")


def _reservation_for_body(body: bytes) -> Decimal:
    """Reserve from UTF-8 bytes: tokenizers byte-fallback to at most one token/byte."""
    return (
        Decimal(len(body)) * MAX_INPUT_OR_CACHE_COST_PER_TOKEN
        + Decimal(MAX_OUTPUT_TOKENS) * MAX_OUTPUT_COST_PER_TOKEN
        + RESERVATION_SAFETY_MARGIN
    )


def _deterministic_budget_split(
    available_budget: Decimal, attempt_count: int
) -> tuple[Decimal, ...]:
    allocation = available_budget / Decimal(attempt_count)
    allocations = [allocation] * (attempt_count - 1)
    allocations.append(available_budget - sum(allocations, start=Decimal(0)))
    return tuple(allocations)


def _allocate_attempt_budgets(
    available_budget: Decimal, *, attempts_per_profile: int
) -> tuple[Decimal, ...]:
    attempt_count = attempts_per_profile * len(PROFILE_CONTRACTS)
    if (
        not available_budget.is_finite()
        or available_budget < 0
        or attempts_per_profile not in {1, 2}
    ):
        raise ValueError("calibration attempt allocation input rejected")
    allocations = _deterministic_budget_split(available_budget, attempt_count)
    worst_request = _reservation_for_body(b"x" * MAX_BODY_BYTES)
    if any(item < worst_request for item in allocations):
        raise RuntimeError("attempt allocation cannot cover one worst-case request")
    if sum(allocations, start=Decimal(0)) != available_budget:
        raise RuntimeError("attempt allocations do not equal available budget")
    return allocations


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
    if UNRESERVED_PROVIDER_FIELDS & set(normalized_fields):
        raise ValueError("broker unreserved provider control rejected")
    forbidden_multiplicity = MULTIPLICITY_FIELDS & set(normalized_fields)
    if forbidden_multiplicity:
        raise ValueError("broker multiplicity field rejected")
    if any(normalized in FORBIDDEN_MEDIA_FIELDS for normalized in normalized_fields):
        raise ValueError("broker non-text modality rejected")
    _validate_client_function_tools(document)
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
        if stream:
            raise ValueError("broker streaming chat is unsupported")
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
    timeout: float,
) -> http.client.HTTPConnection:
    if parsed.hostname is None:
        raise ValueError("upstream host is missing")
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    if parsed.scheme == "http":
        return http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=timeout,
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
        except (RecursionError, RuntimeError, ValueError):
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
        settlement_failure: str | None = None
        response: http.client.HTTPResponse | None = None
        forwarding = ForwardingAdmission()
        upstream_opened = False
        try:
            if state.debug_deny_upstream:
                response_body = b'{"error":"diagnostic upstream denied"}'
                status = HTTPStatus.SERVICE_UNAVAILABLE
                state.release_unforwarded(reservation)
                settlement = "released_unforwarded"
                settlement_finalized = True
            elif state.fake_response_cost is not None:
                response_body = b'{"ok":true}'
                status = HTTPStatus.OK
                cost = state.fake_response_cost
                settlement = "known_fatal"
                state.finish_request(reservation, cost)
                settlement = "settled"
                settlement_finalized = True
            else:
                parsed = _parse_upstream_url(state.upstream_url)
                upstream = _upstream_connection(parsed, state.upstream_io_timeout())
                upstream.connect()
                upstream_opened = True
                state.send_connected_request(
                    upstream,
                    self.headers,
                    endpoint,
                    body,
                    _forward_headers(self),
                    forwarding,
                )
                potentially_paid = forwarding.started
                if upstream.sock is not None:
                    upstream.sock.settimeout(state.upstream_io_timeout())
                response = upstream.getresponse()
                status = response.status
                if state.backend.name == LITELLM_BACKEND.name:
                    call_id = _single_header(response.headers, "X-Litellm-Call-Id")
                    request_id = _single_header(
                        response.headers, "X-Litellm-Request-Id", "X-Request-Id"
                    )
                elif state.backend.name == OPENROUTER_BACKEND.name:
                    generation_id = _single_header(response.headers, "X-Generation-Id")
                    if generation_id is not None:
                        request_id = _response_identifier(generation_id)
                stream = content_type == "text/event-stream"
                if stream:
                    if status != HTTPStatus.OK:
                        raise ValueError(
                            "model streaming response status is unsuccessful"
                        )
                    content_type = _validate_stream_content_type(response.headers)
                else:
                    if state.backend.name == LITELLM_BACKEND.name:
                        cost = _parse_cost(response.headers)
                        usage = _bounded_usage(response.headers)
                    elif status != HTTPStatus.OK:
                        raise ValueError("OpenRouter response status is unsuccessful")
                declared_length = _response_framing(
                    response.headers, require_explicit=stream
                )
                response_body = _read_bounded_response(
                    response, upstream, state, declared_length
                )
                if stream:
                    if state.backend.name == OPENROUTER_BACKEND.name:
                        openrouter_settlement = _parse_openrouter_responses_stream(
                            response_body, expected_models=state.response_models
                        )
                        cost = _poll_openrouter_generation(
                            state,
                            settlement=openrouter_settlement,
                            generation_id=request_id,
                            streamed=True,
                        )
                        request_id = request_id or openrouter_settlement.response_id
                        usage = openrouter_settlement.usage
                    else:
                        cost, usage = _parse_responses_stream(response_body)
                elif state.backend.name == OPENROUTER_BACKEND.name:
                    openrouter_settlement = _parse_openrouter_nonstream(
                        response_body,
                        endpoint=endpoint,
                        expected_models=state.response_models,
                    )
                    cost = _poll_openrouter_generation(
                        state,
                        settlement=openrouter_settlement,
                        generation_id=request_id,
                        streamed=False,
                    )
                    request_id = request_id or openrouter_settlement.response_id
                    usage = openrouter_settlement.usage
                if cost is None:
                    raise RuntimeError("model response cost is unavailable")
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
        except (OSError, http.client.HTTPException, RuntimeError, ValueError) as error:
            if isinstance(error, OpenRouterSettlementError):
                settlement_failure = error.code
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
                            settlement_failure=settlement_failure,
                            retained_unknown_reservation_usd=str(retained),
                            upstream_opened=upstream_opened,
                            parent_authorization_sent=forwarding.started,
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
        canonical_model: str | None = None,
        ledger: SpendLedger,
        backend: BackendContract = LITELLM_BACKEND,
        max_input_tokens: int = MAX_BODY_BYTES,
        timeout: float = MAX_ATTEMPT_SECONDS,
        upstream_url: str | None = None,
        probe_token: str | None = None,
        token: str | None = None,
        evidence_path: Path | None = None,
        fake_response_cost: Decimal | None = None,
        pricing_sha256: str | None = None,
        inactive: bool = False,
        shutdown_event: threading.Event | None = None,
        debug_deny_upstream: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(48)
        self.state = BrokerState(
            parent_key=parent_key,
            token=None if inactive else self.token,
            probe_token=probe_token,
            model=model,
            canonical_model=canonical_model,
            max_input_tokens=max_input_tokens,
            deadline=time.monotonic() + timeout,
            ledger=ledger,
            backend=backend,
            upstream_url=upstream_url,
            evidence_path=evidence_path,
            fake_response_cost=fake_response_cost,
            pricing_sha256=pricing_sha256,
            shutdown_event=shutdown_event,
            debug_deny_upstream=debug_deny_upstream,
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
    models: Any,
    digest: str,
    model: str,
    max_input_tokens: int,
    backend: BackendContract = LITELLM_BACKEND,
    pricing_backend: str | None = None,
    pricing_source: str | None = None,
) -> dict[str, Any]:
    if pricing_backend not in {None, backend.name} or pricing_source not in {
        None,
        backend.pricing_path,
    }:
        raise ValueError("broker pricing backend binding mismatch")
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
    model_pricing = selected[0]
    if backend.name == OPENROUTER_BACKEND.name:
        _openrouter_canonical_model(
            model_pricing.get("canonical_model"), requested_model=model
        )
    return model_pricing


def _broker_child_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="calibration-broker")
    parser.add_argument("--backend", choices=tuple(BACKENDS), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-input-tokens", type=int, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--budget-cap", required=True)
    parser.add_argument("--pricing-json", required=True)
    parser.add_argument("--pricing-sha256", required=True)
    parser.add_argument("--pricing-backend", required=True)
    parser.add_argument("--pricing-source", required=True)
    parser.add_argument("--fake-response-cost")
    parser.add_argument("--debug-deny-upstream", action="store_true")
    args = parser.parse_args(argv)
    backend = BACKENDS[args.backend]
    models = strict_json(args.pricing_json.encode())
    model_pricing = _validate_broker_pricing_binding(
        models,
        args.pricing_sha256,
        args.model,
        args.max_input_tokens,
        backend,
        args.pricing_backend,
        args.pricing_source,
    )
    raw = bytearray(sys.stdin.buffer.readline(65537))
    if len(raw) > 65536:
        raise ValueError("broker credential payload exceeds limit")
    try:
        credentials = strict_json(bytes(raw))
    finally:
        raw[:] = b"\0" * len(raw)
    parent_key, probe_token = _validate_broker_credential_payload(credentials, backend)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_args: stop.set())
    broker = CalibrationBroker(
        host="0.0.0.0",  # nosec B104 - isolated, un-published Docker network
        port=DEFAULT_BROKER_PORT,
        parent_key=parent_key,
        token=None,
        probe_token=probe_token,
        model=args.model,
        canonical_model=model_pricing.get("canonical_model"),
        ledger=SpendLedger(Decimal(args.budget_cap)),
        backend=backend,
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
        debug_deny_upstream=args.debug_deny_upstream,
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
        backend: BackendContract = LITELLM_BACKEND,
        upstream_url: str | None = None,
        fake_response_cost: Decimal | None = None,
        debug_deny_upstream: bool = False,
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
        self.backend = backend
        self.upstream_url = upstream_url or backend.upstream_base_url
        self.fake_response_cost = fake_response_cost
        self.debug_deny_upstream = debug_deny_upstream
        self.process: subprocess.Popen[bytes] | None = None
        self.created_network = False
        self.created_container = False
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread: threading.Thread | None = None
        self.control_pipe_identity: tuple[int, int] | None = None
        self.activated = False
        self.network_allocation: DockerNetworkAllocation | None = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("broker sidecar already started")
        self.network_allocation = _create_attempt_network(self.names)
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
            *[
                value
                for resolver in self.backend.broker_dns
                for value in ("--dns", resolver)
            ],
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
            "--backend",
            self.backend.name,
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
            "--pricing-backend",
            self.pricing.backend,
            "--pricing-source",
            self.pricing.source,
        ]
        if self.fake_response_cost is not None:
            argv.extend(["--fake-response-cost", str(self.fake_response_cost)])
        if self.debug_deny_upstream:
            argv.append("--debug-deny-upstream")
        _docker(argv)
        self.created_container = True
        self.process = subprocess.Popen(  # nosec B603 B607
            ["docker", "start", "--attach", "--interactive", self.names.broker],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    self.backend.credential_env,
                    self.backend.deprecated_credential_env,
                }
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

    def start_heartbeat(self) -> None:
        if not self.activated:
            raise RuntimeError("broker heartbeat requires activation")
        if self.heartbeat_thread is not None:
            raise RuntimeError("broker heartbeat already started")

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
        host = inspected.get("HostConfig")
        if not isinstance(host, dict) or host.get("Dns") != list(
            self.backend.broker_dns
        ):
            raise RuntimeError("broker resolver policy rejected")
        attached = inspected.get("NetworkSettings", {}).get("Networks")
        if not isinstance(attached, dict) or set(attached) != {self.names.network}:
            raise RuntimeError("broker network attachment changed")
        attachment = attached[self.names.network]
        if not isinstance(attachment, dict):
            raise RuntimeError("broker network attachment changed")
        if (
            network.get("Driver") != "bridge"
            or network.get("Internal") is not False
            or network.get("Labels") != self.names.role_labels("network")
        ):
            raise RuntimeError("calibration network contract changed")
        allocation = self.network_allocation
        if allocation is None:
            raise RuntimeError("calibration network allocation evidence missing")
        ipam = network.get("IPAM")
        if not isinstance(ipam, dict):
            raise RuntimeError("calibration network IPAM contract changed")
        ipam_configs = ipam.get("Config")
        if not isinstance(ipam_configs, list) or len(ipam_configs) != 1:
            raise RuntimeError("calibration network IPAM contract changed")
        ipam_config = ipam_configs[0]
        if not isinstance(ipam_config, dict):
            raise RuntimeError("calibration network IPAM contract changed")
        subnet = ipam_config.get("Subnet")
        if not isinstance(subnet, str):
            raise RuntimeError("calibration network IPAM contract changed")
        try:
            inspected_subnet = ipaddress.ip_network(subnet, strict=True)
        except ValueError as error:
            raise RuntimeError("calibration network IPAM contract changed") from error
        if inspected_subnet != ipaddress.ip_network(allocation.subnet):
            raise RuntimeError("calibration network IPAM contract changed")
        gateway_values: set[str] = set()
        for value in (ipam_config.get("Gateway"), attachment.get("Gateway")):
            if value in (None, ""):
                continue
            if not isinstance(value, str):
                raise RuntimeError("calibration network gateway rejected")
            gateway_values.add(value)
        if len(gateway_values) != 1:
            raise RuntimeError("calibration network gateway rejected")
        gateway = next(iter(gateway_values))
        try:
            gateway_address = ipaddress.ip_address(gateway)
        except ValueError as error:
            raise RuntimeError("calibration network gateway rejected") from error
        if gateway_address not in ipaddress.ip_network(allocation.subnet):
            raise RuntimeError("calibration network gateway rejected")
        return {
            "parent_key_absent": True,
            "mounts": {"evidence_rw": True, "source_ro": True},
            "resolver_policy": {
                "addresses": list(self.backend.broker_dns),
                "purpose": self.backend.resolver_purpose,
                "role": "broker",
            },
            "network": {
                "allocation": dataclasses.asdict(allocation),
                "driver": "bridge",
                "gateway": gateway,
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
                    or document.get("schema_version") != 2
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

    def wait_inspect_activate(
        self, command: Future[CommandResult], attempt: dict[str, Any]
    ) -> dict[str, Any]:
        main = _run_phase(
            attempt,
            "main_discovery",
            lambda: self._discover_main(command, attempt),
        )
        evidence = _run_phase(
            attempt, "main_config_validation", lambda: self._validate_main(main)
        )
        _run_phase(attempt, "broker_activation", self.sidecar.activate)
        _run_phase(attempt, "heartbeat_start", self.sidecar.start_heartbeat)
        evidence["activation"] = {
            "completed_before_harbor_healthcheck": True,
            "control_channel": "anonymous-stdin-pipe",
            "main_has_control_channel": False,
        }
        return evidence

    def _discover_main(
        self, command: Future[CommandResult], attempt: dict[str, Any]
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 300
        main: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if command.done():
                try:
                    result = command.result()
                except BaseException as error:
                    attempt["command_outcome"] = _command_exception_evidence(
                        error,
                        self.output_root,
                        before_main_activation=True,
                    )
                    raise
                evidence, _document, _parse_status = _command_outcome_evidence(
                    result,
                    self.output_root,
                    before_main_activation=True,
                )
                attempt["command_outcome"] = evidence
                if result.returncode == 0:
                    raise EarlyCommandSuccess()
                raise EarlyCommandNonzeroReturn()
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
        return main

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
        if host.get("Dns") != list(MAIN_DNS):
            raise RuntimeError("Harbor main resolver policy rejected")
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
            "resolver_policy": {
                "addresses": list(MAIN_DNS),
                "purpose": "public-package-resolution",
                "role": "main",
            },
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


def _failure_evidence(error: BaseException) -> dict[str, str]:
    if isinstance(error, CalibrationStageError):
        return error.evidence
    if isinstance(error, AttemptFailure):
        return error.evidence
    return {
        "exception_class": type(error).__name__,
        "stage": "calibration_harness",
        "type": "unexpected_failure",
    }


def _native_exception_kind(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "malformed"
    if value in {"CancelledError", "NonZeroAgentExitCodeError"}:
        return value
    return "other"


def _native_agent_exit_diagnostic(
    exception_info: Any, *, exception_kind: str | None
) -> dict[str, Any]:
    if exception_kind != "NonZeroAgentExitCodeError":
        return {"status": "not_applicable"}
    message = getattr(exception_info, "exception_message", None)
    if not isinstance(message, str):
        return {"status": "malformed"}
    match = re.match(r"\ACommand failed \(exit ([1-9][0-9]{0,2})\):", message)
    if match is None:
        return {"status": "unavailable"}
    exit_code = int(match.group(1))
    if not 1 <= exit_code <= 255:
        return {"status": "unavailable"}
    return {
        "exit_code": exit_code,
        "exit_kind": "status" if exit_code < 128 else "signal_compatible_status",
        "status": "captured",
    }


OPENCODE_ERROR_KINDS = frozenset(
    {
        "APIError",
        "ContentFilterError",
        "ContextOverflowError",
        "MessageAbortedError",
        "MessageOutputLengthError",
        "ProviderAuthError",
        "StructuredOutputError",
        "UnknownError",
    }
)
OPENCODE_EVENT_KINDS = frozenset(
    {"error", "reasoning", "step_finish", "step_start", "text", "tool_use"}
)
OPENCODE_TOOL_STATUSES = frozenset({"completed", "error"})


def _opencode_error_kind(value: Any) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        return "malformed"
    name = value["name"]
    return name if name in OPENCODE_ERROR_KINDS else "other"


def _opencode_event_diagnostic(
    native: NativeSnapshot, *, trial_name: str
) -> dict[str, Any]:
    path = f"harbor-job/{trial_name}/agent/opencode.txt"
    data = native.files.get(path)
    if data is None:
        return {"status": "absent"}
    error_events = 0
    error_kinds: dict[str, int] = {}
    event_kinds: dict[str, int] = {}
    final_event_kind = None
    malformed_lines = 0
    parsed_events = 0
    tool_statuses: dict[str, int] = {}
    for line_number, line in enumerate(io.BytesIO(data), start=1):
        if line_number > 10_000 or len(line) > 1 << 20:
            return {"status": "malformed"}
        if not line.strip():
            continue
        try:
            event = strict_json(line)
        except RecursionError:
            return {"status": "malformed"}
        except ValueError:
            malformed_lines += 1
            event_kinds["malformed"] = event_kinds.get("malformed", 0) + 1
            final_event_kind = "malformed"
            continue
        parsed_events += 1
        event_type = event.get("type") if isinstance(event, dict) else None
        if not isinstance(event_type, str):
            event_kind = "malformed"
        elif event_type in OPENCODE_EVENT_KINDS:
            event_kind = event_type
        else:
            event_kind = "other"
        event_kinds[event_kind] = event_kinds.get(event_kind, 0) + 1
        final_event_kind = event_kind
        if event_type == "error":
            error_events += 1
            kind = _opencode_error_kind(event.get("error"))
            error_kinds[kind] = error_kinds.get(kind, 0) + 1
        if event_type == "tool_use":
            part = event.get("part")
            state = part.get("state") if isinstance(part, dict) else None
            status = state.get("status") if isinstance(state, dict) else None
            if not isinstance(status, str):
                tool_status = "malformed"
            elif status in OPENCODE_TOOL_STATUSES:
                tool_status = status
            else:
                tool_status = "other"
            tool_statuses[tool_status] = tool_statuses.get(tool_status, 0) + 1
    return {
        "error_event_count": error_events,
        "error_kinds": dict(sorted(error_kinds.items())),
        "event_kinds": dict(sorted(event_kinds.items())),
        "final_event_kind": final_event_kind,
        "malformed_line_count": malformed_lines,
        "parsed_event_count": parsed_events,
        "status": "captured",
        "tool_statuses": dict(sorted(tool_statuses.items())),
    }


def _trajectory_shape_diagnostic(
    native: NativeSnapshot, *, trial_name: str
) -> dict[str, Any]:
    path = f"harbor-job/{trial_name}/agent/trajectory.json"
    data = native.files.get(path)
    if data is None:
        return {"status": "absent"}
    if len(data) > 4 << 20:
        return {"status": "malformed"}
    try:
        document = strict_json(data)
        steps = document.get("steps") if isinstance(document, dict) else None
        if not isinstance(steps, list) or len(steps) > 10_000:
            raise ValueError("trajectory steps are malformed")
        agent_step_count = 0
        final = None
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("trajectory step is malformed")
            if step.get("source") == "agent":
                agent_step_count += 1
                final = step
        if final is None:
            final_shape = None
        else:
            tool_calls = final.get("tool_calls")
            if tool_calls is not None and not isinstance(tool_calls, list):
                raise ValueError("trajectory tool calls are malformed")
            observation = final.get("observation")
            if observation is None:
                observation_results = None
            elif isinstance(observation, dict):
                observation_results = observation.get("results")
                if observation_results is not None and not isinstance(
                    observation_results, list
                ):
                    raise ValueError("trajectory observation is malformed")
            else:
                raise ValueError("trajectory observation is malformed")
            final_shape = {
                "message_present": isinstance(final.get("message"), str)
                and bool(final["message"]),
                "observation_result_count": len(observation_results or []),
                "reasoning_present": isinstance(final.get("reasoning_content"), str)
                and bool(final["reasoning_content"]),
                "tool_call_count": len(tool_calls or []),
            }
    except (RecursionError, ValueError):
        return {"status": "malformed"}
    return {
        "agent_step_count": agent_step_count,
        "final_agent_step": final_shape,
        "status": "captured",
        "step_count": len(steps),
    }


def _native_runtime_diagnostic(
    native: NativeSnapshot, *, job: JobResult, trial: TrialResult
) -> dict[str, Any]:
    completed = job.stats.n_completed_trials
    errored = job.stats.n_errored_trials
    cancelled = job.stats.n_cancelled_trials
    exception_kind = _native_exception_kind(
        trial.exception_info.exception_type
        if trial.exception_info is not None
        else None
    )
    if (
        type(job.n_total_trials) is not int
        or job.n_total_trials != 1
        or type(completed) is not int
        or type(errored) is not int
        or type(cancelled) is not int
        or completed != 1
        or errored != int(trial.exception_info is not None)
        or cancelled != int(exception_kind == "CancelledError")
    ):
        return {"status": "malformed"}
    rewards = (
        trial.verifier_result.rewards if trial.verifier_result is not None else None
    )
    reward = rewards.get("reward") if rewards is not None else None
    if trial.verifier_result is None:
        retained_reward = None
    elif type(reward) is int and reward in {0, 1}:
        retained_reward = reward
    elif isinstance(reward, float) and math.isfinite(reward) and reward in {0, 1}:
        retained_reward = int(reward)
    else:
        return {"status": "malformed"}
    return {
        "job": {
            "cancelled_trials": cancelled,
            "completed_trials": completed,
            "errored_trials": errored,
        },
        "opencode_events": _opencode_event_diagnostic(
            native, trial_name=trial.trial_name
        ),
        "status": "captured",
        "trajectory": _trajectory_shape_diagnostic(native, trial_name=trial.trial_name),
        "trial": {
            "agent_result_present": trial.agent_result is not None,
            "exit": _native_agent_exit_diagnostic(
                trial.exception_info, exception_kind=exception_kind
            ),
            "exception_kind": exception_kind,
            "verifier_result_present": trial.verifier_result is not None,
            "verifier_reward": retained_reward,
        },
    }


def _native_diagnostic(output: Path) -> dict[str, Any]:
    try:
        metadata = os.lstat(output)
    except FileNotFoundError:
        return {"output": {"state": "absent"}, "snapshot": {"status": "absent"}}
    output_state = {
        "mode": oct(stat.S_IMODE(metadata.st_mode)),
        "state": "directory" if stat.S_ISDIR(metadata.st_mode) else "unsafe_type",
    }
    try:
        native = snapshot_native_output(output)
    except BaseException as error:
        return {
            "output": output_state,
            "snapshot": {
                "exception_classes": [type(error).__name__],
                "status": "failed",
            },
        }
    files = [item for item in native.manifest if item["type"] == "file"]
    directories = [item for item in native.manifest if item["type"] == "directory"]
    result_count = 0
    parsed_results = 0
    exception_classes: set[str] = set()
    parsed_job: JobResult | None = None
    parsed_trials: list[TrialResult] = []
    for path, data in native.files.items():
        if path == "harbor-job/result.json":
            result_count += 1
            try:
                parsed_job = JobResult.model_validate_json(data)
                parsed_results += 1
            except BaseException as error:
                exception_classes.add(type(error).__name__)
        elif path.startswith("harbor-job/") and path.endswith("/result.json"):
            result_count += 1
            try:
                parsed_trials.append(TrialResult.model_validate_json(data))
                parsed_results += 1
            except BaseException as error:
                exception_classes.add(type(error).__name__)
    inspection_status = (
        "valid"
        if result_count >= 1 and result_count == parsed_results
        else "invalid"
        if exception_classes
        else "incomplete"
    )
    return {
        "output": output_state,
        "snapshot": {
            "directory_count": len(directories),
            "entry_count": len(native.manifest),
            "file_count": len(files),
            "manifest_sha256": manifest_digest(native.manifest),
            "status": "captured",
        },
        "structure": {
            "exception_classes": sorted(exception_classes),
            "parsed_result_count": parsed_results,
            "result_count": result_count,
            "status": inspection_status,
        },
        "runtime": (
            _native_runtime_diagnostic(native, job=parsed_job, trial=parsed_trials[0])
            if inspection_status == "valid"
            and result_count == 2
            and parsed_job is not None
            and len(parsed_trials) == 1
            else {"status": "unavailable"}
        ),
    }


def _command_outcome_evidence(
    result: CommandResult,
    output: Path,
    *,
    before_main_activation: bool = False,
) -> tuple[dict[str, Any], Any | None, str]:
    stdout_sha256 = hashlib.sha256(result.stdout).hexdigest()
    stderr_sha256 = hashlib.sha256(result.stderr).hexdigest()
    document: Any | None = None
    parse_status = "malformed"
    canonical_fields: dict[str, Any] | None = None
    try:
        candidate = strict_json(result.stdout)
        if result.stdout == (canonical(candidate) + "\n").encode():
            document = candidate
            parse_status = "canonical"
            if isinstance(candidate, dict):
                fields: dict[str, Any] = {}
                schema_version = candidate.get("schema_version")
                outcome = candidate.get("outcome")
                reward = candidate.get("reward")
                if type(schema_version) is int:
                    fields["schema_version"] = schema_version
                if outcome in {"succeeded", "failed", "cancelled"}:
                    fields["outcome"] = outcome
                if reward in {"0", "1"}:
                    fields["reward"] = reward
                canonical_fields = fields
        else:
            parse_status = "noncanonical"
    except BaseException:
        pass
    return (
        {
            "canonical_cli": canonical_fields,
            "completion": {
                "before_main_activation": before_main_activation,
                "exception_class": None,
                "kind": (
                    "successful_early_exit"
                    if before_main_activation and result.returncode == 0
                    else "nonzero_return"
                    if result.returncode != 0
                    else "successful_return"
                ),
            },
            "containment": result.containment,
            "native": _native_diagnostic(output),
            "return_code": result.returncode,
            "status": "returned",
            "stderr": {"bytes": len(result.stderr), "sha256": stderr_sha256},
            "stdout": {
                "bytes": len(result.stdout),
                "parse_status": parse_status,
                "sha256": stdout_sha256,
            },
        },
        document,
        parse_status,
    )


def _command_exception_kind(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout_exception"
    if isinstance(error, ValueError):
        return "output_exception"
    if isinstance(error, RuntimeError):
        return "containment_exception"
    return "command_exception"


def _command_exception_evidence(
    error: BaseException, output: Path, *, before_main_activation: bool
) -> dict[str, Any]:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    return {
        "canonical_cli": None,
        "completion": {
            "before_main_activation": before_main_activation,
            "exception_class": type(error).__name__,
            "kind": _command_exception_kind(error),
        },
        "native": _native_diagnostic(output),
        "return_code": None,
        "status": "exception",
        "stderr": {"bytes": 0, "sha256": empty_sha256},
        "stdout": {
            "bytes": 0,
            "parse_status": "unavailable",
            "sha256": empty_sha256,
        },
    }


def _validate_cli_outcome(
    result: CommandResult,
    document: Any | None,
    parse_status: str,
    *,
    debug_deny_upstream: bool = False,
) -> dict[str, Any]:
    if not debug_deny_upstream and result.returncode != 0:
        raise AttemptFailure(
            "agent_install_or_execution", "nonzero_exit", RuntimeError()
        )
    if debug_deny_upstream and result.returncode == 0:
        raise AttemptFailure(
            "deny_upstream_diagnostic", "unexpected_zero_exit", RuntimeError()
        )
    if result.stderr:
        raise AttemptFailure(
            "agent_install_or_execution", "unexpected_stderr", ValueError()
        )
    if parse_status != "canonical" or not isinstance(document, dict):
        raise AttemptFailure("cli_schema", "malformed_stdout", ValueError())
    if set(document) != {
        "job_directory",
        "outcome",
        "reward",
        "schema_version",
        "summary",
    }:
        raise AttemptFailure("cli_schema", "schema_mismatch", ValueError())
    if document.get("schema_version") != 1:
        raise AttemptFailure("cli_schema", "schema_version", ValueError())
    expected_outcome = "failed" if debug_deny_upstream else "succeeded"
    expected_rewards = {"0"} if debug_deny_upstream else {"0", "1"}
    if (
        document["outcome"] != expected_outcome
        or document["reward"] not in expected_rewards
    ):
        raise AttemptFailure("cli_outcome", "not_succeeded", ValueError())
    return document


def _started_attempt(
    *,
    ordinal: int,
    profile: str,
    model_group: str,
    allocation_usd: Decimal = MAX_TOTAL_COST,
    debug_deny_upstream: bool = False,
) -> dict[str, Any]:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    return {
        "admissible": False,
        "allocation_usd": str(allocation_usd),
        "broker": {
            "locked_endpoint": None,
            "request_count": 0,
            "requests": [],
        },
        "command_outcome": {
            "canonical_cli": None,
            "completion": {
                "before_main_activation": False,
                "exception_class": None,
                "kind": "not_completed",
            },
            "native": {"output": {"state": "not_inspected"}},
            "return_code": None,
            "status": "not_returned",
            "stderr": {"bytes": 0, "sha256": empty_sha256},
            "stdout": {
                "bytes": 0,
                "parse_status": "not_returned",
                "sha256": empty_sha256,
            },
        },
        "model": model_group,
        "model_group": model_group,
        "diagnostic": {
            "admissible": False,
            "deny_upstream": debug_deny_upstream,
        },
        "ordinal": ordinal,
        "outcome": "started",
        "phases": _new_phase_evidence(),
        "profile": profile,
        "spend": {
            "exposure_authority": "preactivation_zero",
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
            "exposure_authority": "broker_ledger",
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
    attempt["failure"] = _failure_evidence(error)
    attempt["outcome"] = "failed"


def _ledger_spend_evidence(
    document: Any,
    *,
    allocation: Decimal,
    activation_started: bool,
    activation_token_sha256: str,
) -> dict[str, str]:
    if not isinstance(document, dict):
        raise ValueError("broker ledger is malformed")
    request_count = document.get("request_count")
    requests = document.get("requests")
    if (
        document.get("schema_version") != 2
        or type(document.get("active")) is not bool
        or type(document.get("activated")) is not bool
        or (
            document.get("activation_token_sha256") is not None
            and (
                type(document.get("activation_token_sha256")) is not str
                or len(document["activation_token_sha256"]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in document["activation_token_sha256"]
                )
            )
        )
        or type(request_count) is not int
        or not 0 <= request_count <= MAX_REQUESTS
        or not isinstance(requests, list)
        or len(requests) != request_count
        or not all(isinstance(request, dict) for request in requests)
        or (
            document.get("fatal") is not None and type(document.get("fatal")) is not str
        )
        or type(document.get("known_actual_cost_usd")) is not str
        or type(document.get("retained_unknown_reservation_usd")) is not str
    ):
        raise ValueError("broker ledger is malformed")
    if activation_started:
        if (
            document["activated"] is not True
            or document["activation_token_sha256"] != activation_token_sha256
        ):
            raise ValueError("broker ledger activation authority is stale")
    elif (
        document["active"] is not False
        or document["activated"] is not False
        or document["activation_token_sha256"] is not None
        or request_count != 0
        or requests
    ):
        raise ValueError("broker ledger preactivation authority is invalid")
    try:
        known = Decimal(document["known_actual_cost_usd"])
        retained = Decimal(document["retained_unknown_reservation_usd"])
    except (InvalidOperation, KeyError, TypeError) as error:
        raise ValueError("broker ledger spend is malformed") from error
    if (
        not known.is_finite()
        or known < 0
        or not retained.is_finite()
        or retained < 0
        or known + retained > allocation
    ):
        raise ValueError("broker ledger spend exceeds attempt allocation")
    if not activation_started and (known != 0 or retained != 0):
        raise ValueError("broker ledger preactivation spend is nonzero")
    return {
        "exposure_authority": "broker_ledger",
        "known_actual_cost_usd": str(known),
        "retained_unknown_reservation_usd": str(retained),
        "total_exposure_usd": str(known + retained),
    }


def _fallback_attempt_spend(
    attempt: Mapping[str, Any], *, allocation: Decimal, activated: bool
) -> dict[str, str]:
    activation = next(
        (
            item
            for item in attempt.get("phases", {}).get("timeline", [])
            if item.get("phase") == "broker_activation"
        ),
        {},
    )
    may_have_forwarded = activated or activation.get("started") is True
    retained = allocation if may_have_forwarded else Decimal(0)
    return {
        "exposure_authority": (
            "conservative_allocation_fallback"
            if may_have_forwarded
            else "preactivation_zero"
        ),
        "known_actual_cost_usd": "0",
        "retained_unknown_reservation_usd": str(retained),
        "total_exposure_usd": str(retained),
    }


def _validate_deny_upstream_ledger(document: Mapping[str, Any]) -> None:
    requests = document.get("requests")
    if (
        document.get("request_count") != DENY_UPSTREAM_EXPECTED_REQUESTS
        or not isinstance(requests, list)
        or len(requests) != DENY_UPSTREAM_EXPECTED_REQUESTS
        or document.get("known_actual_cost_usd") != "0"
        or Decimal(document.get("retained_unknown_reservation_usd", "-1")) != 0
    ):
        raise AttemptFailure(
            "deny_upstream_diagnostic", "ledger_summary_mismatch", ValueError()
        )
    expected = {
        "call_id": None,
        "cost": None,
        "disconnected": False,
        "parent_authorization_sent": False,
        "request_id": None,
        "response_bytes": 38,
        "retained_unknown_reservation_usd": "0",
        "settlement": "released_unforwarded",
        "status": 503,
        "upstream_opened": False,
        "usage": {},
    }
    for request in requests:
        if not isinstance(request, dict) or any(
            request.get(field) != value for field, value in expected.items()
        ):
            raise AttemptFailure(
                "deny_upstream_diagnostic",
                "request_forwarded_or_malformed",
                ValueError(),
            )


def _validate_native_attempt(
    *,
    output: Path,
    document: dict[str, Any],
    ordinal: int,
    expected_task_checksum: str,
    expected_task_digest: str,
    harbor_model: str,
    snapshot_status: str,
    debug_deny_upstream: bool = False,
) -> tuple[NativeSnapshot, dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        native: NativeSnapshot = snapshot_native_output(output)
        record = _native_run_record(
            native,
            document,
            ordinal=ordinal,
            expected_task_checksum=expected_task_checksum,
            expected_task_digest=expected_task_digest,
            expected_agent_name="opencode",
            expected_model_name=harbor_model,
            expected_reward=int(document["reward"]),
            expected_exception_type=(
                "NonZeroAgentExitCodeError" if debug_deny_upstream else None
            ),
            require_atif=not debug_deny_upstream,
        )
    except BaseException as error:
        failure_type = "snapshot" if snapshot_status == "failed" else "validation"
        raise AttemptFailure("native_output", failure_type, error) from error
    if record["native"]["trial"]["agent"]["name"] != "opencode":
        raise ValueError("native OpenCode agent identity mismatch")
    if (
        not debug_deny_upstream
        and record["trajectory"]["agent"]["model_name"] != harbor_model
    ):
        raise ValueError("OpenCode ATIF model identity mismatch")
    trial_name = record["native"]["trial"]["trial_name"]
    diagnostics_path = f"harbor-job/{trial_name}/verifier/diagnostics.json"
    diagnostics = strict_json(native.read(diagnostics_path))
    gates = diagnostics.get("gates") if isinstance(diagnostics, dict) else None
    reward = int(document["reward"])
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
    if debug_deny_upstream:
        job = JobResult.model_validate_json(native.read("harbor-job/result.json"))
        if record["trajectory"] is not None or any(
            value is not None
            for value in (
                job.stats.n_cache_tokens,
                job.stats.n_input_tokens,
                job.stats.n_output_tokens,
                job.stats.cost_usd,
            )
        ):
            raise ValueError("diagnostic native usage evidence is nonzero")
        return (
            native,
            record,
            gates,
            {
                "atif": None,
                "harbor": {
                    "cache_tokens": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "reported_cost_usd": None,
                },
            },
        )
    trajectory_metrics = _validate_metrics(record)
    harbor_metrics = _validate_harbor_metrics(
        native,
        trial_name=trial_name,
        trajectory_metrics=trajectory_metrics,
    )
    return (
        native,
        record,
        gates,
        {
            "atif": trajectory_metrics,
            "harbor": harbor_metrics,
        },
    )


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
    attempt_allocation: Decimal,
    attempt_record: dict[str, Any],
    backend: BackendContract = LITELLM_BACKEND,
    debug_deny_upstream: bool = False,
) -> dict[str, Any]:
    profile_contract = _profile_contract(profile)
    if model_group != profile_contract.broker_model:
        raise ValueError("calibration model identity mismatch")
    model = profile_contract.broker_model
    harbor_model = profile_contract.harbor_model
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
        budget_cap=attempt_allocation,
        backend=backend,
        debug_deny_upstream=debug_deny_upstream,
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
    native_validation: (
        tuple[NativeSnapshot, dict[str, Any], dict[str, Any], dict[str, Any]] | None
    ) = None
    security_evidence: dict[str, Any] | None = None
    attempt_error: BaseException | None = None
    try:
        try:
            _run_phase(attempt_record, "sidecar_start", sidecar.start)
            _run_phase(attempt_record, "topology_probe", sidecar.probe)
            environment = child_environment(
                config_root=config_root,
                private_home=home,
                token=attempt_token,
                base_url=f"http://{names.alias}:{DEFAULT_BROKER_PORT}/v1",
            )
            executor = ThreadPoolExecutor(max_workers=1)
            command_future: Future[CommandResult] | None = None
            try:
                command_future = _run_phase(
                    attempt_record,
                    "cli_spawn",
                    lambda: executor.submit(
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
                        check=False,
                    ),
                )
                network_evidence = authority.wait_inspect_activate(
                    command_future, attempt_record
                )

                def wait_for_cli() -> tuple[CommandResult, dict[str, Any]]:
                    try:
                        command_result = command_future.result(
                            timeout=MAX_ATTEMPT_SECONDS
                        )
                    except BaseException as error:
                        attempt_record["command_outcome"] = _command_exception_evidence(
                            error, output, before_main_activation=False
                        )
                        raise
                    command_evidence, command_document, parse_status = (
                        _command_outcome_evidence(command_result, output)
                    )
                    attempt_record["command_outcome"] = command_evidence
                    validated = _validate_cli_outcome(
                        command_result,
                        command_document,
                        parse_status,
                        debug_deny_upstream=debug_deny_upstream,
                    )
                    return command_result, validated

                result, document = _run_phase(attempt_record, "cli_wait", wait_for_cli)
            except BaseException:
                _remove_owned_attempt_containers(names)
                if command_future is not None:
                    try:
                        completed_result = command_future.result(timeout=30)
                    except BaseException as command_error:
                        if (
                            attempt_record["command_outcome"]["status"]
                            == "not_returned"
                        ):
                            attempt_record["command_outcome"] = (
                                _command_exception_evidence(
                                    command_error,
                                    output,
                                    before_main_activation=True,
                                )
                            )
                    else:
                        if (
                            attempt_record["command_outcome"]["status"]
                            == "not_returned"
                        ):
                            command_evidence, _document, _parse_status = (
                                _command_outcome_evidence(
                                    completed_result,
                                    output,
                                    before_main_activation=True,
                                )
                            )
                            attempt_record["command_outcome"] = command_evidence
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)

            def read_attempt_ledger() -> tuple[dict[str, Any], dict[str, Any]]:
                security = sidecar.inspect_secret_boundary()
                try:
                    ledger = sidecar.read_ledger(minimum_requests=1)
                except BaseException as error:
                    raise AttemptFailure(
                        "broker_evidence", "minimum_requests", error
                    ) from error
                requests = ledger["requests"]
                if not requests:
                    raise ValueError(
                        "completed calibration attempt made no model request"
                    )
                if ledger["locked_endpoint"] is None or any(
                    item["endpoint"] != ledger["locked_endpoint"] for item in requests
                ):
                    raise ValueError(
                        "calibration attempt endpoint lock evidence is invalid"
                    )
                if ledger["fatal"] is not None:
                    raise ValueError("calibration attempt ledger is fatal")
                if debug_deny_upstream:
                    _validate_deny_upstream_ledger(ledger)
                return security, ledger

            security_evidence, ledger_document = _run_phase(
                attempt_record, "ledger_read", read_attempt_ledger
            )
            snapshot_status = attempt_record["command_outcome"]["native"]["snapshot"][
                "status"
            ]
            native_validation = _run_phase(
                attempt_record,
                "native_validation",
                lambda: _validate_native_attempt(
                    output=output,
                    document=document,
                    ordinal=ordinal,
                    expected_task_checksum=expected_task_checksum,
                    expected_task_digest=expected_task_digest,
                    harbor_model=harbor_model,
                    snapshot_status=snapshot_status,
                    debug_deny_upstream=debug_deny_upstream,
                ),
            )
        except BaseException as error:
            attempt_error = error
            raise
        finally:
            try:
                cleanup_evidence = _run_phase(
                    attempt_record, "cleanup", sidecar.cleanup
                )
                attempt_record["cleanup"] = {"status": "completed"}
            except BaseException as cleanup_error:
                attempt_record["cleanup"] = {
                    "exception_class": (
                        cleanup_error.cause_class
                        if isinstance(cleanup_error, CalibrationStageError)
                        else type(cleanup_error).__name__
                    ),
                    "status": "failed",
                }
                if attempt_error is None:
                    raise cleanup_error
    except BaseException as error:
        if ledger_document is None:
            with suppress(BaseException):
                ledger_document = sidecar.read_ledger()
        activation = next(
            (
                item
                for item in attempt_record.get("phases", {}).get("timeline", [])
                if item.get("phase") == "broker_activation"
            ),
            {},
        )
        activation_started = bool(
            getattr(sidecar, "activated", False) or activation.get("started") is True
        )
        activation_token_sha256 = hashlib.sha256(attempt_token.encode()).hexdigest()
        try:
            spend = _ledger_spend_evidence(
                ledger_document,
                allocation=attempt_allocation,
                activation_started=activation_started,
                activation_token_sha256=activation_token_sha256,
            )
        except ValueError:
            spend = _fallback_attempt_spend(
                attempt_record,
                allocation=attempt_allocation,
                activated=bool(getattr(sidecar, "activated", False)),
            )
            ledger_document = None
        if ledger_document is not None:
            request_count = ledger_document.get("request_count")
            requests = ledger_document.get("requests")
            locked_endpoint = ledger_document.get("locked_endpoint")
            if type(request_count) is int and isinstance(requests, list):
                attempt_record["broker"] = {
                    "locked_endpoint": locked_endpoint,
                    "request_count": request_count,
                    "requests": requests,
                }
        attempt_record["spend"] = spend
        _mark_attempt_failed(attempt_record, error)
        raise
    if ledger_document is None or cleanup_evidence is None or native_validation is None:
        raise RuntimeError("broker lifecycle evidence is incomplete")
    requests = ledger_document["requests"]
    broker_evidence = {
        "backend": ledger_document["backend"],
        "locked_endpoint": ledger_document["locked_endpoint"],
        "pricing_sha256": ledger_document["pricing_sha256"],
        "probe_consumed": ledger_document["probe_consumed"],
        "request_count": ledger_document["request_count"],
        "requests": requests,
        "settlement_source": ledger_document["settlement_source"],
        "security": security_evidence,
        "shutdown": cleanup_evidence,
    }
    spend = _ledger_spend_evidence(
        ledger_document,
        allocation=attempt_allocation,
        activation_started=True,
        activation_token_sha256=hashlib.sha256(attempt_token.encode()).hexdigest(),
    )
    attempt_record["broker"] = broker_evidence
    attempt_record["spend"] = spend
    attempt_record["outcome"] = "failed"
    duration = time.monotonic() - started
    reward = int(document["reward"])
    _native, record, gates, metrics = native_validation
    completed = {
        "allocation_usd": str(attempt_allocation),
        "admissible": False,
        "broker": broker_evidence,
        "command_outcome": attempt_record["command_outcome"],
        "containment": result.containment,
        "duration_seconds": str(round(duration, 3)),
        "gates": gates,
        "metrics": metrics,
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
        "model_identities": dataclasses.asdict(profile_contract),
        "outcome": "diagnostic_succeeded" if debug_deny_upstream else "succeeded",
        "diagnostic": {
            "admissible": False,
            "deny_upstream": debug_deny_upstream,
            "passed": debug_deny_upstream,
        },
        "phases": attempt_record["phases"],
        "trajectory": (
            None
            if debug_deny_upstream
            else {
                "agent": record["trajectory"]["agent"],
                "schema_version": record["trajectory"]["schema_version"],
                "sha256": record["trajectory"]["sha256"],
                "step_count": record["trajectory"]["step_count"],
            }
        ),
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
    prior_known = args.prior_known_cost_usd
    prior_unknown = args.prior_unknown_exposure_usd
    prior_total = prior_known + prior_unknown
    backend = BACKENDS[args.backend]
    planned_allocations = _deterministic_budget_split(
        MAX_TOTAL_COST - prior_total,
        args.attempts_per_profile * len(PROFILE_CONTRACTS),
    )
    return {
        "admissible": False,
        "attempt_count": len(completed),
        "attempts": completed,
        "attempts_per_profile": args.attempts_per_profile,
        "budget": {
            "attempt_allocations_usd": [str(item) for item in planned_allocations],
            "available_budget_usd": str(MAX_TOTAL_COST - prior_total),
            "total_cap_usd": str(MAX_TOTAL_COST),
        },
        "diagnostic": {
            "admissible": False,
            "deny_upstream": args.debug_deny_upstream,
        },
        "backend": _backend_evidence(backend),
        "failure": _failure_evidence(error),
        "ok": False,
        "schema_version": 1,
        "spend_exposure": {
            "prior_known_cost_usd": str(prior_known),
            "prior_unknown_exposure_usd": str(prior_unknown),
            "current_known_cost_usd": str(known),
            "current_retained_exposure_usd": str(retained),
            "known_actual_cost_usd": str(prior_known + known),
            "retained_unknown_reservation_usd": str(prior_unknown + retained),
            "total_usd": str(prior_total + known + retained),
        },
        "task_id": TASK_ID,
        "total_authoritative_cost_usd": str(prior_known + known),
        "pricing": (
            {
                "backend": pricing.backend,
                "models": list(pricing.models),
                "sha256": pricing.sha256,
                "source": pricing.source,
            }
            if pricing is not None
            else None
        ),
    }


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write((canonical(value) + "\n").encode())
    sys.stdout.buffer.flush()


def _backend_evidence(backend: BackendContract) -> dict[str, Any]:
    return {
        "broker_dns": list(backend.broker_dns),
        "endpoint_mapping": [
            {"child": child, "upstream": upstream}
            for child, upstream in backend.endpoint_paths
        ],
        "name": backend.name,
        "pricing_source": backend.pricing_path,
        "profiles": [dataclasses.asdict(profile) for profile in PROFILE_CONTRACTS],
        "settlement_source": backend.settlement,
        "upstream_base_url": backend.upstream_base_url,
    }


def _nonnegative_usd(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(
            "must be a finite nonnegative decimal"
        ) from error
    if not parsed.is_finite() or parsed < 0 or parsed > MAX_TOTAL_COST:
        raise argparse.ArgumentTypeError(
            "must be a finite nonnegative decimal at most 25"
        )
    return parsed


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=tuple(BACKENDS), default="openrouter")
    parser.add_argument("--attempts-per-profile", type=int)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-deny-upstream", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--prior-known-cost-usd",
        type=_nonnegative_usd,
        default=Decimal(0),
    )
    parser.add_argument(
        "--prior-unknown-exposure-usd",
        type=_nonnegative_usd,
        default=Decimal(0),
    )
    args = parser.parse_args(argv)
    if args.prior_known_cost_usd + args.prior_unknown_exposure_usd > MAX_TOTAL_COST:
        parser.error("combined prior cost and exposure must be at most 25")
    expected_attempts = 1 if args.debug else 2
    if args.attempts_per_profile is None:
        args.attempts_per_profile = expected_attempts
    elif args.attempts_per_profile != expected_attempts:
        parser.error(
            "debug requires exactly one attempt per profile; proof requires exactly two"
        )
    if args.output is not None and (args.debug or args.attempts_per_profile != 2):
        parser.error("--output requires a clean exact-four calibration")
    if args.debug_deny_upstream and not args.debug:
        parser.error("--debug-deny-upstream requires --debug")
    if args.debug_deny_upstream and args.output is not None:
        parser.error("--debug-deny-upstream does not permit --output")
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
        backend = BACKENDS[args.backend]
        if args.output is not None:
            authority = open_proof_output_authority(args.output)
        parent_key = _take_backend_credential(backend, os.environ)
        prior_total = args.prior_known_cost_usd + args.prior_unknown_exposure_usd
        available_budget = MAX_TOTAL_COST - prior_total
        attempt_allocations = _allocate_attempt_budgets(
            available_budget, attempts_per_profile=args.attempts_per_profile
        )
        with tempfile.TemporaryDirectory(prefix="authority-calibration-") as directory:
            private_root = Path(directory)
            snapshot: SourceSnapshot = (
                create_debug_source_snapshot(private_root)
                if args.debug
                else create_clean_source_snapshot(private_root)
            )
            sweep_stale_calibration_resources()
            pricing = query_model_pricing(parent_key=parent_key, backend=backend)
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
            for profile_contract in PROFILE_CONTRACTS:
                profile = profile_contract.name
                model_group = profile_contract.broker_model
                for _ in range(args.attempts_per_profile):
                    ordinal += 1
                    attempt_record = _started_attempt(
                        ordinal=ordinal,
                        profile=profile,
                        model_group=model_group,
                        allocation_usd=attempt_allocations[ordinal - 1],
                        debug_deny_upstream=args.debug_deny_upstream,
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
                            attempt_allocation=attempt_allocations[ordinal - 1],
                            attempt_record=attempt_record,
                            backend=backend,
                            debug_deny_upstream=args.debug_deny_upstream,
                        )
                        total_cost += Decimal(
                            attempt_record["spend"]["known_actual_cost_usd"]
                        )
                    except BaseException as error:
                        if "failure" not in attempt_record:
                            _mark_attempt_failed(attempt_record, error)
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
                and prior_total + total_cost <= MAX_TOTAL_COST
                and recorded_cost == total_cost
                and actual_within_reservations
            )
            diagnostic_passed = (
                args.debug_deny_upstream
                and len(attempts) == args.attempts_per_profile * len(PROFILE_CONTRACTS)
                and source_unchanged
                and total_cost == 0
                and all(
                    attempt.get("outcome") == "diagnostic_succeeded"
                    for attempt in attempts
                )
            )
            ok = admissible or diagnostic_passed
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
                    "attempt_allocations_usd": [
                        str(item) for item in attempt_allocations
                    ],
                    "available_budget_usd": str(available_budget),
                    "input_and_cache_ceiling_per_million_usd": "10",
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                    "output_ceiling_per_million_usd": "50",
                    "actual_within_reservations": actual_within_reservations,
                    "reservation_safety_margin_usd": str(RESERVATION_SAFETY_MARGIN),
                    "recorded_actual_cost_usd": str(recorded_cost),
                    "prior_known_cost_usd": str(args.prior_known_cost_usd),
                    "prior_unknown_exposure_usd": str(args.prior_unknown_exposure_usd),
                    "current_known_cost_usd": str(total_cost),
                    "current_retained_exposure_usd": "0",
                    "total_cap_exposure_usd": str(prior_total + total_cost),
                    "total_authoritative_cost_usd": str(
                        args.prior_known_cost_usd + total_cost
                    ),
                    "total_cap_usd": str(MAX_TOTAL_COST),
                },
                "cli_distribution": installed_cli.attestation,
                "command": [
                    "python",
                    "tools/run_authority_fencing_calibration.py",
                    *evidence_argv(effective_argv),
                ],
                "debug": args.debug,
                "backend": _backend_evidence(backend),
                "diagnostic": {
                    "admissible": False,
                    "deny_upstream": args.debug_deny_upstream,
                    "passed": diagnostic_passed,
                },
                "ok": ok,
                "profiles": [
                    dataclasses.asdict(profile) for profile in PROFILE_CONTRACTS
                ],
                "pricing": {
                    "backend": pricing.backend,
                    "models": list(pricing.models),
                    "sha256": pricing.sha256,
                    "source": pricing.source,
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
                "spend_exposure": {
                    "prior_known_cost_usd": str(args.prior_known_cost_usd),
                    "prior_unknown_exposure_usd": str(args.prior_unknown_exposure_usd),
                    "current_known_cost_usd": str(total_cost),
                    "current_retained_exposure_usd": "0",
                    "known_actual_cost_usd": str(
                        args.prior_known_cost_usd + total_cost
                    ),
                    "retained_unknown_reservation_usd": str(
                        args.prior_unknown_exposure_usd
                    ),
                    "total_usd": str(prior_total + total_cost),
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
                            if attempt["trajectory"] is not None
                        }
                    ),
                },
            }
            encoded = (canonical(evidence) + "\n").encode()
            if authority is not None and admissible:
                write_exclusive_proof(authority, encoded)
            _emit(evidence)
            return 0 if ok else 1
    except BaseException as error:
        if run_id is not None:
            try:
                sweep_calibration_run(run_id)
            except BaseException as cleanup_error:
                if attempts:
                    attempts[-1]["final_cleanup"] = {
                        "exception_class": type(cleanup_error).__name__,
                        "status": "failed",
                    }
                if not isinstance(error, AttemptFailure):
                    error = AttemptFailure(
                        "resource_cleanup", "final_cleanup_failed", cleanup_error
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
