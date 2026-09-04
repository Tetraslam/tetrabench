from __future__ import annotations

import copy
import dataclasses
import hashlib
import http.client
import json
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from email.message import Message
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import run_authority_fencing_calibration as calibration  # noqa: E402

TEST_PARENT_KEY = "sk-test-deployed-key-1234"
TEST_PROBE_TOKEN = "probe-token-0123456789-abcdefghijklmnopqr"
TEST_ATTEMPT_TOKEN = "attempt-token-0123456789-abcdefghijklmnop"
TEST_OPENROUTER_KEY = "sk-or-v1-test-deployed-key-1234"
PINNED_OPENCODE_ERROR_KINDS = frozenset(
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
PINNED_OPENCODE_EVENT_KINDS = frozenset(
    {"error", "reasoning", "step_finish", "step_start", "text", "tool_use"}
)
PINNED_OPENCODE_TOOL_STATUSES = frozenset({"completed", "error"})


class FakeUpstreamServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    status: int
    body: bytes
    response_headers: list[tuple[str, str]]
    response_gate: threading.Event | None
    body_gate: threading.Event | None
    get_response_gate: threading.Event | None
    get_body_gate: threading.Event | None
    first_body_delay: float
    include_content_length: bool
    close_response: bool
    queued_get_responses: list[tuple[int, list[tuple[str, str]], bytes]]


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        server = self.server
        if not isinstance(server, FakeUpstreamServer):
            raise RuntimeError("fake upstream server type mismatch")
        server.requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body),
                "path": self.path,
            }
        )
        if server.response_gate is not None:
            server.response_gate.wait(timeout=30)
        status = server.status
        response = server.body
        self.send_response(status)
        for name, value in server.response_headers:
            self.send_header(name, value)
        if server.include_content_length:
            self.send_header("Content-Length", str(len(response)))
        if server.close_response:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if server.body_gate is not None:
            server.body_gate.wait(timeout=30)
        if server.first_body_delay:
            time.sleep(server.first_body_delay)
        try:
            if any(
                name.lower() == "transfer-encoding" and value.lower() == "chunked"
                for name, value in server.response_headers
            ):
                self.wfile.write(
                    f"{len(response):x}\r\n".encode() + response + b"\r\n0\r\n\r\n"
                )
            else:
                self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        server = self.server
        if not isinstance(server, FakeUpstreamServer):
            raise RuntimeError("fake upstream server type mismatch")
        server.requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "path": self.path,
            }
        )
        if server.get_response_gate is not None:
            server.get_response_gate.wait(timeout=30)
        if server.queued_get_responses:
            status, headers, response = server.queued_get_responses.pop(0)
        else:
            status, headers, response = (
                server.status,
                server.response_headers,
                server.body,
            )
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if server.include_content_length:
            self.send_header("Content-Length", str(len(response)))
        if server.close_response:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if server.get_body_gate is not None:
            server.get_body_gate.wait(timeout=30)
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def fake_upstream(
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b'{"ok":true}',
    status: int = 200,
    response_gate: threading.Event | None = None,
    body_gate: threading.Event | None = None,
    first_body_delay: float = 0.0,
    include_content_length: bool = True,
    close_response: bool = False,
    queued_get_responses: list[tuple[int, list[tuple[str, str]], bytes]] | None = None,
    get_response_gate: threading.Event | None = None,
    get_body_gate: threading.Event | None = None,
):
    server = FakeUpstreamServer(("127.0.0.1", 0), FakeUpstreamHandler)
    server.requests = []
    server.status = status
    server.body = body
    server.response_headers = (
        headers
        if headers is not None
        else [
            ("Content-Type", "application/json"),
            ("X-Litellm-Response-Cost", "0.125"),
        ]
    )
    server.response_gate = response_gate
    server.body_gate = body_gate
    server.get_response_gate = get_response_gate
    server.get_body_gate = get_body_gate
    server.first_body_delay = first_body_delay
    server.include_content_length = include_content_length
    server.close_response = close_response
    server.queued_get_responses = list(queued_get_responses or [])
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        for gate in (
            response_gate,
            body_gate,
            get_response_gate,
            get_body_gate,
        ):
            if gate is not None:
                gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def responses_sse(
    *,
    cost: object = "0.125",
    input_tokens: object = 2,
    output_tokens: object = 3,
    total_tokens: object = 5,
    status: str = "completed",
    event_type: str = "response.completed",
) -> bytes:
    document = {
        "type": event_type,
        "response": {
            "status": status,
            "usage": {
                "cost": cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
        },
    }
    return f"event: {event_type}\ndata: {json.dumps(document)}\n\n".encode()


def openrouter_responses_sse(
    *,
    response_id: str = "gen-test-1",
    model: str = "openai/gpt-5.6-sol",
    include_cost: bool = True,
    include_event: bool = True,
    include_comment: bool = False,
    event_type: str = "response.completed",
    input_tokens: int = 2,
    output_tokens: int = 3,
) -> bytes:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if include_cost:
        usage["cost"] = "0.00007"
    document = {
        "type": event_type,
        "response": {
            "id": response_id,
            "model": model,
            "status": "completed",
            "usage": usage,
        },
    }
    event = f"event: {event_type}\n" if include_event else ""
    comment = ": OPENROUTER PROCESSING\n\n\n\n" if include_comment else ""
    return f"{comment}{event}data: {json.dumps(document)}\n\ndata: [DONE]\n\n".encode()


def openrouter_chat_sse(
    *,
    response_id: str = "chat-test-1",
    model: str = "z-ai/glm-5.3-flash",
    finish_reason: str = "tool_calls",
    usage_finish_reason: str | None = None,
    choices: int = 1,
) -> bytes:
    base = {
        "created": 1,
        "id": response_id,
        "model": model,
        "object": "chat.completion.chunk",
        "provider": "test",
    }
    documents = [
        {
            **base,
            "choices": [
                {
                    "delta": {"content": "", "role": "assistant"},
                    "finish_reason": None,
                    "index": index,
                    "native_finish_reason": None,
                }
                for index in range(choices)
            ],
        },
        {
            **base,
            "choices": [
                {
                    "delta": {"content": "", "role": "assistant"},
                    "finish_reason": finish_reason,
                    "index": 0,
                    "native_finish_reason": finish_reason,
                }
            ],
        },
        {
            **base,
            "choices": [
                {
                    "delta": {"content": "", "role": "assistant"},
                    "finish_reason": usage_finish_reason or finish_reason,
                    "index": 0,
                    "native_finish_reason": usage_finish_reason or finish_reason,
                }
            ],
            "usage": {
                "completion_tokens": 3,
                "cost": "0.00007",
                "prompt_tokens": 2,
                "total_tokens": 5,
            },
        },
    ]
    frames = "".join(f"data: {json.dumps(document)}\n\n" for document in documents)
    return f"{frames}data: [DONE]\n\n".encode()


def openrouter_generation(
    *,
    response_id: str = "gen-test-1",
    upstream_id: str | None = None,
    model: Any = "openai/gpt-5.6-sol",
    streamed: bool = True,
    cost: str = "0.00007",
    input_tokens: int = 2,
    output_tokens: int = 3,
    native_input_tokens: int | None = None,
    native_output_tokens: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "data": {
                "id": response_id,
                "upstream_id": upstream_id or response_id,
                "model": model,
                "streamed": streamed,
                "total_cost": cost,
                "usage": cost,
                "tokens_prompt": input_tokens,
                "tokens_completion": output_tokens,
                "native_tokens_prompt": (
                    input_tokens if native_input_tokens is None else native_input_tokens
                ),
                "native_tokens_completion": (
                    output_tokens
                    if native_output_tokens is None
                    else native_output_tokens
                ),
            }
        }
    ).encode()


@contextmanager
def running_broker(
    upstream: FakeUpstreamServer,
    *,
    timeout: float = 30,
    ledger: calibration.SpendLedger | None = None,
    max_input_tokens: int = calibration.MAX_BODY_BYTES,
    debug_deny_upstream: bool = False,
    backend: calibration.BackendContract = calibration.LITELLM_BACKEND,
    model: str = "openai/gpt-5.6-sol",
    canonical_model: str | None = None,
):
    broker = calibration.CalibrationBroker(
        host="127.0.0.1",
        port=0,
        parent_key=TEST_PARENT_KEY,
        model=model,
        canonical_model=canonical_model,
        ledger=ledger or calibration.SpendLedger(),
        backend=backend,
        max_input_tokens=max_input_tokens,
        timeout=timeout,
        upstream_url=(
            f"http://127.0.0.1:{upstream.server_address[1]}/api/v1"
            if backend.name == "openrouter"
            else f"http://127.0.0.1:{upstream.server_address[1]}"
        ),
        debug_deny_upstream=debug_deny_upstream,
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.stop()


@contextmanager
def running_probe_broker(probe_token: str = TEST_PROBE_TOKEN):
    broker = calibration.CalibrationBroker(
        host="127.0.0.1",
        port=0,
        parent_key="",
        model="probe-only",
        ledger=calibration.SpendLedger(),
        upstream_url="http://127.0.0.1:1",
        probe_token=probe_token,
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.stop()


def request(
    broker: calibration.CalibrationBroker,
    *,
    token: str | None = None,
    method: str = "POST",
    path: str = "/v1/responses",
    document: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(
        document or {"model": "openai/gpt-5.6-sol", "input": "redacted"}
    ).encode()
    connection = http.client.HTTPConnection("127.0.0.1", broker.port, timeout=5)
    connection.request(
        method,
        path,
        body=body,
        headers={
            "Authorization": f"Bearer {token or broker.token}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    response = connection.getresponse()
    response_body = response.read()
    result = (
        response.status,
        {key.lower(): value for key, value in response.headers.items()},
        response_body,
    )
    connection.close()
    return result


def probe_request(
    broker: calibration.CalibrationBroker, authorization: str | None
) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", broker.port, timeout=5)
    headers = {} if authorization is None else {"Authorization": authorization}
    connection.request("GET", "/__tetrabench_probe", headers=headers)
    response = connection.getresponse()
    response.read()
    status = response.status
    connection.close()
    return status


def probe_headers(token: str) -> Message:
    headers = Message()
    headers["Authorization"] = f"Bearer {token}"
    return headers


def raw_request(port: int, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as stream:
        stream.sendall(data)
        stream.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := stream.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def pricing_document() -> dict[str, Any]:
    return {
        "data": [
            {
                "model_name": model,
                "litellm_params": {"api_key": "masked-secret"},
                "model_info": {
                    "input_cost_per_token": 0.000003,
                    "output_cost_per_token": 0.000015,
                    "cache_read_input_token_cost": 0.0000003,
                    "cache_creation_input_token_cost": 0.00000375,
                    "max_input_tokens": 200000,
                    "max_output_tokens": 65536,
                },
            }
            for _profile, model in calibration.PROFILES
        ]
        + [{"model_name": "unrelated", "model_info": {"api_key": "secret"}}]
    }


def openrouter_pricing_document() -> dict[str, Any]:
    base: dict[str, Any] = {
        "prompt": "0.000002",
        "completion": "0.00001",
        "input_cache_read": "0.0000002",
        "input_cache_write": "0.0000025",
        "input_cache_write_1h": "0.000004",
        "request": "0",
        "image": "0",
        "audio": "0",
    }
    target: dict[str, Any] = dict(base)
    target["overrides"] = [
        {
            "min_prompt_tokens": 100000,
            "prompt": "0.000004",
            "completion": "0.000015",
            "input_cache_read": "0.0000004",
            "input_cache_write": "0.000005",
            "request": "0",
        },
        {
            "utc_start": 1630,
            "utc_end": 30,
            "utc_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
            "input_cache_write": "0.0000045",
        },
    ]
    alternate = {
        **base,
        "prompt": "0.000000075",
        "completion": "0.00000025",
        "input_cache_read": "0.000000015",
    }
    alternate.pop("input_cache_write")
    alternate.pop("input_cache_write_1h")
    return {
        "data": [
            {
                "id": "openai/gpt-5.6-sol",
                "canonical_slug": "openai/gpt-5.6-sol-20260709",
                "context_length": 400000,
                "top_provider": {"max_completion_tokens": 128000},
                "pricing": target,
            },
            {
                "id": "z-ai/glm-5.3-flash",
                "canonical_slug": "z-ai/glm-5.3-flash-20260826",
                "context_length": 1310720,
                "top_provider": {"max_completion_tokens": 131072},
                "pricing": alternate,
            },
            {"id": "unrelated", "pricing": {"prompt": "99"}},
        ]
    }


@pytest.mark.parametrize(
    "parent_key",
    [
        "sk-123456789012",
        "deployed-key-without-prefix",
        "sk-test deployed-key",
        "sk-test\tdeployed-key",
        "sk-test\ndeployed-key",
        "sk-test\0deployed-key",
        "sk-test\x7fdeployed-key",
        "sk-" + "a" * 510,
    ],
)
def test_broker_credential_payload_rejects_invalid_parent_key(
    parent_key: str,
) -> None:
    with pytest.raises(ValueError, match="credential payload rejected"):
        calibration._validate_broker_credential_payload(
            {"parent_key": parent_key, "probe_token": TEST_PROBE_TOKEN}
        )


@pytest.mark.parametrize(
    "token",
    ["a" * 31, "a" * 513, "a" * 31 + "=", "a" * 31 + " "],
)
def test_broker_credential_payload_rejects_weak_or_unbounded_probe_token(
    token: str,
) -> None:
    with pytest.raises(ValueError, match="credential payload rejected"):
        calibration._validate_broker_credential_payload(
            {"parent_key": TEST_PARENT_KEY, "probe_token": token}
        )


@pytest.mark.parametrize(
    "token",
    ["a" * 31, "a" * 513, "a" * 31 + "=", "a" * 31 + "\n"],
)
def test_broker_activation_rejects_weak_or_unbounded_attempt_token(
    token: str,
) -> None:
    state = calibration.BrokerState(
        parent_key=TEST_PARENT_KEY,
        token=None,
        probe_token=None,
        model="model",
        max_input_tokens=100,
        deadline=time.monotonic() + 30,
        ledger=calibration.SpendLedger(),
    )
    with pytest.raises(ValueError, match="activation token rejected"):
        state.activate(token, (1, 2))


def test_profiles_are_exact_ordered_and_candidate_only(tmp_path: Path) -> None:
    project, config_root = calibration._write_project(tmp_path, calibration.TASK)
    config = (config_root / "tetrabench/config.toml").read_text()
    assert calibration.PROFILES == (
        ("target", "openai/gpt-5.6-sol"),
        ("alternate", "z-ai/glm-5.3-flash"),
    )
    assert config.count("[profiles.") == 8
    assert 'model_name = "openai/openai/gpt-5.6-sol"' in config
    assert 'model_name = "zai/z-ai/glm-5.3-flash"' in config
    assert config.count('agent_name = "opencode"') == 2
    assert config.count("attempts = 1") == 2
    assert config.count("concurrency = 1") == 2
    catalog = (project / "benchmarks/catalog.toml").read_text()
    assert catalog.count('id = "authority-fencing"') == 1
    assert 'reward_policy = "binary"' in catalog


def test_glm_profile_produces_content_free_openai_compatible_config() -> None:
    profile = calibration.PROFILE_CONTRACTS[1]
    content = calibration._opencode_provider_config_content(
        profile=profile,
        base_url="http://broker:62017/v1",
        max_input_tokens=1_245_184,
        max_output_tokens=131_072,
    )
    assert content is not None
    document = json.loads(content)
    provider = document["provider"]["zai"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "apiKey": "{env:ZAI_API_KEY}",
        "baseURL": "http://broker:62017/v1",
        "headerTimeout": 300_000,
    }
    assert provider["models"]["z-ai/glm-5.3-flash"]["limit"] == {
        "context": 1_245_184,
        "output": calibration.MAX_OUTPUT_TOKENS,
    }
    assert document["model"] == "zai/z-ai/glm-5.3-flash"
    assert document["small_model"] == "zai/z-ai/glm-5.3-flash"
    assert "secret" not in json.dumps(document).lower()


def test_glm_config_is_injected_into_harbor_main_environment(tmp_path: Path) -> None:
    content = calibration._opencode_provider_config_content(
        profile=calibration.PROFILE_CONTRACTS[1],
        base_url="http://broker:62017/v1",
        max_input_tokens=1_245_184,
        max_output_tokens=131_072,
    )
    assert content is not None
    overlay = calibration.create_task_overlay(
        calibration.TASK,
        tmp_path / "overlay",
        "tb-cal-safe-1",
        opencode_config_content=content,
    )
    compose = (overlay.task / "environment/docker-compose.yaml").read_text()
    assert "    environment:\n" in compose
    assert f"      OPENCODE_CONFIG_CONTENT: {json.dumps(content)}\n" in compose
    assert TEST_OPENROUTER_KEY not in content


def test_backend_and_profile_contracts_are_immutable_and_separate() -> None:
    args = calibration.parse_arguments(["--debug"])
    assert args.backend == "openrouter"
    assert calibration.OPENROUTER_BACKEND.endpoint_paths == (
        ("/v1/responses", "/responses"),
        ("/v1/chat/completions", "/chat/completions"),
    )
    assert calibration.LITELLM_BACKEND.endpoint_paths[0][1] == "/v1/responses"
    target = calibration.PROFILE_CONTRACTS[0]
    assert target.child_model == target.broker_model == target.upstream_model
    assert target.harbor_model == "openai/openai/gpt-5.6-sol"
    assert calibration.ProfileContract.__dataclass_params__.frozen is True


def test_backend_credential_selection_is_isolated_and_fallback_unambiguous() -> None:
    environment = {
        calibration.OPENROUTER_KEY_ENV: TEST_OPENROUTER_KEY,
        calibration.LITELLM_KEY_ENV: TEST_PARENT_KEY,
        calibration.LEGACY_LITELLM_KEY_ENV: "sk-legacy-deployed-key",
    }
    assert (
        calibration._take_backend_credential(
            calibration.OPENROUTER_BACKEND, environment
        )
        == TEST_OPENROUTER_KEY
    )
    assert calibration.LITELLM_KEY_ENV in environment
    assert calibration.LEGACY_LITELLM_KEY_ENV in environment
    with pytest.raises(RuntimeError, match="ambiguous"):
        calibration._take_backend_credential(calibration.LITELLM_BACKEND, environment)
    fallback = {calibration.LEGACY_LITELLM_KEY_ENV: TEST_PARENT_KEY}
    assert (
        calibration._take_backend_credential(calibration.LITELLM_BACKEND, fallback)
        == TEST_PARENT_KEY
    )
    assert fallback == {}


def test_bearer_validation_is_general_and_backend_prefixes_are_specific() -> None:
    assert calibration._validate_bearer("provider.key+/=printable")
    assert (
        calibration._validate_parent_key(
            TEST_OPENROUTER_KEY, calibration.OPENROUTER_BACKEND
        )
        == TEST_OPENROUTER_KEY
    )
    with pytest.raises(ValueError, match="credential payload rejected"):
        calibration._validate_parent_key(
            TEST_PARENT_KEY, calibration.OPENROUTER_BACKEND
        )
    with pytest.raises(ValueError, match="credential payload rejected"):
        calibration._validate_bearer("provider key")


def test_authenticated_model_info_selects_exact_groups_and_redacts_snapshot() -> None:
    document = pricing_document()
    with fake_upstream(
        headers=[("Content-Type", "application/json")],
        body=json.dumps(document).encode(),
    ) as upstream:
        snapshot = calibration.query_model_pricing(
            parent_key=TEST_PARENT_KEY,
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
    assert upstream.requests == [
        {"authorization": f"Bearer {TEST_PARENT_KEY}", "path": "/model/info"}
    ]
    assert [item["model_group"] for item in snapshot.models] == [
        model for _profile, model in calibration.PROFILES
    ]
    serialized = json.dumps(dataclasses.asdict(snapshot))
    assert "secret" not in serialized
    assert len(snapshot.sha256) == 64


def test_openrouter_models_pricing_preserves_base_path_and_takes_override_maxima() -> (
    None
):
    with fake_upstream(
        headers=[("Content-Type", "application/json")],
        body=json.dumps(openrouter_pricing_document()).encode(),
    ) as upstream:
        snapshot = calibration.query_model_pricing(
            parent_key=TEST_OPENROUTER_KEY,
            backend=calibration.OPENROUTER_BACKEND,
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}/api/v1",
        )
    assert upstream.requests == [
        {
            "authorization": f"Bearer {TEST_OPENROUTER_KEY}",
            "path": "/api/v1/models",
        }
    ]
    assert snapshot.backend == "openrouter"
    assert snapshot.source == "/models"
    assert snapshot.models[0] == {
        "backend": "openrouter",
        "cache_creation_input_token_cost": "0.000005",
        "cache_read_input_token_cost": "4E-7",
        "canonical_model": "openai/gpt-5.6-sol-20260709",
        "input_cost_per_token": "0.000004",
        "max_input_tokens": 400000 - calibration.MAX_OUTPUT_TOKENS,
        "max_output_tokens": 128000,
        "model_group": "openai/gpt-5.6-sol",
        "output_cost_per_token": "0.000015",
        "pricing_source": "/models",
    }
    assert snapshot.models[1]["cache_creation_input_token_cost"] == "7.5E-8"
    assert snapshot.models[1]["canonical_model"] == "z-ai/glm-5.3-flash-20260826"


@pytest.mark.parametrize("model_index", [0, 1])
@pytest.mark.parametrize(
    "canonical_slug",
    [None, "", "contains whitespace", 1],
)
def test_openrouter_models_rejects_missing_or_malformed_canonical_slug(
    model_index: int, canonical_slug: Any
) -> None:
    document = openrouter_pricing_document()
    document["data"][model_index]["canonical_slug"] = canonical_slug
    with pytest.raises(ValueError, match="identifier is malformed"):
        calibration._validate_openrouter_pricing_document(document)


@pytest.mark.parametrize("model_index", [0, 1])
def test_openrouter_models_rejects_mismatched_canonical_slug(model_index: int) -> None:
    document = openrouter_pricing_document()
    document["data"][model_index]["canonical_slug"] = "other/provider-model-20260101"
    with pytest.raises(ValueError, match="canonical model identity mismatch"):
        calibration._validate_openrouter_pricing_document(document)


@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai/api/v1?admin=true",
        "https://user:secret@openrouter.ai/api/v1",
        "https://openrouter.ai/api/../v1",
        "ftp://openrouter.ai/api/v1",
    ],
)
def test_backend_upstream_url_rejects_authority_and_path_ambiguity(url: str) -> None:
    with pytest.raises(ValueError, match="upstream URL rejected"):
        calibration._parse_upstream_url(url)


def test_safe_upstream_join_preserves_base_for_all_openrouter_paths() -> None:
    base = calibration._parse_upstream_url("https://openrouter.ai/api/v1").path
    assert calibration._join_upstream_path(base, "/responses") == "/api/v1/responses"
    assert calibration._join_upstream_path(base, "/models") == "/api/v1/models"
    assert calibration._join_upstream_path(base, "/generation") == "/api/v1/generation"


@pytest.mark.parametrize("field", ["request", "internal_reasoning"])
def test_openrouter_models_rejects_unsupported_nonzero_paid_pricing(field: str) -> None:
    document = openrouter_pricing_document()
    document["data"][0]["pricing"][field] = "0.000001"
    with pytest.raises(ValueError, match="unsupported paid pricing"):
        calibration._validate_openrouter_pricing_document(document)


@pytest.mark.parametrize(
    "field",
    ["audio", "image", "input_audio", "output_audio", "web_search"],
)
def test_openrouter_models_accepts_paid_surfaces_rejected_by_broker(field: str) -> None:
    document = openrouter_pricing_document()
    document["data"][0]["pricing"][field] = "0.01"
    snapshot = calibration._validate_openrouter_pricing_document(document)
    assert snapshot.models[0]["model_group"] == "openai/gpt-5.6-sol"


def test_openrouter_models_rejects_unknown_paid_pricing_key() -> None:
    document = openrouter_pricing_document()
    document["data"][0]["pricing"]["new_paid_surface"] = "0.000001"
    with pytest.raises(ValueError, match="unknown pricing field"):
        calibration._validate_openrouter_pricing_document(document)


def test_openrouter_models_rejects_unknown_override_condition() -> None:
    document = openrouter_pricing_document()
    document["data"][0]["pricing"]["overrides"][0]["region"] = "US"
    with pytest.raises(ValueError, match="condition is unknown"):
        calibration._validate_openrouter_pricing_document(document)


@pytest.mark.parametrize("field", ["utc_start", "utc_end"])
@pytest.mark.parametrize("value", [0, 30, 59, 100, 1630, 2359])
def test_openrouter_override_accepts_documented_hhmm_boundaries(
    field: str, value: int
) -> None:
    calibration._validate_openrouter_override_condition(field, value)


@pytest.mark.parametrize("field", ["utc_start", "utc_end"])
@pytest.mark.parametrize(
    "value",
    [-1, 60, 99, 1260, 2360, 2400, True, 16.5, "1630", None],
)
def test_openrouter_override_rejects_malformed_hhmm(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match="condition is malformed"):
        calibration._validate_openrouter_override_condition(field, value)


def test_openrouter_override_accepts_documented_utc_weekday_spellings() -> None:
    calibration._validate_openrouter_override_condition(
        "utc_days",
        [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ],
    )


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["Monday"],
        ["mon"],
        ["monday", "monday"],
        ["monday", 1],
        [0, 1, 2, 3, 4, 5, 6],
        "monday",
        None,
    ],
)
def test_openrouter_override_rejects_malformed_utc_days(value: Any) -> None:
    with pytest.raises(ValueError, match="condition is malformed"):
        calibration._validate_openrouter_override_condition("utc_days", value)


@pytest.mark.parametrize(
    "update",
    [
        {"utc_start": 1630},
        {"utc_end": 30},
        {"utc_start": 30, "utc_end": 30},
    ],
)
def test_openrouter_models_rejects_incomplete_or_empty_utc_windows(
    update: dict[str, int],
) -> None:
    document = openrouter_pricing_document()
    override = document["data"][0]["pricing"]["overrides"][1]
    override.pop("utc_start")
    override.pop("utc_end")
    override.update(update)
    with pytest.raises(ValueError, match="condition is malformed"):
        calibration._validate_openrouter_pricing_document(document)


def test_debug_deny_upstream_prices_then_records_request_without_completion() -> None:
    document = pricing_document()
    with fake_upstream(
        headers=[("Content-Type", "application/json")],
        body=json.dumps(document).encode(),
    ) as upstream:
        pricing = calibration.query_model_pricing(
            parent_key=TEST_PARENT_KEY,
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
        assert pricing.models
        with running_broker(upstream, debug_deny_upstream=True) as broker:
            status, _headers, _body = request(broker)
            assert status == 503
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.locked_endpoint == "/v1/responses"
            assert broker.state.request_count == 1
            assert broker.state.ledger.cost == 0
            assert broker.state.ledger.reserved == 0
            record = broker.state.records[0]
            assert record.status == 503
            assert record.settlement == "released_unforwarded"
            assert record.upstream_opened is False
            assert record.parent_authorization_sent is False
    assert upstream.requests == [
        {"authorization": f"Bearer {TEST_PARENT_KEY}", "path": "/model/info"}
    ]


def test_model_info_accepts_large_document_and_discards_irrelevant_rows() -> None:
    document = pricing_document()
    document["data"].append(
        {
            "model_name": "irrelevant-large-row",
            "bounded_padding": "x" * ((2 << 20) + 1),
        }
    )
    body = json.dumps(document).encode()
    assert 2 << 20 < len(body) <= calibration.MODEL_INFO_MAX_RESPONSE_BYTES
    with fake_upstream(body=body) as upstream:
        snapshot = calibration.query_model_pricing(
            parent_key=TEST_PARENT_KEY,
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
    assert len(snapshot.models) == 2
    assert [item["model_group"] for item in snapshot.models] == [
        model for _profile, model in calibration.PROFILES
    ]
    assert all(
        set(item)
        == {
            "cache_creation_input_token_cost",
            "cache_read_input_token_cost",
            "input_cost_per_token",
            "max_input_tokens",
            "max_output_tokens",
            "model_group",
            "output_cost_per_token",
        }
        for item in snapshot.models
    )


@pytest.mark.parametrize("include_content_length", [True, False])
def test_model_info_rejects_response_above_four_mib(
    include_content_length: bool,
) -> None:
    body = b"x" * (calibration.MODEL_INFO_MAX_RESPONSE_BYTES + 1)
    with fake_upstream(
        body=body,
        include_content_length=include_content_length,
        close_response=not include_content_length,
    ) as upstream:
        with pytest.raises(ValueError, match="exceeds limit"):
            calibration.query_model_pricing(
                parent_key=TEST_PARENT_KEY,
                upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
            )


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", "2")],
        [("Content-Length", "malformed")],
        [
            (
                "Content-Length",
                str(calibration.MODEL_INFO_MAX_RESPONSE_BYTES + 1),
            )
        ],
        [("Content-Length", "2"), ("Transfer-Encoding", "chunked")],
    ],
)
def test_model_info_rejects_ambiguous_malformed_or_oversized_content_length(
    headers: list[tuple[str, str]],
) -> None:
    include_content_length = headers == [("Content-Length", "2")]
    with fake_upstream(
        headers=headers,
        body=b"{}",
        include_content_length=include_content_length,
        close_response=True,
    ) as upstream:
        with pytest.raises(ValueError, match=r"framing|exceeds limit"):
            calibration.query_model_pricing(
                parent_key=TEST_PARENT_KEY,
                upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
            )


def test_model_info_rejects_truncated_or_malformed_document() -> None:
    with fake_upstream(
        headers=[("Content-Length", "3")],
        body=b"{}",
        include_content_length=False,
        close_response=True,
    ) as upstream:
        with pytest.raises(ValueError, match="truncated"):
            calibration.query_model_pricing(
                parent_key=TEST_PARENT_KEY,
                upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
            )
    with fake_upstream(body=b"not-json") as upstream:
        with pytest.raises(ValueError, match="invalid JSON"):
            calibration.query_model_pricing(
                parent_key=TEST_PARENT_KEY,
                upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_cost_per_token", 0),
        ("input_cost_per_token", float("inf")),
        ("input_cost_per_token", 0.0000101),
        ("output_cost_per_token", 0.0000501),
        ("cache_read_input_token_cost", None),
        ("cache_creation_input_token_cost", -1),
        ("max_input_tokens", 0),
        ("max_output_tokens", 65535),
    ],
)
def test_model_info_rejects_bad_rates_ceilings_and_limits(
    field: str, value: Any
) -> None:
    document = pricing_document()
    document["data"][0]["model_info"][field] = value
    with pytest.raises(ValueError):
        calibration._validate_pricing_document(document)


def test_model_info_rejects_missing_or_duplicate_required_group() -> None:
    document = pricing_document()
    document["data"] = document["data"][:-2]
    with pytest.raises(ValueError, match="omitted"):
        calibration._validate_pricing_document(document)
    document = pricing_document()
    document["data"].append(document["data"][0])
    with pytest.raises(ValueError, match="ambiguous"):
        calibration._validate_pricing_document(document)


def test_broker_pricing_binding_requires_exact_host_snapshot_and_model() -> None:
    snapshot = calibration._validate_pricing_document(pricing_document())
    models = list(snapshot.models)
    calibration._validate_broker_pricing_binding(
        models, snapshot.sha256, "openai/gpt-5.6-sol", 200000
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        calibration._validate_broker_pricing_binding(
            models, "0" * 64, "openai/gpt-5.6-sol", 200000
        )
    with pytest.raises(ValueError, match="model binding mismatch"):
        calibration._validate_broker_pricing_binding(
            models, snapshot.sha256, "openai/gpt-5.6-sol", 199999
        )


def test_openrouter_broker_binding_requires_signed_canonical_model() -> None:
    snapshot = calibration._validate_openrouter_pricing_document(
        openrouter_pricing_document()
    )
    models = [dict(item) for item in snapshot.models]
    selected = calibration._validate_broker_pricing_binding(
        models,
        snapshot.sha256,
        "openai/gpt-5.6-sol",
        400000 - calibration.MAX_OUTPUT_TOKENS,
        calibration.OPENROUTER_BACKEND,
    )
    assert selected["canonical_model"] == "openai/gpt-5.6-sol-20260709"
    assert selected["max_input_tokens"] == 400000 - calibration.MAX_OUTPUT_TOKENS

    models[0].pop("canonical_model")
    digest = hashlib.sha256(calibration.canonical(models).encode()).hexdigest()
    with pytest.raises(ValueError, match="identifier is malformed"):
        calibration._validate_broker_pricing_binding(
            models,
            digest,
            "openai/gpt-5.6-sol",
            400000 - calibration.MAX_OUTPUT_TOKENS,
            calibration.OPENROUTER_BACKEND,
        )


def test_openrouter_context_limit_reserves_complete_output_headroom() -> None:
    document = openrouter_pricing_document()
    document["data"][0]["context_length"] = calibration.MAX_OUTPUT_TOKENS
    with pytest.raises(ValueError, match="context limit cannot cover"):
        calibration._validate_openrouter_pricing_document(document)


def test_atif_token_metrics_are_mandatory_coherent_and_nonzero() -> None:
    record = {
        "trajectory": {
            "step_count": 2,
            "final_metrics": {
                "total_prompt_tokens": 12,
                "total_completion_tokens": 3,
                "total_cached_tokens": 4,
                "total_steps": 2,
                "total_cost_usd": 0.01,
            },
        }
    }
    assert calibration._validate_metrics(record)["total_tokens"] == 15
    no_cache = json.loads(json.dumps(record))
    no_cache["trajectory"]["final_metrics"]["total_cached_tokens"] = None
    assert calibration._validate_metrics(no_cache)["total_cached_tokens"] == 0
    for field in ("total_prompt_tokens", "total_completion_tokens"):
        invalid = json.loads(json.dumps(record))
        invalid["trajectory"]["final_metrics"][field] = None
        with pytest.raises(ValueError, match="ATIF metrics"):
            calibration._validate_metrics(invalid)
    invalid = json.loads(json.dumps(record))
    invalid["trajectory"]["final_metrics"]["total_cached_tokens"] = "0"
    with pytest.raises(ValueError, match="ATIF metrics"):
        calibration._validate_metrics(invalid)
    invalid = json.loads(json.dumps(record))
    invalid["trajectory"]["final_metrics"].update(
        total_prompt_tokens=0, total_completion_tokens=0
    )
    with pytest.raises(ValueError, match="incoherent or empty"):
        calibration._validate_metrics(invalid)
    invalid = json.loads(json.dumps(record))
    invalid["trajectory"]["final_metrics"]["total_completion_tokens"] = 0
    with pytest.raises(ValueError, match="incoherent or empty"):
        calibration._validate_metrics(invalid)
    invalid = json.loads(json.dumps(record))
    invalid["trajectory"]["final_metrics"]["total_cached_tokens"] = 13
    with pytest.raises(ValueError, match="incoherent or empty"):
        calibration._validate_metrics(invalid)


@pytest.mark.parametrize(
    ("profile", "model"),
    [
        ("target", "openai/openai/gpt-5.6-sol"),
        ("alternate", "zai/z-ai/glm-5.3-flash"),
    ],
)
def test_production_cli_compiles_each_calibration_profile_without_model_access(
    tmp_path: Path, profile: str, model: str
) -> None:
    project, config_root = calibration._write_project(tmp_path, calibration.TASK)
    environment = calibration.child_environment(
        config_root=config_root,
        private_home=tmp_path / "home",
        token="ephemeral-not-used-by-plan",
        base_url="http://127.0.0.1:1/v1",
        ambient={},
    )
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/tetrabench"),
            "plan",
            "systems-design",
            "--profile",
            profile,
            "--json",
        ],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        timeout=30,
    )
    document = json.loads(result.stdout)
    assert result.stderr == b""
    assert document["harbor"] == {
        "agent_name": "opencode",
        "attempts": 1,
        "concurrency": 1,
        "model_name": model,
    }
    assert document["execution"] == {"kind": "docker"}
    assert document["controller"] == {"kind": "local"}
    assert len(document["trials"]) == 1


def test_child_environment_is_clean_and_contains_only_ephemeral_provider_access(
    tmp_path: Path,
) -> None:
    ambient = {
        calibration.PARENT_KEY_ENV: TEST_PARENT_KEY,
        "OPENAI_API_KEY": "broad",
        "ANTHROPIC_API_KEY": "provider",
        "AWS_SECRET_ACCESS_KEY": "storage",
        "GH_TOKEN": "github",
        "DOCKER_HOST": "unix:///run/docker.sock",
        "LANG": "C.UTF-8",
    }
    environment = calibration.child_environment(
        config_root=tmp_path / "config",
        private_home=tmp_path / "home",
        token="ephemeral",
        base_url="http://127.0.0.1:1234/v1",
        ambient=ambient,
    )
    assert environment["OPENAI_API_KEY"] == "ephemeral"
    assert environment["OPENAI_BASE_URL"].endswith("/v1")
    assert environment["DOCKER_HOST"] == ambient["DOCKER_HOST"]
    serialized = json.dumps(environment)
    for secret in ("parent", "broad", "provider", "storage", "github"):
        assert secret not in serialized
    assert calibration.PARENT_KEY_ENV not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "GH_TOKEN" not in environment


def test_broker_forwards_exact_model_caps_output_and_strips_sensitive_headers() -> None:
    headers = [
        ("Content-Type", "application/json"),
        ("X-Litellm-Response-Cost", "0.125"),
        ("X-Litellm-Prompt-Tokens", "12"),
        ("X-Litellm-Completion-Tokens", "3"),
        ("X-Litellm-Call-Id", "call-1"),
        ("X-Litellm-Request-Id", "request-1"),
        ("Set-Cookie", "provider-secret=1"),
        ("Authorization", "Bearer upstream-secret"),
    ]
    with fake_upstream(headers=headers) as upstream:
        with running_broker(upstream) as broker:
            status, returned_headers, body = request(broker)
            assert status == 200
            assert body == b'{"ok":true}'
            assert "x-litellm-response-cost" not in returned_headers
            assert "x-litellm-call-id" not in returned_headers
            assert "set-cookie" not in returned_headers
            assert "authorization" not in returned_headers
            observed = upstream.requests[0]
            assert observed["authorization"] == f"Bearer {TEST_PARENT_KEY}"
            assert observed["path"] == "/v1/responses"
            assert observed["body"]["model"] == "openai/gpt-5.6-sol"
            assert observed["body"]["max_output_tokens"] == 65536
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            record = broker.state.records[0]
            assert record.cost == "0.125"
            forwarded = calibration.canonical(observed["body"]).encode()
            assert record.worst_case_reservation_usd == str(
                calibration._reservation_for_body(forwarded)
            )
            assert record.usage == {"input_tokens": 12, "output_tokens": 3}
            assert record.call_id == "call-1"
            assert record.request_id == "request-1"
            assert record.settlement == "settled"
            assert record.settlement_failure is None
            assert record.retained_unknown_reservation_usd == "0"
            deadline = time.monotonic() + 2
            while broker.state.ledger.cost == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.ledger.cost == Decimal("0.125")


@pytest.mark.parametrize(
    ("path", "document", "field"),
    [
        ("/v1/responses", {"model": "openai/gpt-5.6-sol"}, "max_output_tokens"),
        (
            "/v1/responses",
            {"model": "openai/gpt-5.6-sol", "max_output_tokens": 99999},
            "max_output_tokens",
        ),
        (
            "/v1/chat/completions",
            {"model": "openai/gpt-5.6-sol"},
            "max_completion_tokens",
        ),
        (
            "/v1/chat/completions",
            {"model": "openai/gpt-5.6-sol", "max_tokens": 99999},
            "max_tokens",
        ),
        (
            "/v1/chat/completions",
            {"model": "openai/gpt-5.6-sol", "max_completion_tokens": 99999},
            "max_completion_tokens",
        ),
    ],
)
def test_endpoint_specific_output_limit_is_injected_or_capped(
    path: str, document: dict[str, Any], field: str
) -> None:
    if path == "/v1/responses":
        document["input"] = "redacted"
    else:
        document["messages"] = [{"role": "user", "content": "redacted"}]
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            status, _headers, _body = request(broker, path=path, document=document)
            assert status == 200
            assert upstream.requests[0]["body"][field] == 65536


@pytest.mark.parametrize(
    ("path", "document"),
    [
        (
            "/v1/responses",
            {"model": "openai/gpt-5.6-sol", "max_completion_tokens": 1},
        ),
        (
            "/v1/responses",
            {"model": "openai/gpt-5.6-sol", "max_tokens": 1},
        ),
        (
            "/v1/responses",
            {"model": "openai/gpt-5.6-sol", "maxOutputTokens": 1},
        ),
        (
            "/v1/chat/completions",
            {"model": "openai/gpt-5.6-sol", "max_output_tokens": 1},
        ),
        (
            "/v1/chat/completions",
            {
                "model": "openai/gpt-5.6-sol",
                "max_tokens": 1,
                "max_completion_tokens": 1,
            },
        ),
        (
            "/v1/chat/completions",
            {"model": "openai/gpt-5.6-sol", "maximum_tokens": 1},
        ),
        (
            "/v1/chat/completions",
            {"model": "openai/gpt-5.6-sol", "max_tokens": False},
        ),
    ],
)
def test_endpoint_specific_output_limit_bypasses_are_rejected(
    path: str, document: dict[str, Any]
) -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            status, _headers, _body = request(broker, path=path, document=document)
            assert status == 400
            assert upstream.requests == []


def test_first_valid_endpoint_locks_attempt_and_rejects_switching() -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert request(broker, path="/v1/responses")[0] == 200
            assert request(broker, path="/v1/chat/completions")[0] == 400
            assert broker.state.locked_endpoint == "/v1/responses"
            assert [item["path"] for item in upstream.requests] == ["/v1/responses"]


def test_nonstream_content_type_is_derived_from_validated_request() -> None:
    headers = [
        ("Content-Type", "text/upstream-controlled"),
        ("Content-Encoding", "upstream-secret"),
        ("X-Litellm-Response-Cost", "0.125"),
    ]
    with fake_upstream(headers=headers) as upstream:
        with running_broker(upstream) as broker:
            status, returned_headers, _body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": False,
                },
            )
    assert status == 200
    assert returned_headers["content-type"] == "application/json"
    assert "content-encoding" not in returned_headers
    assert returned_headers["content-length"] == str(len(b'{"ok":true}'))


def test_valid_responses_stream_settles_terminal_cost_and_forwards_exact_bytes() -> (
    None
):
    body = responses_sse()
    content_type = "text/event-stream; charset=utf-8"
    with fake_upstream(headers=[("Content-Type", content_type)], body=body) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, returned_headers, returned_body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": True,
                },
            )
            assert status == 200
            assert returned_headers["content-type"] == content_type
            assert returned_body == body
            assert ledger.cost == Decimal("0.125")
            assert ledger.reserved == 0
            assert ledger.fatal is None
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records[0].usage == {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            }


def test_valid_chunked_responses_stream_settles_terminal_cost() -> None:
    body = responses_sse()
    with fake_upstream(
        headers=[
            ("Content-Type", "text/event-stream"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=body,
        include_content_length=False,
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, _headers, returned_body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": True,
                },
            )
            assert status == 200
            assert returned_body == body
            assert ledger.cost == Decimal("0.125")
            assert ledger.reserved == 0


@pytest.mark.parametrize("event_type", ["response.completed", "response.done"])
def test_openrouter_responses_stream_settles_only_after_generation_404_retry(
    event_type: str,
) -> None:
    body = openrouter_responses_sse(event_type=event_type)
    generation = openrouter_generation()
    queued: list[tuple[int, list[tuple[str, str]], bytes]] = [
        (int(HTTPStatus.NOT_FOUND), [("Content-Type", "application/json")], b"{}"),
        (int(HTTPStatus.OK), [("Content-Type", "application/json")], generation),
    ]
    with fake_upstream(
        headers=[
            ("Content-Type", "text/event-stream"),
            ("X-Generation-Id", "gen-test-1"),
        ],
        body=body,
        queued_get_responses=queued,
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream, ledger=ledger, backend=calibration.OPENROUTER_BACKEND
        ) as broker:
            status, returned_headers, returned_body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": True,
                },
            )
            assert status == 200
            assert returned_headers["content-type"] == "text/event-stream"
            assert returned_body == body
            assert ledger.cost == Decimal("0.00007")
            assert ledger.reserved == 0
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            record = broker.state.records[0]
            assert record.request_id == "gen-test-1"
            assert record.usage == {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            }
    assert [item["path"] for item in upstream.requests] == [
        "/api/v1/responses",
        "/api/v1/generation?id=gen-test-1",
        "/api/v1/generation?id=gen-test-1",
    ]
    assert all(
        item["authorization"] == f"Bearer {TEST_PARENT_KEY}"
        for item in upstream.requests
    )


@pytest.mark.parametrize("stream", [False, True])
def test_openrouter_generation_header_and_terminal_bind_distinct_identities(
    stream: bool,
) -> None:
    body = (
        openrouter_responses_sse(response_id="gen-terminal")
        if stream
        else json.dumps(
            {
                "id": "gen-terminal",
                "model": "openai/gpt-5.6-sol",
                "status": "completed",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                },
            }
        ).encode()
    )
    with fake_upstream(
        headers=[
            (
                "Content-Type",
                "text/event-stream" if stream else "application/json",
            ),
            ("X-Generation-Id", "gen-header"),
        ],
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(
                    response_id="gen-header",
                    upstream_id="gen-terminal",
                    streamed=stream,
                ),
            )
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream, ledger=ledger, backend=calibration.OPENROUTER_BACKEND
        ) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": stream,
                    },
                )[0]
                == HTTPStatus.OK
            )
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            record = broker.state.records[0]
            assert record.request_id == "gen-header"
            assert record.settlement == "settled"
            assert record.cost == "0.00007"
            assert record.retained_unknown_reservation_usd == "0"
            assert ledger.cost == Decimal("0.00007")
    assert [item["path"] for item in upstream.requests] == [
        "/api/v1/responses",
        "/api/v1/generation?id=gen-header",
    ]


def test_openrouter_generation_compares_terminal_usage_to_native_tokens() -> None:
    body = openrouter_responses_sse(
        response_id="resp-terminal", input_tokens=6028, output_tokens=145
    )
    with fake_upstream(
        headers=[
            ("Content-Type", "text/event-stream"),
            ("X-Generation-Id", "gen-header"),
        ],
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(
                    response_id="gen-header",
                    upstream_id="resp-terminal",
                    model="openai/gpt-5.6-sol-20260709",
                    input_tokens=7415,
                    output_tokens=86,
                    native_input_tokens=6028,
                    native_output_tokens=145,
                ),
            )
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream,
            ledger=ledger,
            backend=calibration.OPENROUTER_BACKEND,
            canonical_model="openai/gpt-5.6-sol-20260709",
        ) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == HTTPStatus.OK
            )
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records[0].usage == {
                "input_tokens": 6028,
                "output_tokens": 145,
                "total_tokens": 6173,
            }
            assert ledger.cost == Decimal("0.00007")


@pytest.mark.parametrize("stream", [False, True])
def test_openrouter_generation_upstream_identifier_is_not_response_identity(
    stream: bool,
) -> None:
    body = (
        openrouter_responses_sse(response_id="gen-terminal")
        if stream
        else json.dumps(
            {
                "id": "gen-terminal",
                "model": "openai/gpt-5.6-sol",
                "status": "completed",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                },
            }
        ).encode()
    )
    with fake_upstream(
        headers=[
            (
                "Content-Type",
                "text/event-stream" if stream else "application/json",
            ),
            ("X-Generation-Id", "gen-header"),
        ],
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(
                    response_id="gen-header",
                    upstream_id="wrong-terminal",
                    streamed=stream,
                ),
            )
        ],
    ) as upstream:
        with running_broker(upstream, backend=calibration.OPENROUTER_BACKEND) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": stream,
                    },
                )[0]
                == HTTPStatus.OK
            )
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            record = broker.state.records[0]
            assert record.request_id == "gen-header"
            assert record.settlement == "settled"
            assert record.settlement_failure is None
            assert record.cost == "0.00007"
            assert record.retained_unknown_reservation_usd == "0"
    assert [item["path"] for item in upstream.requests] == [
        "/api/v1/responses",
        "/api/v1/generation?id=gen-header",
    ]


def test_openrouter_generation_retry_timeout_shrinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration, "SETTLEMENT_WINDOW_SECONDS", 0.5)
    monkeypatch.setattr(calibration, "SETTLEMENT_POLL_SECONDS", 0.05)
    original_connection = calibration._upstream_connection
    timeouts: list[float] = []

    def connection(
        parsed: urllib.parse.SplitResult, timeout: float
    ) -> http.client.HTTPConnection:
        timeouts.append(timeout)
        return original_connection(parsed, timeout)

    monkeypatch.setattr(calibration, "_upstream_connection", connection)
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=openrouter_responses_sse(),
        queued_get_responses=[
            (HTTPStatus.NOT_FOUND, [("Content-Type", "application/json")], b"{}"),
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(),
            ),
        ],
    ) as upstream:
        with running_broker(upstream, backend=calibration.OPENROUTER_BACKEND) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == HTTPStatus.OK
            )
    generation_timeouts = timeouts[1:]
    assert len(generation_timeouts) == 2
    assert 0 < generation_timeouts[1] < generation_timeouts[0] <= 0.5


@pytest.mark.parametrize("stall", ["connect", "headers", "body"])
def test_openrouter_generation_stalls_use_shrinking_settlement_window_and_retain(
    monkeypatch: pytest.MonkeyPatch, stall: str
) -> None:
    settlement_window = 0.15
    monkeypatch.setattr(calibration, "SETTLEMENT_WINDOW_SECONDS", settlement_window)
    response_gate = threading.Event() if stall == "headers" else None
    body_gate = threading.Event() if stall == "body" else None
    with fake_upstream(
        body=json.dumps(
            {
                "id": "gen-test-1",
                "model": "openai/gpt-5.6-sol",
                "status": "completed",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                },
            }
        ).encode(),
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(streamed=False),
            )
        ],
        get_response_gate=response_gate,
        get_body_gate=body_gate,
    ) as upstream:
        if stall == "connect":
            original_connection = calibration._upstream_connection
            calls = 0
            observed_timeouts: list[float] = []

            class StallingConnection(http.client.HTTPConnection):
                def connect(self) -> None:
                    assert self.timeout is not None
                    observed_timeouts.append(self.timeout)
                    time.sleep(self.timeout)
                    raise TimeoutError("simulated connect stall")

            def connection(
                parsed: urllib.parse.SplitResult, timeout: float
            ) -> http.client.HTTPConnection:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original_connection(parsed, timeout)
                assert parsed.hostname is not None
                return StallingConnection(parsed.hostname, parsed.port, timeout=timeout)

            monkeypatch.setattr(calibration, "_upstream_connection", connection)
        ledger = calibration.SpendLedger()
        started = time.monotonic()
        with running_broker(
            upstream,
            timeout=10,
            ledger=ledger,
            backend=calibration.OPENROUTER_BACKEND,
        ) as broker:
            status, _headers, _body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": False,
                },
            )
        elapsed = time.monotonic() - started
    assert status == HTTPStatus.BAD_GATEWAY
    assert elapsed < 1.0
    assert ledger.fatal is not None
    assert ledger.cost == 0
    assert ledger.reserved > 0
    if stall == "connect":
        assert observed_timeouts
        assert 0 < observed_timeouts[0] <= settlement_window


@pytest.mark.parametrize("include_event", [False, True])
def test_openrouter_stream_accepts_data_only_or_matching_event_with_comments(
    include_event: bool,
) -> None:
    body = openrouter_responses_sse(
        include_cost=False, include_event=include_event, include_comment=True
    )
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(),
            )
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream, ledger=ledger, backend=calibration.OPENROUTER_BACKEND
        ) as broker:
            status, _headers, returned = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": True,
                },
            )
            assert status == 200
            assert returned == body
            assert ledger.cost == Decimal("0.00007")


@pytest.mark.parametrize(
    ("model", "canonical_model"),
    [
        ("openai/gpt-5.6-sol", "openai/gpt-5.6-sol-20260709"),
        ("z-ai/glm-5.3-flash", "z-ai/glm-5.3-flash-20260826"),
    ],
)
def test_openrouter_stream_settles_provider_declared_canonical_model(
    model: str, canonical_model: str
) -> None:
    body = openrouter_responses_sse(model=canonical_model)
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(model=canonical_model),
            )
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream,
            ledger=ledger,
            backend=calibration.OPENROUTER_BACKEND,
            model=model,
            canonical_model=canonical_model,
        ) as broker:
            status, _headers, returned = request(
                broker,
                document={"model": model, "input": "redacted", "stream": True},
            )
    assert status == HTTPStatus.OK
    assert returned == body
    assert ledger.cost == Decimal("0.00007")


def test_openrouter_stream_rejects_model_outside_signed_alias_binding() -> None:
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=openrouter_responses_sse(model="other/provider-model-20260101"),
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream,
            ledger=ledger,
            backend=calibration.OPENROUTER_BACKEND,
            canonical_model="openai/gpt-5.6-sol-20260709",
        ) as broker:
            status, _headers, _body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": True,
                },
            )
    assert status == HTTPStatus.BAD_GATEWAY
    assert ledger.fatal is not None
    assert ledger.cost == 0
    assert ledger.reserved > 0
    assert [item["path"] for item in upstream.requests] == ["/api/v1/responses"]


def test_openrouter_stream_rejects_mismatched_optional_event_type() -> None:
    body = openrouter_responses_sse().replace(
        b"event: response.completed", b"event: response.created"
    )
    with pytest.raises(ValueError, match="event is malformed"):
        calibration._parse_openrouter_responses_stream(
            body, expected_models=frozenset({"openai/gpt-5.6-sol"})
        )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"data: [DONE]\n\n", id="missing-terminal"),
        pytest.param(
            openrouter_responses_sse().removesuffix(b"data: [DONE]\n\n"),
            id="missing-done",
        ),
        pytest.param(
            openrouter_responses_sse().replace(
                b"data: [DONE]\n\n",
                b'data: {"type":"response.created"}\n\ndata: [DONE]\n\n',
            ),
            id="trailing-event",
        ),
        pytest.param(
            openrouter_responses_sse() + b"data: [DONE]\n\n",
            id="duplicate-done",
        ),
        pytest.param(
            openrouter_responses_sse().replace(b"data: {", b"data:{", 1),
            id="malformed-field",
        ),
    ],
)
def test_openrouter_stream_rejects_missing_trailing_duplicate_or_malformed_frames(
    body: bytes,
) -> None:
    with pytest.raises(ValueError):
        calibration._parse_openrouter_responses_stream(
            body, expected_models=frozenset({"openai/gpt-5.6-sol"})
        )


def test_openrouter_nonstream_settles_from_body_and_generation() -> None:
    body = json.dumps(
        {
            "id": "gen-test-1",
            "model": "openai/gpt-5.6-sol",
            "status": "completed",
            "usage": {
                "cost": 0.00007,
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        }
    ).encode()
    with fake_upstream(
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(streamed=False),
            )
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream, ledger=ledger, backend=calibration.OPENROUTER_BACKEND
        ) as broker:
            status, _headers, returned_body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": False,
                },
            )
            assert status == 200
            assert returned_body == body
            assert ledger.cost == Decimal("0.00007")
    assert [item["path"] for item in upstream.requests] == [
        "/api/v1/responses",
        "/api/v1/generation?id=gen-test-1",
    ]


def test_openrouter_glm_streaming_chat_settles_exact_generation() -> None:
    body = openrouter_chat_sse(model="z-ai/glm-5.3-flash-20260826")
    with fake_upstream(
        headers=[
            ("Content-Type", "text/event-stream"),
            ("X-Generation-Id", "chat-generation-1"),
        ],
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(
                    response_id="chat-generation-1",
                    model="z-ai/glm-5.3-flash-20260826",
                ),
            )
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream,
            ledger=ledger,
            backend=calibration.OPENROUTER_BACKEND,
            model="z-ai/glm-5.3-flash",
            canonical_model="z-ai/glm-5.3-flash-20260826",
        ) as broker:
            status, _headers, returned = request(
                broker,
                path="/v1/chat/completions",
                document={
                    "messages": [{"content": "redacted", "role": "user"}],
                    "model": "z-ai/glm-5.3-flash",
                    "stream": True,
                },
            )
    assert status == 200
    assert returned == body
    assert ledger.cost == Decimal("0.00007")


@pytest.mark.parametrize(
    "body",
    [
        openrouter_chat_sse(choices=2),
        openrouter_chat_sse(usage_finish_reason="stop"),
        openrouter_chat_sse() + b'data: {"post":"terminal"}\n\n',
    ],
)
def test_openrouter_glm_streaming_chat_rejects_ambiguous_terminal(body: bytes) -> None:
    with pytest.raises(ValueError, match="OpenRouter chat"):
        calibration._parse_openrouter_chat_stream(
            body,
            expected_models=frozenset(
                {"z-ai/glm-5.3-flash", "z-ai/glm-5.3-flash-20260826"}
            ),
        )


def test_openrouter_nonstream_chat_normalizes_prompt_completion_usage() -> None:
    body = json.dumps(
        {
            "id": "gen-test-1",
            "model": "openai/gpt-5.6-sol",
            "choices": [],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            },
        }
    ).encode()
    with fake_upstream(
        body=body,
        queued_get_responses=[
            (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                openrouter_generation(streamed=False),
            )
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream, ledger=ledger, backend=calibration.OPENROUTER_BACKEND
        ) as broker:
            assert (
                request(
                    broker,
                    path="/v1/chat/completions",
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "messages": [{"role": "user", "content": "redacted"}],
                    },
                )[0]
                == 200
            )
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records
            assert broker.state.records[0].usage == {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            }
            assert ledger.cost == Decimal("0.00007")


@pytest.mark.parametrize(
    ("endpoint", "usage"),
    [
        (
            "/v1/responses",
            {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        ),
        (
            "/v1/chat/completions",
            {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        ),
    ],
)
def test_openrouter_nonstream_rejects_cross_endpoint_usage_fields(
    endpoint: str, usage: dict[str, int]
) -> None:
    document: dict[str, Any] = {
        "id": "gen-test-1",
        "model": "openai/gpt-5.6-sol",
        "usage": usage,
    }
    if endpoint == "/v1/responses":
        document["status"] = "completed"
    else:
        document["choices"] = []
    with pytest.raises(ValueError, match="token usage"):
        calibration._parse_openrouter_nonstream(
            json.dumps(document).encode(),
            endpoint=endpoint,
            expected_models=frozenset({"openai/gpt-5.6-sol"}),
        )


@pytest.mark.parametrize(
    ("generation", "settlement_failure"),
    [
        (openrouter_generation(response_id="wrong"), "generation_id_mismatch"),
        (openrouter_generation(model="other/model"), "generation_model_mismatch"),
        (openrouter_generation(model=["other/model"]), "generation_model_malformed"),
        (openrouter_generation(streamed=False), "generation_stream_mismatch"),
        (openrouter_generation(cost="0.00008"), "generation_cost_mismatch"),
        (
            openrouter_generation(native_input_tokens=4),
            "generation_native_tokens_mismatch",
        ),
    ],
)
def test_openrouter_generation_mismatch_retains_reservation_and_blocks_retry(
    generation: bytes, settlement_failure: str
) -> None:
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=openrouter_responses_sse(),
        queued_get_responses=[
            (HTTPStatus.OK, [("Content-Type", "application/json")], generation)
        ],
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(
            upstream, ledger=ledger, backend=calibration.OPENROUTER_BACKEND
        ) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == 502
            )
            assert ledger.cost == 0
            assert ledger.reserved > 0
            assert ledger.fatal == "authoritative settlement unavailable"
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records[0].settlement_failure == settlement_failure
            assert request(broker)[0] == 400
            assert len(upstream.requests) == 2


def test_openrouter_stream_requires_done_after_terminal() -> None:
    body = openrouter_responses_sse().removesuffix(b"data: [DONE]\n\n")
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")], body=body
    ) as upstream:
        with running_broker(upstream, backend=calibration.OPENROUTER_BACKEND) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == 502
            )
            assert broker.state.ledger.reserved > 0


def test_streaming_chat_is_rejected_before_reservation_or_forwarding() -> None:
    with fake_upstream() as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, _headers, _body = request(
                broker,
                path="/v1/chat/completions",
                document={
                    "model": "openai/gpt-5.6-sol",
                    "messages": [{"role": "user", "content": "redacted"}],
                    "stream": True,
                },
            )
            assert status == 400
            assert upstream.requests == []
            assert broker.state.request_count == 0
            assert ledger.cost == 0
            assert ledger.reserved == 0


def test_nonboolean_stream_mode_is_rejected_before_upstream() -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            status, _headers, _body = request(
                broker,
                document={"model": "openai/gpt-5.6-sol", "stream": "true"},
            )
            assert status == 400
            assert upstream.requests == []


def test_chat_injects_exactly_one_completion_and_reservation_covers_it() -> None:
    document = {
        "model": "openai/gpt-5.6-sol",
        "messages": [{"role": "user", "content": "https://example.test/plain"}],
    }
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert (
                request(broker, path="/v1/chat/completions", document=document)[0]
                == 200
            )
            forwarded = upstream.requests[0]["body"]
            assert forwarded["n"] == 1
            assert forwarded["max_completion_tokens"] == calibration.MAX_OUTPUT_TOKENS
            encoded = calibration.canonical(forwarded).encode()
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records
            assert Decimal(broker.state.records[0].worst_case_reservation_usd) == (
                Decimal(len(encoded)) * calibration.MAX_INPUT_OR_CACHE_COST_PER_TOKEN
                + Decimal(calibration.MAX_OUTPUT_TOKENS)
                * calibration.MAX_OUTPUT_COST_PER_TOKEN
                + calibration.RESERVATION_SAFETY_MARGIN
            )


@pytest.mark.parametrize(
    "field",
    [
        "n",
        "best_of",
        "candidate_count",
        "num_return_sequences",
        "parallel_samples",
        "num_generations",
    ],
)
def test_chat_rejects_completion_multiplicity(field: str) -> None:
    document: dict[str, Any] = {
        "model": "openai/gpt-5.6-sol",
        "messages": [{"role": "user", "content": "text"}],
        field: 64 if field == "n" else 2,
    }
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert (
                request(broker, path="/v1/chat/completions", document=document)[0]
                == 400
            )
            assert upstream.requests == []


@pytest.mark.parametrize(
    "field",
    ["n", "best_of", "candidate_count", "num_return_sequences", "background"],
)
def test_responses_rejects_multiplicity_and_background(field: str) -> None:
    document = {
        "model": "openai/gpt-5.6-sol",
        "input": "text",
        field: True if field == "background" else 2,
    }
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert request(broker, document=document)[0] == 400
            assert upstream.requests == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugins", [{"id": "web"}]),
        ("web_search_options", {}),
        ("models", ["fallback/model"]),
        ("route", "fallback"),
        ("transforms", ["middle-out"]),
        ("provider", {"order": ["provider-a"]}),
    ],
)
def test_unreserved_openrouter_routing_and_billable_surfaces_reject_before_reservation(
    field: str, value: Any
) -> None:
    with fake_upstream() as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, _headers, _body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "text",
                    field: value,
                },
            )
            assert status == 400
            assert broker.state.request_count == 0
            assert ledger.cost == 0
            assert ledger.reserved == 0
            assert upstream.requests == []


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search_preview"},
        {"type": "file_search", "vector_store_ids": ["vs-1"]},
        {"type": "computer_use_preview"},
        {"type": "code_interpreter"},
        {"type": "mcp", "server_url": "https://example.test"},
    ],
)
def test_server_side_tools_reject_before_reservation(tool: dict[str, Any]) -> None:
    with fake_upstream() as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "text",
                        "tools": [tool],
                    },
                )[0]
                == 400
            )
            assert broker.state.request_count == 0
            assert ledger.cost == 0
            assert ledger.reserved == 0
            assert upstream.requests == []


def test_client_function_tools_and_tool_results_are_preserved() -> None:
    document = {
        "model": "openai/gpt-5.6-sol",
        "messages": [
            {"role": "user", "content": "call it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "read a value",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert (
                request(broker, path="/v1/chat/completions", document=document)[0]
                == 200
            )
    forwarded = upstream.requests[0]["body"]
    assert forwarded["tools"] == document["tools"]
    assert forwarded["messages"] == document["messages"]


def test_responses_function_tools_calls_and_outputs_are_preserved() -> None:
    document = {
        "model": "openai/gpt-5.6-sol",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "call it"}],
            },
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "read",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "result",
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "read",
                "description": "read a value",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert request(broker, document=document)[0] == 200
    forwarded = upstream.requests[0]["body"]
    assert forwarded["tools"] == document["tools"]
    assert forwarded["input"] == document["input"]


def test_responses_pinned_encrypted_reasoning_replay_is_preserved() -> None:
    for summary in (
        [],
        [{"type": "summary_text", "text": "bounded summary"}],
    ):
        reasoning = {
            "type": "reasoning",
            "encrypted_content": "opaque-provider-reasoning",
            "summary": summary,
        }
        document = {
            "model": "openai/gpt-5.6-sol",
            "input": [
                reasoning,
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "result",
                },
            ],
        }
        with fake_upstream() as upstream:
            with running_broker(upstream) as broker:
                assert request(broker, document=document)[0] == 200

        assert upstream.requests[0]["body"]["input"] == document["input"]


@pytest.mark.parametrize(
    "reasoning",
    [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {
            "type": "reasoning",
            "encrypted_content": "opaque",
            "summary": [],
            "id": "provider-item-id",
        },
        {
            "type": "reasoning",
            "encrypted_content": "opaque",
            "summary": [],
            "image_url": "https://private.invalid/image",
        },
        {"type": "reasoning", "encrypted_content": None, "summary": []},
        {"type": "reasoning", "encrypted_content": {}, "summary": []},
        {"type": "reasoning", "encrypted_content": "opaque", "summary": None},
        {
            "type": "reasoning",
            "encrypted_content": "opaque",
            "summary": [{"type": "private", "text": "summary"}],
        },
        {
            "type": "reasoning",
            "encrypted_content": "opaque",
            "summary": [{"type": "summary_text", "text": 1}],
        },
        {
            "type": "reasoning",
            "encrypted_content": "opaque",
            "summary": [
                {"type": "summary_text", "text": "summary", "private": "value"}
            ],
        },
    ],
)
def test_responses_rejects_non_pinned_reasoning_replay_shape(
    reasoning: dict[str, Any],
) -> None:
    with fake_upstream() as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, _headers, _body = request(
                broker,
                document={"model": "openai/gpt-5.6-sol", "input": [reasoning]},
            )

            assert status == 400
            assert broker.state.request_count == 0
            assert ledger.cost == 0
            assert ledger.reserved == 0
            assert upstream.requests == []


@pytest.mark.parametrize(
    "field",
    [
        "maxOutputTokens",
        "maximum_output_length",
        "max_new_tokens",
        "output_token_limit",
    ],
)
def test_unknown_output_limit_aliases_are_rejected(field: str) -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            document = {"model": "openai/gpt-5.6-sol", "input": "text", field: 1}
            assert request(broker, document=document)[0] == 400
            assert upstream.requests == []


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "image_url", "image_url": {"url": "https://example.test/a"}}],
        [{"type": "input_audio", "audio": "AAAA"}],
        [{"type": "input_file", "file_id": "file-1"}],
        [{"type": "text", "text": "ok", "data_url": "data:text/plain,x"}],
        [{"type": "text", "text": "ok", "blob": "fetch-me"}],
    ],
)
def test_chat_rejects_multimodal_remote_and_file_content(content: Any) -> None:
    document = {
        "model": "openai/gpt-5.6-sol",
        "messages": [{"role": "user", "content": content}],
    }
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert (
                request(broker, path="/v1/chat/completions", document=document)[0]
                == 400
            )
            assert upstream.requests == []


@pytest.mark.parametrize(
    "item",
    [
        {"type": "input_image", "image_url": "https://example.test/a"},
        {"type": "input_file", "file_id": "file-1"},
        {"type": "message", "content": [{"type": "audio", "audio": "AAAA"}]},
    ],
)
def test_responses_rejects_multimodal_url_file_and_audio(item: Any) -> None:
    document = {"model": "openai/gpt-5.6-sol", "input": [item]}
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert request(broker, document=document)[0] == 400
            assert upstream.requests == []


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        ("/v1/responses", "modalities", ["text"]),
        ("/v1/responses", "audio", {"format": "wav"}),
        ("/v1/responses", "prediction", {"type": "content", "content": "x"}),
        ("/v1/chat/completions", "modalities", ["text"]),
        ("/v1/chat/completions", "audio", {"format": "wav"}),
    ],
)
def test_top_level_non_text_modalities_audio_and_prediction_are_rejected(
    path: str, field: str, value: Any
) -> None:
    document: dict[str, Any] = {"model": "openai/gpt-5.6-sol", field: value}
    if path == "/v1/responses":
        document["input"] = "text"
    else:
        document["messages"] = [{"role": "user", "content": "text"}]
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            assert request(broker, path=path, document=document)[0] == 400
            assert upstream.requests == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"token": "wrong"}, 401),
        ({"method": "GET"}, 405),
        ({"method": "DELETE"}, 405),
        ({"path": "/v1/models"}, 400),
        ({"path": "/v1/../models"}, 400),
        ({"path": "/v1/responses?admin=true"}, 400),
        ({"document": {"model": "z-ai/glm-5.3-flash"}}, 400),
        (
            {
                "document": {
                    "model": "openai/gpt-5.6-sol",
                    "max_output_tokens": 0,
                }
            },
            400,
        ),
        (
            {
                "document": {
                    "model": "openai/gpt-5.6-sol",
                    "max_tokens": 0,
                }
            },
            400,
        ),
    ],
)
def test_broker_rejects_auth_route_method_model_and_output_limits(
    kwargs: dict[str, Any], expected: int
) -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            status, _headers, _body = request(broker, **kwargs)
            assert status == expected
            assert upstream.requests == []


def test_broker_rejects_connect_transfer_encoding_and_ambiguous_length() -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            connect = raw_request(
                broker.port,
                b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n",
            )
            assert b" 405 " in connect
            token = broker.token.encode()
            transfer = raw_request(
                broker.port,
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer "
                + token
                + b"\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            )
            assert b" 400 " in transfer
            ambiguous = raw_request(
                broker.port,
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer "
                + token
                + b"\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            )
            assert b" 400 " in ambiguous


def test_broker_rejects_deep_valid_json_before_request_admission() -> None:
    depth = 2_000
    body = (
        b'{"model":"openai/gpt-5.6-sol","input":'
        + b"[" * depth
        + b'"private"'
        + b"]" * depth
        + b"}"
    )
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            token = broker.token.encode()
            response = raw_request(
                broker.port,
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer "
                + token
                + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body,
            )

            assert b" 400 " in response
            assert broker.state.request_count == 0
            assert upstream.requests == []


def test_broker_rejects_parser_safe_recursive_content_before_admission() -> None:
    depth = 1_100
    body = (
        b'{"model":"openai/gpt-5.6-sol","input":[{"type":'
        b'"function_call_output","call_id":"call-1","output":'
        + b"[" * depth
        + b'"private"'
        + b"]" * depth
        + b"}]}"
    )
    calibration.strict_json(body)
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            token = broker.token.encode()
            response = raw_request(
                broker.port,
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer "
                + token
                + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body,
            )

            assert b" 400 " in response
            assert broker.state.request_count == 0
            assert upstream.requests == []
            duplicate_auth = raw_request(
                broker.port,
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer "
                + token
                + b"\r\nAuthorization: Bearer "
                + token
                + b"\r\nContent-Length: 2\r\n\r\n{}",
            )
            assert b" 401 " in duplicate_auth
            duplicate_limit_body = (
                b'{"model":"openai/gpt-5.6-sol","max_output_tokens":1,'
                b'"max_output_tokens":2}'
            )
            duplicate_limit = raw_request(
                broker.port,
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + b"Authorization: Bearer "
                + token
                + b"\r\nContent-Length: "
                + str(len(duplicate_limit_body)).encode()
                + b"\r\n\r\n"
                + duplicate_limit_body,
            )
            assert b" 400 " in duplicate_limit
            assert upstream.requests == []


def test_broker_rejects_body_and_header_size_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            body_limit = calibration.MAX_BODY_BYTES
            monkeypatch.setattr(calibration, "MAX_BODY_BYTES", 8)
            status, _headers, _body = request(broker)
            assert status == 400
            monkeypatch.setattr(calibration, "MAX_BODY_BYTES", body_limit)
            monkeypatch.setattr(calibration, "MAX_HEADER_BYTES", 32)
            status, _headers, _body = request(broker, headers={"X-Oversized": "x" * 64})
            assert status == 400
            assert upstream.requests == []


def test_broker_accepts_exact_body_limit_and_rejects_one_byte_over() -> None:
    document = {
        "input": "",
        "max_output_tokens": calibration.MAX_OUTPUT_TOKENS,
        "model": "openai/gpt-5.6-sol",
    }
    empty = calibration.canonical(document).encode()
    document["input"] = "x" * (calibration.MAX_BODY_BYTES - len(empty))
    exact = calibration.canonical(document).encode()
    document["input"] += "x"
    over = calibration.canonical(document).encode()
    assert len(exact) == calibration.MAX_BODY_BYTES
    assert len(over) == calibration.MAX_BODY_BYTES + 1
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            token = broker.token.encode()

            def send(body: bytes) -> bytes:
                return raw_request(
                    broker.port,
                    b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                    + b"Authorization: Bearer "
                    + token
                    + b"\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body,
                )

            assert b" 200 " in send(exact)
            assert b" 400 " in send(over)
            assert len(upstream.requests) == 1


def test_conservative_input_byte_bound_honors_live_model_limit() -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream, max_input_tokens=64) as broker:
            status, _headers, _body = request(
                broker,
                document={"model": "openai/gpt-5.6-sol", "input": "x" * 64},
            )
            assert status == 400
            assert upstream.requests == []


def test_broker_rejects_expired_and_invalidated_token() -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream, timeout=0) as broker:
            status, _headers, _body = request(broker)
            assert status == 401
        headers = Message()
        headers["Authorization"] = f"Bearer {broker.token}"
        with pytest.raises(PermissionError, match="authorization rejected"):
            broker.state.authorize(headers)


def test_task_overlay_changes_only_compose_and_binds_external_network(
    tmp_path: Path,
) -> None:
    overlay = calibration.create_task_overlay(
        calibration.TASK, tmp_path / "overlay", "tb-cal-safe-1"
    )
    compose = (overlay.task / "environment/docker-compose.yaml").read_text()
    assert "context: ." in compose
    assert "dockerfile: Dockerfile" in compose
    assert ("    dns:\n      - 1.1.1.1\n      - 9.9.9.9\n    networks:\n") in compose
    assert compose.count("    dns:\n") == 1
    assert "external: true" in compose
    assert "name: tb-cal-safe-1" in compose
    candidate = {
        item["path"]: item
        for item in overlay.candidate_manifest
        if item["path"] != "environment/docker-compose.yaml"
    }
    overlaid = {
        item["path"]: item
        for item in overlay.overlay_manifest
        if item["path"] != "environment/docker-compose.yaml"
    }
    assert candidate == overlaid
    assert overlay.candidate_manifest_sha256 != overlay.overlay_manifest_sha256
    assert len(overlay.compose_sha256) == 64
    compose_entry = next(
        item
        for item in overlay.overlay_manifest
        if item["path"] == "environment/docker-compose.yaml"
    )
    assert compose_entry["mode"] == 0o644


@pytest.mark.parametrize(
    "value",
    ["bad network", "../escape", "$(id)", "-leading", "UPPER", "a" * 64],
)
def test_network_and_alias_injection_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="unsafe Docker"):
        calibration._safe_docker_name(value, "network name")


def test_network_allocation_excludes_policy_routes_and_docker_ipam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = calibration._docker_names("cal-network-selection", 2)
    candidates = tuple(
        calibration.ipaddress.IPv4Network(value)
        for value in ("10.224.0.0/28", "10.224.0.16/28", "10.224.0.32/28")
    )
    monkeypatch.setattr(calibration, "_network_candidates", lambda *_args: candidates)
    monkeypatch.setattr(
        calibration,
        "_host_policy_subnets",
        lambda: calibration._parse_host_policy_subnets(
            b'[{"dst":"10.224.0.0/28","dev":"tailscale0","table":52}]'
        ),
    )
    monkeypatch.setattr(
        calibration,
        "_docker_network_subnets",
        lambda: (calibration.ipaddress.IPv4Network("10.224.0.16/28"),),
    )
    calls: list[list[str]] = []

    def docker(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"network-id\n", b"")

    monkeypatch.setattr(calibration, "_docker", docker)
    allocation = calibration._create_attempt_network(names)
    assert allocation == calibration.DockerNetworkAllocation(
        subnet="10.224.0.32/28",
        candidate_probe=2,
        route_prefixes_checked=1,
        docker_subnets_checked=1,
        route_collision_rejections=1,
        docker_collision_rejections=1,
        create_overlap_retries=0,
    )
    assert calls == [
        [
            "network",
            "create",
            "--driver",
            "bridge",
            "--subnet",
            "10.224.0.32/28",
            *[
                value
                for key, value in names.role_labels("network").items()
                for value in ("--label", f"{key}={value}")
            ],
            names.network,
        ]
    ]


def test_network_allocation_retries_only_atomic_overlap_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = calibration._docker_names("cal-network-race", 1)
    candidates = tuple(
        calibration.ipaddress.IPv4Network(value)
        for value in ("10.224.1.0/28", "10.224.1.16/28")
    )
    assert calibration._network_candidates(
        names.run_id, names.ordinal
    ) == calibration._network_candidates(names.run_id, names.ordinal)
    monkeypatch.setattr(calibration, "_network_candidates", lambda *_args: candidates)
    monkeypatch.setattr(calibration, "_host_policy_subnets", lambda: ())
    monkeypatch.setattr(calibration, "_docker_network_subnets", lambda: ())
    calls = 0

    def docker(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                argv,
                1,
                b"",
                (
                    b"Error response from daemon: Pool overlaps with other one "
                    b"on this address space"
                ),
            )
        return subprocess.CompletedProcess(argv, 0, b"network-id\n", b"")

    monkeypatch.setattr(calibration, "_docker", docker)
    allocation = calibration._create_attempt_network(names)
    assert allocation.subnet == "10.224.1.16/28"
    assert allocation.candidate_probe == 1
    assert allocation.create_overlap_retries == 1
    assert calls == 2


def test_concurrent_network_allocators_deterministically_probe_after_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [calibration._docker_names("cal-concurrent-network", 1) for _ in range(2)]
    candidates = tuple(
        calibration.ipaddress.IPv4Network(value)
        for value in ("10.224.2.0/28", "10.224.2.16/28")
    )
    monkeypatch.setattr(calibration, "_network_candidates", lambda *_args: candidates)
    monkeypatch.setattr(calibration, "_host_policy_subnets", lambda: ())
    monkeypatch.setattr(calibration, "_docker_network_subnets", lambda: ())
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    allocated: set[str] = set()

    def docker(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        subnet = argv[argv.index("--subnet") + 1]
        if subnet == str(candidates[0]):
            barrier.wait(timeout=2)
        with lock:
            if subnet in allocated:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    b"",
                    b"Pool overlaps with other one on this address space",
                )
            allocated.add(subnet)
        return subprocess.CompletedProcess(argv, 0, b"network-id\n", b"")

    monkeypatch.setattr(calibration, "_docker", docker)
    with ThreadPoolExecutor(max_workers=2) as executor:
        allocations = list(executor.map(calibration._create_attempt_network, names))
    assert {allocation.subnet for allocation in allocations} == {
        "10.224.2.0/28",
        "10.224.2.16/28",
    }
    assert sorted(allocation.create_overlap_retries for allocation in allocations) == [
        0,
        1,
    ]


def test_network_allocation_does_not_swallow_other_create_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = calibration._docker_names("cal-network-failure", 1)
    monkeypatch.setattr(calibration, "_host_policy_subnets", lambda: ())
    monkeypatch.setattr(calibration, "_docker_network_subnets", lambda: ())
    monkeypatch.setattr(
        calibration,
        "_docker",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 1, b"", b"permission denied"
        ),
    )
    with pytest.raises(subprocess.CalledProcessError) as caught:
        calibration._create_attempt_network(names)
    assert caught.value.stderr == b"permission denied"


def test_network_allocation_rejects_candidate_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = calibration._docker_names("cal-network-exhaustion", 1)
    candidates = (
        calibration.ipaddress.IPv4Network("10.224.3.0/28"),
        calibration.ipaddress.IPv4Network("10.224.3.16/28"),
    )
    monkeypatch.setattr(calibration, "_network_candidates", lambda *_args: candidates)
    monkeypatch.setattr(
        calibration,
        "_host_policy_subnets",
        lambda: (calibration.ipaddress.IPv4Network("10.224.3.0/27"),),
    )
    monkeypatch.setattr(calibration, "_docker_network_subnets", lambda: ())
    monkeypatch.setattr(
        calibration,
        "_docker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("overlapping candidate reached Docker")
        ),
    )
    with pytest.raises(RuntimeError, match="candidate pool exhausted"):
        calibration._create_attempt_network(names)


@pytest.mark.parametrize(
    "data",
    [b"not-json", b"{}", b'[{"dst":42}]', b'[{"dst":"not-a-prefix"}]'],
)
def test_host_policy_route_json_fails_closed(data: bytes) -> None:
    with pytest.raises(ValueError):
        calibration._parse_host_policy_subnets(data)


def test_host_policy_route_command_failure_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        calibration.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, b"", b"failed"),
    )
    with pytest.raises(RuntimeError, match="route discovery failed"):
        calibration._ip_routes()


@pytest.mark.parametrize(
    "stdout",
    [b"not-json", b"{}", b'[{"IPAM":{"Config":"bad"}}]'],
)
def test_docker_network_command_json_fails_closed(
    stdout: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, b"network-id\n", b""),
            subprocess.CompletedProcess([], 0, stdout, b""),
        ]
    )
    monkeypatch.setattr(
        calibration, "_docker", lambda *_args, **_kwargs: next(responses)
    )
    with pytest.raises(ValueError):
        calibration._docker_network_subnets()


def test_docker_network_discovery_failure_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        calibration,
        "_docker",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b"", b"failed"),
    )
    with pytest.raises(RuntimeError, match="network discovery failed"):
        calibration._docker_network_subnets()


def test_docker_network_inspection_failure_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, b"network-id\n", b""),
            subprocess.CompletedProcess([], 1, b"", b"failed"),
        ]
    )
    monkeypatch.setattr(
        calibration, "_docker", lambda *_args, **_kwargs: next(responses)
    )
    with pytest.raises(RuntimeError, match="network inspection failed"):
        calibration._docker_network_subnets()


def test_docker_network_inspection_identity_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, b"expected-id\n", b""),
            subprocess.CompletedProcess(
                [],
                0,
                b'[{"Id":"other-id","IPAM":{"Config":[]}}]',
                b"",
            ),
        ]
    )
    monkeypatch.setattr(
        calibration, "_docker", lambda *_args, **_kwargs: next(responses)
    )
    with pytest.raises(ValueError, match="inspection is ambiguous"):
        calibration._docker_network_subnets()


def test_overlay_byte_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def corrupt_copy(source: Path, destination: Path) -> list[dict[str, Any]]:
        shutil.copytree(source, destination)
        (destination / "instruction.md").write_text("drift")
        return calibration.tree_manifest(source)

    monkeypatch.setattr(calibration, "_copy_verified_tree", corrupt_copy)
    with pytest.raises(ValueError, match="changed candidate bytes"):
        calibration.create_task_overlay(
            calibration.TASK, tmp_path / "overlay", "tb-cal-safe-1"
        )


def test_probe_invalid_then_valid_preserves_one_shot_token() -> None:
    token = TEST_PROBE_TOKEN
    with running_probe_broker(token) as broker:
        assert probe_request(broker, "Bearer wrong-token") == 401
        assert broker.state.probe_consumed is False
        assert probe_request(broker, f"Bearer {token}") == 204
        assert broker.state.probe_consumed is True


def test_probe_missing_then_valid_preserves_one_shot_token() -> None:
    token = TEST_PROBE_TOKEN
    with running_probe_broker(token) as broker:
        assert probe_request(broker, None) == 401
        assert broker.state.probe_consumed is False
        assert probe_request(broker, f"Bearer {token}") == 204
        assert broker.state.probe_consumed is True


def test_concurrent_valid_probes_have_exactly_one_success() -> None:
    token = TEST_PROBE_TOKEN
    barrier = threading.Barrier(3)

    def invoke(broker: calibration.CalibrationBroker) -> int:
        barrier.wait(timeout=5)
        return probe_request(broker, f"Bearer {token}")

    with running_probe_broker(token) as broker:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invoke, broker) for _ in range(2)]
            barrier.wait(timeout=5)
            statuses = [future.result(timeout=5) for future in futures]
        assert sorted(statuses) == [204, 401]
        assert broker.state.probe_consumed is True


def test_probe_before_deadline_consumes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = TEST_PROBE_TOKEN
    broker = calibration.CalibrationBroker(
        host="127.0.0.1",
        port=0,
        parent_key="",
        model="probe-only",
        ledger=calibration.SpendLedger(),
        probe_token=token,
    )
    broker.state.deadline = 100.0
    monkeypatch.setattr(calibration.time, "monotonic", lambda: 99.999)

    broker.state.consume_probe(probe_headers(token))

    assert broker.state.probe_consumed is True
    assert broker.state.active is True


@pytest.mark.parametrize("now", [100.0, 100.001])
def test_probe_at_or_after_deadline_expires_without_consuming(
    monkeypatch: pytest.MonkeyPatch, now: float
) -> None:
    token = TEST_PROBE_TOKEN
    broker = calibration.CalibrationBroker(
        host="127.0.0.1",
        port=0,
        parent_key="",
        model="probe-only",
        ledger=calibration.SpendLedger(),
        probe_token=token,
    )
    broker.state.deadline = 100.0
    monkeypatch.setattr(calibration.time, "monotonic", lambda: now)

    with pytest.raises(PermissionError, match="probe authorization rejected"):
        broker.state.consume_probe(probe_headers(token))

    assert broker.state.probe_consumed is False
    assert broker.state.active is False
    monkeypatch.setattr(calibration.time, "monotonic", lambda: 99.0)
    with pytest.raises(PermissionError, match="probe authorization rejected"):
        broker.state.consume_probe(probe_headers(token))


def test_concurrent_probes_waiting_at_expiry_never_consume_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = TEST_PROBE_TOKEN
    broker = calibration.CalibrationBroker(
        host="127.0.0.1",
        port=0,
        parent_key="",
        model="probe-only",
        ledger=calibration.SpendLedger(),
        probe_token=token,
    )
    broker.state.deadline = 100.0
    now = [99.0]
    monkeypatch.setattr(calibration.time, "monotonic", lambda: now[0])
    barrier = threading.Barrier(3)

    def consume() -> bool:
        barrier.wait(timeout=5)
        try:
            broker.state.consume_probe(probe_headers(token))
        except PermissionError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        with broker.state.lock:
            futures = [executor.submit(consume) for _ in range(2)]
            barrier.wait(timeout=5)
            now[0] = 100.0
        successes = [future.result(timeout=5) for future in futures]

    assert successes == [False, False]
    assert broker.state.probe_consumed is False
    assert broker.state.active is False


def test_broker_request_and_concurrency_limits_fail_before_upstream() -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            broker.state.request_count = calibration.MAX_REQUESTS
            status, _headers, _body = request(broker)
            assert status == 400
            broker.state.request_count = 0
            assert broker.state.semaphore.acquire()
            assert broker.state.semaphore.acquire()
            try:
                status, _headers, _body = request(broker)
                assert status == 429
            finally:
                broker.state.semaphore.release()
                broker.state.semaphore.release()
            assert upstream.requests == []


def test_forty_idle_connections_keep_worker_pool_bounded() -> None:
    with fake_upstream() as upstream:
        with running_broker(upstream) as broker:
            sockets: list[socket.socket] = []
            try:
                for _ in range(40):
                    connection = socket.create_connection(
                        ("127.0.0.1", broker.port), timeout=2
                    )
                    sockets.append(connection)
                deadline = time.monotonic() + 1
                while (
                    broker.server is not None
                    and broker.server.worker_count < calibration.MAX_WORKERS
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                assert broker.server is not None
                assert broker.server.worker_count <= calibration.MAX_WORKERS
                assert not any(
                    thread.name == "calibration-broker-request"
                    for thread in threading.enumerate()
                    if thread not in broker.server._worker_threads
                )
                deadline = (
                    time.monotonic() + calibration.HEADER_READ_TIMEOUT_SECONDS + 1
                )
                while broker.server.worker_count and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert broker.server.worker_count == 0
            finally:
                for connection in sockets:
                    connection.close()


@pytest.mark.parametrize("status", [200, 400, 500])
@pytest.mark.parametrize("value", [None, "bad", "-1", "NaN", "Infinity"])
def test_every_upstream_status_requires_finite_nonnegative_cost_and_retry_is_blocked(
    status: int,
    value: str | None,
) -> None:
    headers = [("Content-Type", "application/json")]
    if value is not None:
        headers.append(("X-Litellm-Response-Cost", value))
    with fake_upstream(headers=headers, status=status) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, returned_headers, body = request(broker)
            assert status == 502
            assert returned_headers["connection"] == "close"
            assert body == b'{"error":"upstream rejected"}'
            deadline = time.monotonic() + 2
            while ledger.fatal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ledger.fatal == "authoritative settlement unavailable"
            assert ledger.reserved > 0
            assert ledger.cost == 0
            assert request(broker)[0] == 400
            assert broker.state.request_count == 1
            assert len(upstream.requests) == 1
            deadline = time.monotonic() + 2
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(broker.state.records) == 1
            record = broker.state.records[0]
            assert record.cost is None
            assert record.settlement == "retained_unknown"
            assert Decimal(record.retained_unknown_reservation_usd) == ledger.reserved


@pytest.mark.parametrize("upstream_status", [400, 500])
def test_error_status_with_authoritative_cost_forwards_and_settles(
    upstream_status: int,
) -> None:
    with fake_upstream(status=upstream_status) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, returned_headers, body = request(broker)
            assert status == upstream_status
            assert returned_headers["content-type"] == "application/json"
            assert body == b'{"ok":true}'
            assert request(broker)[0] == upstream_status
            assert ledger.fatal is None
            assert ledger.reserved == 0
            assert ledger.cost == Decimal("0.25")
            assert len(upstream.requests) == 2


def test_successful_upstream_rejects_ambiguous_cost() -> None:
    headers = [
        ("Content-Type", "application/json"),
        ("X-Litellm-Response-Cost", "0.1"),
        ("X-Litellm-Response-Cost", "0.1"),
    ]
    with fake_upstream(headers=headers) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, returned_headers, body = request(broker)
            assert status == 502
            assert returned_headers["connection"] == "close"
            assert body == b'{"error":"upstream rejected"}'
            deadline = time.monotonic() + 2
            while ledger.fatal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ledger.fatal == "authoritative settlement unavailable"
            assert ledger.reserved > 0
            assert ledger.cost == 0
            assert request(broker)[0] == 400
            assert len(upstream.requests) == 1


def test_slow_first_stream_chunk_uses_attempt_deadline_not_backpressure_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration, "BACKPRESSURE_TIMEOUT_SECONDS", 0.05)
    body = responses_sse()
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=body,
        first_body_delay=0.15,
    ) as upstream:
        with running_broker(upstream, timeout=2) as broker:
            started = time.monotonic()
            status, _headers, returned_body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": True,
                },
            )
            assert time.monotonic() - started > 0.05
            assert status == 200
            assert returned_body == body
            assert broker.state.ledger.cost == Decimal("0.125")


def _nonterminal_sse(event_type: str) -> bytes:
    document = {"type": event_type, "response": {"status": "in_progress"}}
    return f"event: {event_type}\ndata: {json.dumps(document)}\n\n".encode()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty"),
        pytest.param(_nonterminal_sse("response.created"), id="missing-terminal"),
        pytest.param(
            responses_sse(event_type="response.failed", status="failed"),
            id="failed",
        ),
        pytest.param(
            responses_sse(event_type="response.incomplete", status="incomplete"),
            id="incomplete",
        ),
        pytest.param(responses_sse(status="incomplete"), id="unsuccessful-terminal"),
        pytest.param(responses_sse() + responses_sse(), id="duplicate-terminal"),
        pytest.param(
            responses_sse() + _nonterminal_sse("response.output_text.done"),
            id="trailing-event",
        ),
        pytest.param(responses_sse()[:-1], id="unterminated-sse"),
        pytest.param(
            b'event: response.completed\ndata: {"type":"response.completed",}\n\n',
            id="malformed-json",
        ),
        pytest.param(responses_sse(cost=True), id="boolean-cost"),
        pytest.param(responses_sse(cost="NaN"), id="nonfinite-cost"),
        pytest.param(responses_sse(input_tokens=True), id="boolean-token"),
        pytest.param(responses_sse(output_tokens=-1), id="negative-token"),
        pytest.param(responses_sse(total_tokens=6), id="incoherent-total"),
    ],
)
def test_invalid_responses_stream_retains_unknown_and_blocks_retry(body: bytes) -> None:
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")], body=body
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, returned_headers, returned_body = request(
                broker,
                document={
                    "model": "openai/gpt-5.6-sol",
                    "input": "redacted",
                    "stream": True,
                },
            )
            assert status == 502
            assert returned_headers["connection"] == "close"
            assert returned_body == b'{"error":"upstream rejected"}'
            assert ledger.fatal == "authoritative settlement unavailable"
            assert ledger.cost == 0
            assert ledger.reserved > 0
            assert request(broker)[0] == 400
            assert len(upstream.requests) == 1
            assert broker.state.records[0].settlement == "retained_unknown"


def test_stream_missing_cost_retains_unknown() -> None:
    document = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        },
    }
    body = f"event: response.completed\ndata: {json.dumps(document)}\n\n".encode()
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")], body=body
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == 502
            )
            assert ledger.fatal == "authoritative settlement unavailable"
            assert ledger.cost == 0
            assert ledger.reserved > 0


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="missing-content-type"),
        pytest.param([("Content-Type", "application/json")], id="wrong-content-type"),
        pytest.param(
            [("Content-Type", "text/event-stream; boundary=x")],
            id="unsupported-parameter",
        ),
        pytest.param(
            [
                ("Content-Type", "text/event-stream"),
                ("Content-Type", "text/event-stream"),
            ],
            id="duplicate-content-type",
        ),
    ],
)
def test_invalid_stream_content_type_retains_unknown(
    headers: list[tuple[str, str]],
) -> None:
    with fake_upstream(headers=headers, body=responses_sse()) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == 502
            )
            assert ledger.fatal == "authoritative settlement unavailable"
            assert ledger.cost == 0
            assert ledger.reserved > 0


def test_ambiguous_or_truncated_stream_framing_retains_unknown() -> None:
    body = responses_sse()
    cases = [
        (
            [
                ("Content-Type", "text/event-stream"),
                ("Transfer-Encoding", "chunked"),
            ],
            True,
            False,
        ),
        (
            [
                ("Content-Type", "text/event-stream"),
                ("Content-Length", str(len(body) + 1)),
            ],
            False,
            True,
        ),
        (
            [("Content-Type", "text/event-stream")],
            False,
            True,
        ),
    ]
    for headers, include_content_length, close_response in cases:
        with fake_upstream(
            body=body,
            headers=headers,
            include_content_length=include_content_length,
            close_response=close_response,
        ) as upstream:
            ledger = calibration.SpendLedger()
            with running_broker(upstream, ledger=ledger) as broker:
                assert (
                    request(
                        broker,
                        document={
                            "model": "openai/gpt-5.6-sol",
                            "input": "redacted",
                            "stream": True,
                        },
                    )[0]
                    == 502
                )
                assert ledger.fatal == "authoritative settlement unavailable"
                assert ledger.cost == 0
                assert ledger.reserved > 0


def test_unsuccessful_stream_http_status_retains_unknown() -> None:
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=responses_sse(),
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == 502
            )
            assert ledger.fatal == "authoritative settlement unavailable"
            assert ledger.cost == 0
            assert ledger.reserved > 0


def test_stream_timeout_retains_unknown_and_never_exceeds_attempt_deadline() -> None:
    gate = threading.Event()
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=responses_sse(),
        body_gate=gate,
    ) as upstream:
        ledger = calibration.SpendLedger()
        try:
            with running_broker(upstream, ledger=ledger, timeout=0.2) as broker:
                started = time.monotonic()
                assert (
                    request(
                        broker,
                        document={
                            "model": "openai/gpt-5.6-sol",
                            "input": "redacted",
                            "stream": True,
                        },
                    )[0]
                    == 502
                )
                assert time.monotonic() - started < 1
                assert ledger.fatal == "authoritative settlement unavailable"
                assert ledger.cost == 0
                assert ledger.reserved > 0
        finally:
            gate.set()


def test_cost_cap_and_exact_reservations_are_atomic() -> None:
    ledger = calibration.SpendLedger()
    first = ledger.reserve(Decimal("40"))
    ledger.settle(first, Decimal("40"))
    second = ledger.reserve(Decimal("10"))
    with pytest.raises(RuntimeError, match="remaining budget"):
        ledger.reserve(Decimal("0.01"))
    ledger.settle(second, Decimal("10"))
    assert ledger.cost == Decimal("50")
    assert ledger.reserved == 0


def test_prior_unknown_exposure_consumes_cap_without_becoming_cost_or_attempt() -> None:
    ledger = calibration.SpendLedger(prior_unknown_exposure=Decimal("0.96086"))
    assert ledger.prior_unknown_exposure == Decimal("0.96086")
    assert ledger.cost == 0
    with pytest.raises(RuntimeError, match="remaining budget"):
        ledger.reserve(Decimal("49.03915"))
    reservation = ledger.reserve(Decimal("49.03914"))
    ledger.release_unforwarded(reservation)
    assert ledger.cost == 0
    assert ledger.reserved == 0


def test_four_worst_case_reservations_fit_each_run_cap() -> None:
    worst = calibration._reservation_for_body(b"x" * calibration.MAX_BODY_BYTES)
    assert calibration.MAX_BODY_BYTES == 192 << 10
    assert calibration.MAX_OUTPUT_TOKENS == 65536
    assert worst == Decimal("5.49288")
    assert 4 * worst <= calibration.MAX_TOTAL_COST
    assert calibration.MAX_TOTAL_COST / 4 == Decimal("12.5")


@pytest.mark.parametrize("attempts_per_profile", [1, 2])
def test_attempt_allocations_are_deterministic_exact_and_individually_bounded(
    attempts_per_profile: int,
) -> None:
    available = calibration.MAX_TOTAL_COST
    allocations = calibration._allocate_attempt_budgets(
        available, attempts_per_profile=attempts_per_profile
    )
    assert len(allocations) == attempts_per_profile * len(calibration.PROFILE_CONTRACTS)
    assert sum(allocations, start=Decimal(0)) == available
    assert allocations == calibration._allocate_attempt_budgets(
        available, attempts_per_profile=attempts_per_profile
    )
    worst = calibration._reservation_for_body(b"x" * calibration.MAX_BODY_BYTES)
    assert all(allocation >= worst for allocation in allocations)


def test_first_attempt_multiple_requests_cannot_consume_later_allocations() -> None:
    allocations = calibration._allocate_attempt_budgets(
        calibration.MAX_TOTAL_COST, attempts_per_profile=2
    )
    ledger = calibration.SpendLedger(max_total=allocations[0])
    amount = allocations[0] / Decimal(2)
    first = ledger.reserve(amount)
    second = ledger.reserve(amount)
    with pytest.raises(RuntimeError, match="remaining budget"):
        ledger.reserve(Decimal("0.000001"))
    ledger.settle(first, Decimal(0))
    ledger.settle(second, Decimal(0))
    assert sum(allocations[1:], start=Decimal(0)) > 0


def test_attempt_allocation_fails_when_one_worst_request_cannot_fit() -> None:
    worst = calibration._reservation_for_body(b"x" * calibration.MAX_BODY_BYTES)
    with pytest.raises(RuntimeError, match="one worst-case request"):
        calibration._allocate_attempt_budgets(
            worst * 4 - Decimal("0.000001"), attempts_per_profile=2
        )


def test_arbitrary_settlement_cannot_exceed_its_reservation_or_cap() -> None:
    ledger = calibration.SpendLedger()
    reservation = ledger.reserve(Decimal("1"))
    with pytest.raises(RuntimeError, match="exceeded exact reservation"):
        ledger.settle(reservation, Decimal("50"))
    assert ledger.cost == 0
    assert ledger.reserved == 0
    assert ledger.fatal == "authoritative cost exceeded exact reservation"


def test_pricing_changes_within_hard_ceiling_cannot_break_reservation() -> None:
    body = b"x" * calibration.MAX_BODY_BYTES
    reservation = calibration._reservation_for_body(body)
    live_cost_at_declared_ceiling = (
        Decimal(len(body)) * calibration.MAX_INPUT_OR_CACHE_COST_PER_TOKEN
        + Decimal(calibration.MAX_OUTPUT_TOKENS) * calibration.MAX_OUTPUT_COST_PER_TOKEN
    )
    assert live_cost_at_declared_ceiling < reservation
    assert reservation - live_cost_at_declared_ceiling == (
        calibration.RESERVATION_SAFETY_MARGIN
    )


def test_two_concurrent_reservations_cannot_oversubscribe() -> None:
    ledger = calibration.SpendLedger()
    barrier = threading.Barrier(3)
    outcomes: list[calibration.Reservation | BaseException] = []

    def reserve() -> None:
        barrier.wait()
        try:
            outcomes.append(ledger.reserve(Decimal("26")))
        except BaseException as error:
            outcomes.append(error)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
    assert sum(isinstance(item, calibration.Reservation) for item in outcomes) == 1
    assert sum(isinstance(item, RuntimeError) for item in outcomes) == 1
    assert ledger.reserved == Decimal("26")


def test_reservation_failure_does_not_commit_endpoint_or_request_count() -> None:
    ledger = calibration.SpendLedger()
    ledger.reserve(calibration.MAX_TOTAL_COST)
    state = calibration.BrokerState(
        parent_key=TEST_PARENT_KEY,
        token=TEST_ATTEMPT_TOKEN,
        probe_token=None,
        model="model",
        max_input_tokens=100,
        deadline=time.monotonic() + 30,
        ledger=ledger,
    )
    with pytest.raises(RuntimeError, match="remaining budget"):
        state.begin_request("/v1/responses", Decimal("0.01"))
    assert state.locked_endpoint is None
    assert state.request_count == 0


def test_concurrent_begin_request_commits_only_the_reserved_winner() -> None:
    ledger = calibration.SpendLedger()
    state = calibration.BrokerState(
        parent_key=TEST_PARENT_KEY,
        token=TEST_ATTEMPT_TOKEN,
        probe_token=None,
        model="model",
        max_input_tokens=100,
        deadline=time.monotonic() + 30,
        ledger=ledger,
    )
    barrier = threading.Barrier(3)
    outcomes: list[tuple[int, calibration.Reservation] | BaseException] = []

    def begin() -> None:
        barrier.wait()
        try:
            outcomes.append(state.begin_request("/v1/responses", Decimal("26")))
        except BaseException as error:
            outcomes.append(error)

    threads = [threading.Thread(target=begin) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert sum(isinstance(item, tuple) for item in outcomes) == 1
    assert sum(isinstance(item, RuntimeError) for item in outcomes) == 1
    assert state.locked_endpoint == "/v1/responses"
    assert state.request_count == 1
    assert ledger.reserved == Decimal("26")
    state.semaphore.release()


def test_streaming_response_cap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calibration, "MAX_RESPONSE_BYTES", 8)
    with fake_upstream(body=b"0123456789") as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            status, returned_headers, body = request(broker)
            assert status == 502
            assert returned_headers["connection"] == "close"
            assert body == b'{"error":"upstream rejected"}'
            deadline = time.monotonic() + 2
            while ledger.fatal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ledger.fatal == "broker upstream response invalid"
            assert ledger.reserved == 0
            assert ledger.cost == Decimal("0.125")


def test_streaming_response_cap_retains_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = responses_sse()
    monkeypatch.setattr(calibration, "MAX_RESPONSE_BYTES", len(body) - 1)
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")], body=body
    ) as upstream:
        ledger = calibration.SpendLedger()
        with running_broker(upstream, ledger=ledger) as broker:
            assert (
                request(
                    broker,
                    document={
                        "model": "openai/gpt-5.6-sol",
                        "input": "redacted",
                        "stream": True,
                    },
                )[0]
                == 502
            )
            assert ledger.fatal == "authoritative settlement unavailable"
            assert ledger.cost == 0
            assert ledger.reserved > 0


def test_invalid_success_metadata_never_reaches_child() -> None:
    headers = [
        ("Content-Type", "text/upstream-secret"),
        ("X-Litellm-Response-Cost", "0.1"),
        ("Set-Cookie", "secret=1"),
        ("Content-Length", str(calibration.MAX_RESPONSE_BYTES + 1)),
    ]
    with fake_upstream(headers=headers) as upstream:
        with running_broker(upstream) as broker:
            status, returned_headers, body = request(broker)
            assert status == 502
            assert returned_headers["content-type"] == "application/json"
            assert returned_headers["connection"] == "close"
            assert "set-cookie" not in returned_headers
            assert body == b'{"error":"upstream rejected"}'


def test_disconnect_is_bounded_and_recorded() -> None:
    with fake_upstream(body=b"x" * (1 << 20)) as upstream:
        with running_broker(upstream) as broker:
            stream = socket.create_connection(("127.0.0.1", broker.port), timeout=5)
            body = b'{"model":"openai/gpt-5.6-sol","input":"text"}'
            stream.sendall(
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + f"Authorization: Bearer {broker.token}\r\n".encode()
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            stream.close()
            deadline = time.monotonic() + 3
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records
            assert broker.state.records[0].disconnected is True


def test_shutdown_invalidates_token_closes_listener_and_leaves_no_thread() -> None:
    with fake_upstream() as upstream:
        broker = calibration.CalibrationBroker(
            host="127.0.0.1",
            port=0,
            parent_key=TEST_PARENT_KEY,
            model="openai/gpt-5.6-sol",
            ledger=calibration.SpendLedger(),
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
        broker.start()
        port = broker.port
        broker.stop()
        assert broker.state.active is False
        assert broker.thread is None
        assert broker.server is None
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_shutdown_waits_for_inflight_upstream_and_leaves_no_request_thread() -> None:
    gate = threading.Event()
    with fake_upstream(response_gate=gate) as upstream:
        broker = calibration.CalibrationBroker(
            host="127.0.0.1",
            port=0,
            parent_key=TEST_PARENT_KEY,
            model="openai/gpt-5.6-sol",
            ledger=calibration.SpendLedger(),
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
        broker.start()
        client_done = threading.Event()

        def invoke() -> None:
            try:
                request(broker)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                client_done.set()

        client = threading.Thread(target=invoke)
        client.start()
        deadline = time.monotonic() + 3
        while not upstream.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert upstream.requests
        stop_errors: list[BaseException] = []

        def stop() -> None:
            try:
                broker.stop()
            except BaseException as error:
                stop_errors.append(error)

        stopper = threading.Thread(target=stop)
        stopper.start()
        time.sleep(0.1)
        gate.set()
        stopper.join(timeout=3)
        client.join(timeout=3)
        assert not stopper.is_alive()
        assert stop_errors == []
        assert client_done.is_set()
        assert not any(
            thread.name == "calibration-broker-request"
            for thread in threading.enumerate()
        )


def test_attempt_token_is_inactive_until_probe_then_activation_and_lease() -> None:
    state = calibration.BrokerState(
        parent_key=TEST_PARENT_KEY,
        token=None,
        probe_token=TEST_PROBE_TOKEN,
        model="model",
        max_input_tokens=100,
        deadline=time.monotonic() + 30,
        ledger=calibration.SpendLedger(),
    )
    attempt_headers = probe_headers("attempt-token-0123456789-abcdefghijklmnop")
    with pytest.raises(PermissionError):
        state.authorize(attempt_headers)
    state.consume_probe(probe_headers(TEST_PROBE_TOKEN))
    channel = (1, 2)
    state.activate("attempt-token-0123456789-abcdefghijklmnop", channel)
    state.authorize(attempt_headers)
    state.heartbeat(channel)
    state.last_heartbeat = time.monotonic() - calibration.HEARTBEAT_LEASE_SECONDS - 0.1
    assert state.lease_expired() is True
    state.invalidate()
    assert state.parent_key == ""


def test_authorize_at_exact_lease_expiry_revokes_all_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = threading.Event()
    state = calibration.BrokerState(
        parent_key=TEST_PARENT_KEY,
        token="attempt-token-0123456789-abcdefghijklmnop",
        probe_token=TEST_PROBE_TOKEN,
        model="model",
        max_input_tokens=100,
        deadline=1_000,
        ledger=calibration.SpendLedger(),
        shutdown_event=shutdown,
    )
    state.last_heartbeat = 100 - calibration.HEARTBEAT_LEASE_SECONDS

    class Upstream:
        def __init__(self) -> None:
            self.auto_open = 1
            self.closed = False
            self.sock: Upstream | None = self

        def shutdown(self, _how: int) -> None:
            self.closed = True

        def close(self) -> None:
            self.closed = True

    upstream = Upstream()
    state.upstreams.add(cast(Any, upstream))
    monkeypatch.setattr(calibration.time, "monotonic", lambda: 100)

    with pytest.raises(PermissionError, match="authorization rejected"):
        state.authorize(probe_headers("attempt-token-0123456789-abcdefghijklmnop"))
    assert state.active is False
    assert state.parent_key == ""
    assert upstream.closed is True
    assert shutdown.is_set()
    with pytest.raises(PermissionError, match="heartbeat rejected"):
        state.heartbeat((1, 2))


def test_expiry_after_capture_before_upstream_request_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = threading.Event()
    release_connect = threading.Event()

    class GatedConnection(http.client.HTTPConnection):
        connect_count = 0

        def connect(self) -> None:
            self.connect_count += 1
            super().connect()
            connected.set()
            assert release_connect.wait(timeout=5)

    created: list[GatedConnection] = []

    def connection_factory(
        parsed: urllib.parse.SplitResult,
        timeout: float,
    ) -> http.client.HTTPConnection:
        assert parsed.hostname is not None
        connection = GatedConnection(parsed.hostname, parsed.port, timeout=timeout)
        created.append(connection)
        return connection

    monkeypatch.setattr(calibration, "_upstream_connection", connection_factory)
    ledger = calibration.SpendLedger()
    with fake_upstream() as upstream:
        with running_broker(upstream, ledger=ledger) as broker:
            outcome: list[tuple[int, dict[str, str], bytes] | BaseException] = []

            def invoke() -> None:
                try:
                    outcome.append(request(broker))
                except BaseException as error:
                    outcome.append(error)

            client = threading.Thread(target=invoke)
            client.start()
            assert connected.wait(timeout=5)
            assert broker.state.request_count == 1
            broker.state.last_heartbeat = (
                time.monotonic() - calibration.HEARTBEAT_LEASE_SECONDS
            )
            with pytest.raises(PermissionError, match="heartbeat rejected"):
                broker.state.heartbeat((1, 2))
            release_connect.set()
            client.join(timeout=5)
            assert not client.is_alive()
            assert len(outcome) == 1
            assert upstream.requests == []
            assert ledger.cost == 0
            assert ledger.reserved == 0
            assert ledger.fatal == "broker upstream response invalid"
            assert created[0].connect_count == 1
            assert created[0].auto_open == 0
            assert created[0].sock is None


def test_expiry_closes_registered_socket_and_disables_reconnect() -> None:
    token = "attempt-token-0123456789-abcdefghijklmnop"
    with fake_upstream() as upstream:
        state = calibration.BrokerState(
            parent_key=TEST_PARENT_KEY,
            token=token,
            probe_token=None,
            model="model",
            max_input_tokens=100,
            deadline=time.monotonic() + 30,
            ledger=calibration.SpendLedger(),
        )
        connection = http.client.HTTPConnection(
            "127.0.0.1", upstream.server_address[1], timeout=5
        )
        connection.connect()
        admission = calibration.ForwardingAdmission()
        state.send_connected_request(
            connection,
            probe_headers(token),
            "/v1/responses",
            b"{}",
            {"Content-Type": "application/json"},
            admission,
        )
        response = connection.getresponse()
        response.read()
        assert admission.started is True
        assert upstream.requests[0]["authorization"] == f"Bearer {TEST_PARENT_KEY}"
        state.last_heartbeat = time.monotonic() - calibration.HEARTBEAT_LEASE_SECONDS
        with pytest.raises(PermissionError, match="heartbeat rejected"):
            state.heartbeat((1, 2))
        assert connection.sock is None
        assert connection.auto_open == 0
        assert state.upstreams == set()
        with pytest.raises(http.client.NotConnected):
            connection.request("POST", "/v1/responses", body=b"{}", headers={})
        assert len(upstream.requests) == 1


def test_heartbeat_expiry_closes_slow_stream_and_retains_unknown() -> None:
    body_gate = threading.Event()
    ledger = calibration.SpendLedger()
    with fake_upstream(
        headers=[("Content-Type", "text/event-stream")],
        body=responses_sse(),
        body_gate=body_gate,
    ) as upstream:
        with running_broker(upstream, ledger=ledger) as broker:
            outcome: list[tuple[int, dict[str, str], bytes] | BaseException] = []

            def invoke() -> None:
                try:
                    outcome.append(
                        request(
                            broker,
                            document={
                                "model": "openai/gpt-5.6-sol",
                                "input": "redacted",
                                "stream": True,
                            },
                        )
                    )
                except BaseException as error:
                    outcome.append(error)

            client = threading.Thread(target=invoke)
            client.start()
            deadline = time.monotonic() + 5
            while not upstream.requests and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(upstream.requests) == 1
            reserved = ledger.reserved
            assert reserved > 0
            broker.state.last_heartbeat = (
                time.monotonic() - calibration.HEARTBEAT_LEASE_SECONDS
            )
            with pytest.raises(PermissionError, match="heartbeat rejected"):
                broker.state.heartbeat((1, 2))
            body_gate.set()
            client.join(timeout=5)
            assert not client.is_alive()
            assert len(outcome) == 1
            assert len(upstream.requests) == 1
            assert ledger.cost == 0
            assert ledger.reserved == reserved
            assert ledger.fatal == "authoritative settlement unavailable"
            deadline = time.monotonic() + 5
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records[0].settlement == "retained_unknown"
            assert broker.state.upstreams == set()


def test_post_expiry_request_does_not_change_upstream_count_or_cost() -> None:
    ledger = calibration.SpendLedger()
    with fake_upstream() as upstream:
        with running_broker(upstream, ledger=ledger) as broker:
            broker.state.last_heartbeat = (
                time.monotonic() - calibration.HEARTBEAT_LEASE_SECONDS
            )
            status, _headers, _body = request(broker)
            assert status == HTTPStatus.UNAUTHORIZED
            assert upstream.requests == []
            assert broker.state.request_count == 0
            assert ledger.cost == 0
            assert ledger.reserved == 0


def test_concurrent_authorize_vs_exact_expiry_accepts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "attempt-token-0123456789-abcdefghijklmnop"
    state = calibration.BrokerState(
        parent_key=TEST_PARENT_KEY,
        token=token,
        probe_token=None,
        model="model",
        max_input_tokens=100,
        deadline=1_000,
        ledger=calibration.SpendLedger(),
    )
    state.last_heartbeat = 100 - calibration.HEARTBEAT_LEASE_SECONDS
    monkeypatch.setattr(calibration.time, "monotonic", lambda: 100)
    barrier = threading.Barrier(9)
    accepted: list[bool] = []

    def authorize() -> None:
        barrier.wait()
        try:
            state.authorize(probe_headers(token))
        except PermissionError:
            accepted.append(False)
        else:
            accepted.append(True)

    threads = [threading.Thread(target=authorize) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert accepted == [False] * 8
    assert state.active is False
    assert state.parent_key == ""


def test_create_disconnect_still_reconciles_exact_owned_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _docker_sidecar(tmp_path)
    exists = {sidecar.names.broker: False, sidecar.names.network: False}
    create_argv: list[str] = []

    def docker(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if argv[:2] == ["network", "create"]:
            exists[sidecar.names.network] = True
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv and argv[0] == "create":
            create_argv.extend(argv)
            exists[sidecar.names.broker] = True
            raise subprocess.TimeoutExpired(argv, 120)
        if argv[:3] == ["rm", "--force", sidecar.names.broker]:
            exists[sidecar.names.broker] = False
        if argv[:2] == ["network", "rm"]:
            exists[sidecar.names.network] = False
        if "inspect" in argv:
            name = argv[-1]
            return subprocess.CompletedProcess(
                argv, 0 if exists.get(name, False) else 1, b"", b""
            )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(calibration, "_docker", docker)
    monkeypatch.setattr(calibration, "_host_policy_subnets", lambda: ())
    monkeypatch.setattr(
        calibration,
        "_owned_resource_exists",
        lambda _kind, name, _labels: exists.get(name, False),
    )
    monkeypatch.setattr(
        calibration, "_remove_owned_attempt_containers", lambda _n: None
    )
    with pytest.raises(subprocess.TimeoutExpired):
        sidecar.start()
    cleanup = sidecar.cleanup()
    assert cleanup["broker_absent"] is True
    assert cleanup["network_absent"] is True
    assert exists == {sidecar.names.broker: False, sidecar.names.network: False}
    assert create_argv[create_argv.index("--network-alias") + 2 :][:2] == [
        "--dns",
        "100.100.100.100",
    ]
    assert [
        create_argv[index + 1]
        for index, value in enumerate(create_argv)
        if value == "--dns"
    ] == list(calibration.BROKER_DNS)


def test_attach_timeout_is_terminated_and_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _docker_sidecar(tmp_path)

    class Stdin:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Process:
        stdin = Stdin()
        terminated = False
        waits = 0

        def wait(self, timeout: float) -> int:
            del timeout
            self.waits += 1
            if not self.terminated:
                raise subprocess.TimeoutExpired("attach", 10)
            return 0

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            raise AssertionError("terminated attach client should reap")

        def poll(self) -> int | None:
            return 0 if self.terminated else None

    process = Process()
    sidecar.process = cast(Any, process)
    monkeypatch.setattr(calibration, "_owned_resource_exists", lambda *_args: False)
    monkeypatch.setattr(calibration, "_wait_resource_absent", lambda *_args: True)
    cleanup = sidecar.cleanup()
    assert process.terminated is True
    assert process.waits == 2
    assert cleanup["attach_client_reaped"] is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--attempts-per-profile", "0"],
        ["--attempts-per-profile", "1"],
        ["--debug", "--attempts-per-profile", "2"],
        [
            "--host-address",
            "127.0.0.1",
            "--debug",
            "--attempts-per-profile",
            "1",
            "--output",
            "proof.json",
        ],
        ["--debug", "--host-address", "0.0.0.0"],
        ["--debug", "--host-address", "docker-host"],
        ["--host-address", "127.0.0.1"],
        ["--broker-port", "49151"],
        ["--broker-port", "65536"],
        ["--debug-deny-upstream"],
        ["--debug-deny-upstream", "--output", "proof.json"],
        ["--debug", "--debug-deny-upstream", "--attempts-per-profile", "2"],
        ["--debug", "--prior-unknown-exposure-usd", "-1"],
        ["--debug", "--prior-unknown-exposure-usd", "NaN"],
        ["--debug", "--prior-known-cost-usd", "-1"],
        ["--debug", "--prior-known-cost-usd", "NaN"],
    ],
)
def test_parser_rejects_noncanonical_attempt_or_listener_contract(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        calibration.parse_arguments(argv)
    assert error.value.code == 2


def test_historical_accounting_does_not_reduce_current_run_cap() -> None:
    args = calibration.parse_arguments(
        [
            "--debug",
            "--prior-known-cost-usd",
            "60",
            "--prior-unknown-exposure-usd",
            "70",
        ]
    )
    report = calibration._failure(args, RuntimeError("preflight failed"))
    assert report["budget"] == {
        "attempt_allocations_usd": ["25", "25"],
        "available_budget_usd": "50",
        "total_cap_usd": "50",
    }
    assert report["spend_exposure"]["total_usd"] == "130"


def test_debug_two_attempts_rejects_before_pricing_or_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def query(**_kwargs: Any) -> calibration.PricingSnapshot:
        nonlocal called
        called = True
        raise AssertionError("pricing must not run")

    monkeypatch.setattr(calibration, "query_model_pricing", query)
    with pytest.raises(SystemExit) as error:
        calibration.main(["--debug", "--attempts-per-profile", "2"])
    assert error.value.code == 2
    assert called is False
    assert not any(
        thread.name == "calibration-broker" for thread in threading.enumerate()
    )


def test_zero_request_nonzero_cli_failure_keeps_command_precedence(
    tmp_path: Path,
) -> None:
    result = calibration.CommandResult(
        returncode=7,
        stdout=b'{"error":"credential-shaped private material"}\n',
        stderr=b"private stderr material",
        containment={"survivors": 0},
    )
    evidence, document, parse_status = calibration._command_outcome_evidence(
        result, tmp_path / "absent-output"
    )
    attempt = calibration._started_attempt(
        ordinal=1,
        profile="target",
        model_group="openai/gpt-5.6-sol",
    )
    attempt["command_outcome"] = evidence
    with pytest.raises(calibration.AttemptFailure) as caught:
        calibration._validate_cli_outcome(result, document, parse_status)
    calibration._mark_attempt_failed(attempt, caught.value)
    assert attempt["failure"] == {
        "exception_class": "RuntimeError",
        "stage": "agent_install_or_execution",
        "type": "nonzero_exit",
    }
    assert attempt["broker"]["request_count"] == 0
    retained = json.dumps(attempt)
    assert "credential-shaped" not in retained
    assert "private stderr" not in retained


def test_malformed_cli_stdout_is_digest_only_cli_schema_failure(tmp_path: Path) -> None:
    result = calibration.CommandResult(
        returncode=0,
        stdout=b"malformed secret-bearing stdout",
        stderr=b"",
        containment={"survivors": 0},
    )
    evidence, document, parse_status = calibration._command_outcome_evidence(
        result, tmp_path / "absent-output"
    )
    with pytest.raises(calibration.AttemptFailure) as caught:
        calibration._validate_cli_outcome(result, document, parse_status)
    assert caught.value.evidence == {
        "exception_class": "ValueError",
        "stage": "cli_schema",
        "type": "malformed_stdout",
    }
    assert evidence["stdout"]["bytes"] == len(result.stdout)
    assert len(evidence["stdout"]["sha256"]) == 64
    assert evidence["canonical_cli"] is None
    assert "secret-bearing" not in json.dumps(evidence)


def test_deny_upstream_accepts_expected_failed_cli_outcome() -> None:
    document = {
        "job_directory": "/private/output",
        "outcome": "failed",
        "reward": "0",
        "schema_version": 1,
        "summary": {},
    }
    result = calibration.CommandResult(
        returncode=1,
        stdout=(json.dumps(document) + "\n").encode(),
        stderr=b"",
        containment={"survivors": 0},
    )
    assert (
        calibration._validate_cli_outcome(
            result, document, "canonical", debug_deny_upstream=True
        )
        == document
    )


def test_deny_upstream_rejects_any_forwarded_request() -> None:
    requests = [
        {
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
        for _ in range(calibration.DENY_UPSTREAM_EXPECTED_REQUESTS["target"])
    ]
    requests[-1]["upstream_opened"] = True
    with pytest.raises(
        calibration.AttemptFailure, match="request_forwarded_or_malformed"
    ):
        calibration._validate_deny_upstream_ledger(
            {
                "known_actual_cost_usd": "0",
                "request_count": len(requests),
                "requests": requests,
                "retained_unknown_reservation_usd": "0",
            },
            profile="target",
        )


def test_failed_native_output_retains_only_structure_digests_and_classes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    harbor = output / "harbor-job"
    harbor.mkdir()
    (harbor / "result.json").write_text('{"exception":"private model output"}')
    result = calibration.CommandResult(
        returncode=1,
        stdout=b"{}\n",
        stderr=b"",
        containment={"survivors": 0},
    )
    evidence, _document, _parse_status = calibration._command_outcome_evidence(
        result, output
    )
    native = evidence["native"]
    assert native["snapshot"]["status"] == "captured"
    assert native["snapshot"]["file_count"] == 1
    assert len(native["snapshot"]["manifest_sha256"]) == 64
    assert native["structure"]["status"] == "invalid"
    assert native["structure"]["exception_classes"] == ["ValidationError"]
    retained = json.dumps(native)
    assert "private model output" not in retained
    assert str(output) not in retained


def test_native_runtime_diagnostic_retains_only_failure_shapes_and_counts() -> None:
    trial_name = "authority-fencing__opencode__model__1"
    native = calibration.NativeSnapshot(
        files={
            f"harbor-job/{trial_name}/agent/opencode.txt": (
                b'{"type":"step_start","private":"prompt"}\n'
                b'{"type":"tool_use","part":{"state":{"status":"completed"},'
                b'"private":"tool output"}}\n'
                b'{"type":"error","error":{"name":"UnknownError",'
                b'"data":{"message":"private error"}}}\n'
                b"private malformed line\n"
            ),
            f"harbor-job/{trial_name}/agent/trajectory.json": json.dumps(
                {
                    "steps": [
                        {"message": "private prompt", "source": "user"},
                        {
                            "message": "private model text",
                            "observation": {
                                "results": [{"content": "private tool output"}]
                            },
                            "reasoning_content": "private reasoning",
                            "source": "agent",
                            "tool_calls": [
                                {
                                    "arguments": {"secret": "private"},
                                    "function_name": "private_tool_name",
                                }
                            ],
                        },
                    ]
                }
            ).encode(),
        },
        manifest=[],
    )
    job = SimpleNamespace(
        n_total_trials=1,
        stats=SimpleNamespace(
            n_cancelled_trials=0,
            n_completed_trials=1,
            n_errored_trials=1,
        ),
    )
    trial = SimpleNamespace(
        agent_result=object(),
        exception_info=SimpleNamespace(
            exception_message=(
                "Command failed (exit 137): private command and private output"
            ),
            exception_type="NonZeroAgentExitCodeError",
        ),
        trial_name=trial_name,
        verifier_result=SimpleNamespace(rewards={"reward": 1}),
    )

    diagnostic = calibration._native_runtime_diagnostic(
        native, job=cast(Any, job), trial=cast(Any, trial)
    )

    assert diagnostic == {
        "job": {
            "cancelled_trials": 0,
            "completed_trials": 1,
            "errored_trials": 1,
        },
        "opencode_events": {
            "error_event_count": 1,
            "error_kinds": {"UnknownError": 1},
            "event_kinds": {
                "error": 1,
                "malformed": 1,
                "step_start": 1,
                "tool_use": 1,
            },
            "final_event_kind": "malformed",
            "malformed_line_count": 1,
            "parsed_event_count": 3,
            "status": "captured",
            "tool_statuses": {"completed": 1},
        },
        "status": "captured",
        "trajectory": {
            "agent_step_count": 1,
            "final_agent_step": {
                "message_present": True,
                "observation_result_count": 1,
                "reasoning_present": True,
                "tool_call_count": 1,
            },
            "status": "captured",
            "step_count": 2,
        },
        "trial": {
            "agent_result_present": True,
            "exit": {
                "exit_code": 137,
                "exit_kind": "signal_compatible_status",
                "status": "captured",
            },
            "exception_kind": "NonZeroAgentExitCodeError",
            "verifier_result_present": True,
            "verifier_reward": 1,
        },
    }
    retained = json.dumps(diagnostic)
    assert "private" not in retained
    assert trial_name not in retained


@pytest.mark.parametrize(
    ("total", "counts", "reward"),
    [
        (2, (1, 1, 0), 1),
        (1, (-1, 1, 0), 1),
        (1, (0, 0, 0), 1),
        (1, (2, 1, 0), 1),
        (1, (1, 0, 0), 1),
        (1, (1, 2, 0), 1),
        (1, (1, 1, 2), 1),
        (1, (1, 1, 0), 10**1000),
    ],
)
def test_native_runtime_diagnostic_rejects_unbounded_counts_and_rewards(
    total: int, counts: tuple[int, int, int], reward: int
) -> None:
    completed, errored, cancelled = counts
    job = SimpleNamespace(
        n_total_trials=total,
        stats=SimpleNamespace(
            n_cancelled_trials=cancelled,
            n_completed_trials=completed,
            n_errored_trials=errored,
        ),
    )
    trial = SimpleNamespace(
        agent_result=object(),
        exception_info=SimpleNamespace(exception_type="PrivateException"),
        trial_name="private-trial-name",
        verifier_result=SimpleNamespace(rewards={"reward": reward}),
    )

    diagnostic = calibration._native_runtime_diagnostic(
        calibration.NativeSnapshot(files={}, manifest=[]),
        job=cast(Any, job),
        trial=cast(Any, trial),
    )

    assert diagnostic == {"status": "malformed"}


def test_native_diagnostic_rejects_partial_valid_result_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    harbor = output / "harbor-job"
    good_trial = harbor / "good"
    bad_trial = harbor / "bad"
    harbor.mkdir()
    good_trial.mkdir()
    bad_trial.mkdir()
    (harbor / "result.json").write_bytes(b"job")
    (good_trial / "result.json").write_bytes(b"good")
    (bad_trial / "result.json").write_bytes(b"private malformed result")
    fake_job = SimpleNamespace(
        n_total_trials=1,
        stats=SimpleNamespace(
            n_cancelled_trials=0,
            n_completed_trials=1,
            n_errored_trials=1,
        ),
    )
    fake_trial = SimpleNamespace(
        agent_result=object(),
        exception_info=SimpleNamespace(exception_type="NonZeroAgentExitCodeError"),
        trial_name="good",
        verifier_result=SimpleNamespace(rewards={"reward": 1}),
    )
    monkeypatch.setattr(
        calibration.JobResult,
        "model_validate_json",
        staticmethod(lambda data: fake_job),
    )

    def parse_trial(data: bytes) -> Any:
        if data == b"good":
            return fake_trial
        raise ValueError("private parser failure")

    monkeypatch.setattr(
        calibration.TrialResult,
        "model_validate_json",
        staticmethod(parse_trial),
    )

    diagnostic = calibration._native_diagnostic(output)

    assert diagnostic["structure"] == {
        "exception_classes": ["ValueError"],
        "parsed_result_count": 2,
        "result_count": 3,
        "status": "invalid",
    }
    assert diagnostic["runtime"] == {"status": "unavailable"}
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("CancelledError", "CancelledError"),
        ("NonZeroAgentExitCodeError", "NonZeroAgentExitCodeError"),
        ("PrivateProviderMessage", "other"),
        ({"private": "value"}, "malformed"),
    ],
)
def test_native_exception_kind_is_a_fixed_bounded_enum(
    value: Any, expected: str | None
) -> None:
    assert calibration._native_exception_kind(value) == expected


@pytest.mark.parametrize(
    ("exception_kind", "message", "expected"),
    [
        (
            "NonZeroAgentExitCodeError",
            "Command failed (exit 1): private command\nstdout: private output",
            {"exit_code": 1, "exit_kind": "status", "status": "captured"},
        ),
        (
            "NonZeroAgentExitCodeError",
            "Command failed (exit 255): private command",
            {
                "exit_code": 255,
                "exit_kind": "signal_compatible_status",
                "status": "captured",
            },
        ),
        (
            "NonZeroAgentExitCodeError",
            "OpenCode emitted error event(s): private error",
            {"status": "unavailable"},
        ),
        (
            "NonZeroAgentExitCodeError",
            "Command failed (exit 0): private command",
            {"status": "unavailable"},
        ),
        (
            "NonZeroAgentExitCodeError",
            "Command failed (exit 001): private command",
            {"status": "unavailable"},
        ),
        (
            "NonZeroAgentExitCodeError",
            "Command failed (exit 256): private command",
            {"status": "unavailable"},
        ),
        (
            "NonZeroAgentExitCodeError",
            "Command failed (exit 999): private command",
            {"status": "unavailable"},
        ),
        ("other", "Command failed (exit 1): private", {"status": "not_applicable"}),
        ("NonZeroAgentExitCodeError", {"private": "value"}, {"status": "malformed"}),
    ],
)
def test_native_agent_exit_diagnostic_retains_only_bounded_status(
    exception_kind: str, message: Any, expected: dict[str, Any]
) -> None:
    diagnostic = calibration._native_agent_exit_diagnostic(
        SimpleNamespace(exception_message=message), exception_kind=exception_kind
    )

    assert diagnostic == expected
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        *[
            ({"name": name, "data": {"message": "private"}}, name)
            for name in sorted(PINNED_OPENCODE_ERROR_KINDS)
        ],
        ({"name": "PrivateError", "data": {"message": "private"}}, "other"),
        ({"name": {"private": "value"}}, "malformed"),
        (["private"], "malformed"),
    ],
)
def test_opencode_event_diagnostic_retains_only_fixed_error_kind(
    value: Any, expected: str
) -> None:
    trial_name = "private-trial"
    native = calibration.NativeSnapshot(
        files={
            f"harbor-job/{trial_name}/agent/opencode.txt": json.dumps(
                {"type": "error", "error": value, "private": "event value"}
            ).encode()
        },
        manifest=[],
    )

    diagnostic = calibration._opencode_event_diagnostic(native, trial_name=trial_name)

    assert diagnostic == {
        "error_event_count": 1,
        "error_kinds": {expected: 1},
        "event_kinds": {"error": 1},
        "final_event_kind": "error",
        "malformed_line_count": 0,
        "parsed_event_count": 1,
        "status": "captured",
        "tool_statuses": {},
    }
    assert "private" not in json.dumps(diagnostic)


def test_opencode_error_kind_contract_matches_pinned_v1_18_26_schema() -> None:
    assert calibration.OPENCODE_ERROR_KINDS == PINNED_OPENCODE_ERROR_KINDS


def test_opencode_event_contract_matches_pinned_v1_18_26_schema() -> None:
    assert calibration.OPENCODE_EVENT_KINDS == PINNED_OPENCODE_EVENT_KINDS
    assert calibration.OPENCODE_TOOL_STATUSES == PINNED_OPENCODE_TOOL_STATUSES


@pytest.mark.parametrize(
    ("event_type", "tool_status", "expected_event", "expected_tool"),
    [
        ("step_start", None, "step_start", None),
        ("step_finish", None, "step_finish", None),
        ("text", None, "text", None),
        ("reasoning", None, "reasoning", None),
        ("tool_use", "completed", "tool_use", "completed"),
        ("tool_use", "error", "tool_use", "error"),
        ("tool_use", "private", "tool_use", "other"),
        ("private", None, "other", None),
        (None, None, "malformed", None),
    ],
)
def test_opencode_event_diagnostic_retains_only_fixed_shape(
    event_type: Any,
    tool_status: Any,
    expected_event: str,
    expected_tool: str | None,
) -> None:
    trial_name = "private-trial"
    event: dict[str, Any] = {"type": event_type, "private": "event content"}
    if event_type == "tool_use":
        event["part"] = {
            "state": {"status": tool_status, "private": "state content"},
            "private": "part content",
        }
    native = calibration.NativeSnapshot(
        files={
            f"harbor-job/{trial_name}/agent/opencode.txt": json.dumps(event).encode()
        },
        manifest=[],
    )

    diagnostic = calibration._opencode_event_diagnostic(native, trial_name=trial_name)

    assert diagnostic == {
        "error_event_count": 0,
        "error_kinds": {},
        "event_kinds": {expected_event: 1},
        "final_event_kind": expected_event,
        "malformed_line_count": 0,
        "parsed_event_count": 1,
        "status": "captured",
        "tool_statuses": ({expected_tool: 1} if expected_tool is not None else {}),
    }
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "part",
    [
        None,
        ["private"],
        {"state": None, "private": "part content"},
        {"state": ["private"], "private": "part content"},
        {"state": {"status": None, "private": "state content"}},
    ],
)
def test_opencode_event_diagnostic_collapses_malformed_tool_shape(part: Any) -> None:
    trial_name = "private-trial"
    native = calibration.NativeSnapshot(
        files={
            f"harbor-job/{trial_name}/agent/opencode.txt": json.dumps(
                {"type": "tool_use", "part": part}
            ).encode()
        },
        manifest=[],
    )

    diagnostic = calibration._opencode_event_diagnostic(native, trial_name=trial_name)

    assert diagnostic["tool_statuses"] == {"malformed": 1}
    assert "private" not in json.dumps(diagnostic)


def test_trajectory_shape_diagnostic_rejects_malformed_shape_without_content() -> None:
    trial_name = "trial-private"
    native = calibration.NativeSnapshot(
        files={
            f"harbor-job/{trial_name}/agent/trajectory.json": json.dumps(
                {
                    "steps": [
                        {
                            "message": "private model text",
                            "source": "agent",
                            "tool_calls": {"private": "value"},
                        }
                    ]
                }
            ).encode()
        },
        manifest=[],
    )

    diagnostic = calibration._trajectory_shape_diagnostic(native, trial_name=trial_name)

    assert diagnostic == {"status": "malformed"}
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b'{"steps":[0]}', id="non-object-step"),
        pytest.param(
            b"[" * 10_000 + b"]" * 10_000,
            id="recursive-json",
        ),
        pytest.param(
            json.dumps({"steps": [{"source": "agent"}] * 10_001}).encode(),
            id="too-many-steps",
        ),
        pytest.param(b"{" + b"x" * (4 << 20), id="oversized"),
    ],
)
def test_trajectory_shape_diagnostic_bounds_input(data: bytes) -> None:
    trial_name = "private-trial"
    native = calibration.NativeSnapshot(
        files={f"harbor-job/{trial_name}/agent/trajectory.json": data},
        manifest=[],
    )

    diagnostic = calibration._trajectory_shape_diagnostic(native, trial_name=trial_name)

    assert diagnostic == {"status": "malformed"}


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"{}\n" * 10_001, id="too-many-lines"),
        pytest.param(
            b"[" * 10_000 + b"]" * 10_000 + b"\n",
            id="recursive-json",
        ),
        pytest.param(
            b'{"type":"error","private":"' + b"x" * (1 << 20) + b'"}\n',
            id="oversized-line",
        ),
    ],
)
def test_opencode_event_diagnostic_bounds_jsonl_work(data: bytes) -> None:
    trial_name = "private-trial"
    native = calibration.NativeSnapshot(
        files={f"harbor-job/{trial_name}/agent/opencode.txt": data},
        manifest=[],
    )

    diagnostic = calibration._opencode_event_diagnostic(native, trial_name=trial_name)

    assert diagnostic == {"status": "malformed"}
    assert "private" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "failed_phase",
    [
        "sidecar_start",
        "topology_probe",
        "cli_spawn",
        "main_discovery",
        "main_config_validation",
        "broker_activation",
        "heartbeat_start",
    ],
)
def test_each_preactivation_phase_retains_only_safe_stage_authority(
    failed_phase: str,
) -> None:
    attempt = calibration._started_attempt(
        ordinal=1,
        profile="target",
        model_group="openai/gpt-5.6-sol",
    )

    class PrivateFailure(RuntimeError):
        pass

    for phase in calibration.ATTEMPT_PHASES:
        if phase == failed_phase:
            with pytest.raises(calibration.CalibrationStageError) as caught:
                calibration._run_phase(
                    attempt,
                    phase,
                    lambda: (_ for _ in ()).throw(
                        PrivateFailure("secret message /private/path")
                    ),
                )
            break
        calibration._run_phase(attempt, phase, lambda: None)

    assert caught.value.evidence == {
        "cause_class": "PrivateFailure",
        "failed_stage": failed_phase,
    }
    assert attempt["phases"]["failed_stage"] == failed_phase
    assert [item["phase"] for item in attempt["phases"]["timeline"]] == list(
        calibration.ATTEMPT_PHASES
    )
    failed = next(
        item for item in attempt["phases"]["timeline"] if item["phase"] == failed_phase
    )
    assert failed == {
        "completed": False,
        "failed": True,
        "phase": failed_phase,
        "started": True,
    }
    retained = json.dumps(
        {"failure": caught.value.evidence, "phases": attempt["phases"]}
    )
    assert "secret message" not in retained
    assert "/private/path" not in retained


@pytest.mark.parametrize(
    ("message", "cause_class"),
    [
        (
            "native Harbor production config mismatch",
            "NativeProductionConfigMismatch",
        ),
        ("native Harbor job outcome mismatch", "NativeJobOutcomeMismatch"),
        (
            "native Harbor trial directory count mismatch",
            "NativeTrialDirectoryMismatch",
        ),
        ("native Harbor trial evidence mismatch", "NativeTrialEvidenceMismatch"),
        ("native Harbor agent identity mismatch", "NativeAgentIdentityMismatch"),
        (
            "native artifact manifest provenance mismatch",
            "NativeArtifactManifestMismatch",
        ),
        (
            "production CLI binary reward summary mismatch",
            "NativeRewardSummaryMismatch",
        ),
        (
            "production OpenCode run omitted its ATIF trajectory",
            "NativeTrajectoryMissing",
        ),
        ("private model output /private/path", "NativeSnapshotValidationError"),
    ],
)
def test_native_record_failures_retain_only_closed_boundary(
    message: str, cause_class: str
) -> None:
    attempt = calibration._started_attempt(
        ordinal=1,
        profile="alternate",
        model_group="z-ai/glm-5.3-flash",
    )

    with pytest.raises(calibration.CalibrationStageError) as caught:
        calibration._run_phase(
            attempt,
            "native_validation",
            lambda: calibration._raise_classified_native_record_error(
                ValueError(message)
            ),
        )

    assert caught.value.evidence == {
        "cause_class": cause_class,
        "failed_stage": "native_validation",
    }
    retained = json.dumps(caught.value.evidence)
    assert "private model output" not in retained
    assert "/private/path" not in retained


@pytest.mark.parametrize(
    ("returncode", "kind", "cause_class"),
    [
        (0, "successful_early_exit", "EarlyCommandSuccess"),
        (7, "nonzero_return", "EarlyCommandNonzeroReturn"),
    ],
)
def test_early_command_result_is_captured_before_main_discovery_failure(
    tmp_path: Path, returncode: int, kind: str, cause_class: str
) -> None:
    attempt = calibration._started_attempt(
        ordinal=1,
        profile="target",
        model_group="openai/gpt-5.6-sol",
    )
    future: Future[calibration.CommandResult] = Future()
    future.set_result(
        calibration.CommandResult(
            returncode=returncode,
            stdout=b'{"schema_version":1,"outcome":"failed","reward":"0"}\n',
            stderr=b"private stderr bytes",
            containment={"survivors": 0},
        )
    )
    authority = calibration.DockerMainAuthority(
        calibration._docker_names("cal-early-result", 1),
        TEST_PARENT_KEY,
        tmp_path / "output",
        cast(Any, object()),
        "0" * 64,
    )
    with pytest.raises(calibration.CalibrationStageError) as caught:
        calibration._run_phase(
            attempt,
            "main_discovery",
            lambda: authority._discover_main(future, attempt),
        )
    assert caught.value.evidence == {
        "cause_class": cause_class,
        "failed_stage": "main_discovery",
    }
    outcome = attempt["command_outcome"]
    assert outcome["return_code"] == returncode
    assert outcome["completion"] == {
        "before_main_activation": True,
        "exception_class": None,
        "kind": kind,
    }
    assert outcome["containment"] == {"survivors": 0}
    assert "private stderr bytes" not in json.dumps(outcome)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError("private timeout"), "timeout_exception"),
        (ValueError("private output"), "output_exception"),
        (RuntimeError("private containment"), "containment_exception"),
    ],
)
def test_early_command_exception_class_is_captured_without_content(
    tmp_path: Path, error: BaseException, kind: str
) -> None:
    attempt = calibration._started_attempt(
        ordinal=1,
        profile="target",
        model_group="openai/gpt-5.6-sol",
    )
    future: Future[calibration.CommandResult] = Future()
    future.set_exception(error)
    authority = calibration.DockerMainAuthority(
        calibration._docker_names("cal-early-exception", 1),
        TEST_PARENT_KEY,
        tmp_path / "output",
        cast(Any, object()),
        "0" * 64,
    )
    with pytest.raises(calibration.CalibrationStageError) as caught:
        calibration._run_phase(
            attempt,
            "main_discovery",
            lambda: authority._discover_main(future, attempt),
        )
    assert caught.value.evidence == {
        "cause_class": type(error).__name__,
        "failed_stage": "main_discovery",
    }
    outcome = attempt["command_outcome"]
    assert outcome["status"] == "exception"
    assert outcome["completion"] == {
        "before_main_activation": True,
        "exception_class": type(error).__name__,
        "kind": kind,
    }
    assert str(error) not in json.dumps(outcome)


@pytest.mark.parametrize(
    ("failure_point", "ledger_mode", "exposure_authority"),
    [
        ("postactivation", "valid", "broker_ledger"),
        ("preactivation", "read_error", "preactivation_zero"),
        ("preactivation", "stale_preactivation", "broker_ledger"),
        ("preactivation", "preactivation_nonzero", "preactivation_zero"),
        ("postactivation", "missing", "conservative_allocation_fallback"),
        ("postactivation", "malformed", "conservative_allocation_fallback"),
        ("postactivation", "read_error", "conservative_allocation_fallback"),
        (
            "postactivation",
            "stale_preactivation",
            "conservative_allocation_fallback",
        ),
        (
            "postactivation",
            "wrong_attempt",
            "conservative_allocation_fallback",
        ),
        (
            "activation_token_write",
            "stale_preactivation",
            "conservative_allocation_fallback",
        ),
    ],
)
def test_run_attempt_captures_result_before_zero_request_ledger_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    ledger_mode: str,
    exposure_authority: str,
) -> None:
    pricing = calibration._validate_pricing_document(pricing_document())
    read_minimums: list[int] = []
    cleanup_calls = 0

    class Sidecar:
        def __init__(self, **kwargs: Any) -> None:
            self.attempt_token = kwargs["attempt_token"]
            self.activated = False

        def start(self) -> None:
            pass

        def probe(self) -> None:
            pass

        def activate(self) -> None:
            if failure_point == "activation_token_write":
                raise RuntimeError("private activation write failure")
            self.activated = True

        def cleanup(self) -> dict[str, Any]:
            nonlocal cleanup_calls
            cleanup_calls += 1
            return {"broker_absent": True, "network_absent": True}

        def read_ledger(self, *, minimum_requests: int = 0) -> dict[str, Any]:
            assert cleanup_calls == 1
            read_minimums.append(minimum_requests)
            if ledger_mode in {"missing", "read_error"}:
                raise OSError("private ledger read failure")
            if ledger_mode == "malformed":
                return {"known_actual_cost_usd": "not-a-decimal"}
            activated = ledger_mode != "stale_preactivation" and (
                failure_point == "postactivation"
            )
            return {
                "active": False,
                "activation_token_sha256": (
                    "0" * 64
                    if ledger_mode == "wrong_attempt"
                    else hashlib.sha256(self.attempt_token.encode()).hexdigest()
                    if activated
                    else None
                ),
                "activated": activated,
                "fatal": None,
                "known_actual_cost_usd": (
                    "0.25" if ledger_mode == "preactivation_nonzero" else "0"
                ),
                "locked_endpoint": None,
                "request_count": 0,
                "requests": [],
                "retained_unknown_reservation_usd": "0",
                "schema_version": 2,
            }

    class Authority:
        def __init__(self, *_args: Any) -> None:
            self.sidecar = _args[3]

        def wait_inspect_activate(
            self, future: Future[Any], attempt: dict[str, Any]
        ) -> dict[str, Any]:
            if failure_point == "preactivation":

                def fail_discovery() -> None:
                    future.result(timeout=5)
                    raise RuntimeError("private discovery failure")

                calibration._run_phase(attempt, "main_discovery", fail_discovery)
            for phase in ("main_discovery", "main_config_validation"):
                calibration._run_phase(attempt, phase, lambda: None)
            calibration._run_phase(attempt, "broker_activation", self.sidecar.activate)
            calibration._run_phase(attempt, "heartbeat_start", lambda: None)
            return {}

    def command(*_args: Any, **_kwargs: Any) -> calibration.CommandResult:
        return calibration.CommandResult(
            returncode=9,
            stdout=b'{"error":"private failure"}\n',
            stderr=b"private stderr",
            containment={"survivors": 0},
        )

    monkeypatch.setattr(calibration, "DockerBrokerSidecar", Sidecar)
    monkeypatch.setattr(calibration, "DockerMainAuthority", Authority)
    monkeypatch.setattr(calibration, "_bounded_command", command)
    monkeypatch.setattr(
        calibration, "_remove_owned_attempt_containers", lambda _n: None
    )
    snapshot = calibration.SourceSnapshot(
        root=ROOT,
        revision="0" * 40,
        source_state="dirty-debug",
        archive_sha256=None,
        mode="debug-worktree-copy",
    )
    attempt = calibration._started_attempt(
        ordinal=1,
        profile="target",
        model_group="openai/gpt-5.6-sol",
    )
    with pytest.raises(calibration.CalibrationStageError) as caught:
        calibration._run_attempt(
            ordinal=1,
            profile="target",
            model_group="openai/gpt-5.6-sol",
            installed_cli=calibration.InstalledCLI(
                executable=Path("unused"), python=Path("unused"), attestation={}
            ),
            snapshot=snapshot,
            private_root=tmp_path,
            run_id="cal-test",
            parent_key=TEST_PARENT_KEY,
            pricing=pricing,
            model_pricing=pricing.models[0],
            attempt_allocation=Decimal("1"),
            attempt_record=attempt,
        )
    assert caught.value.evidence == (
        {"cause_class": "RuntimeError", "failed_stage": "main_discovery"}
        if failure_point == "preactivation"
        else (
            {"cause_class": "RuntimeError", "failed_stage": "broker_activation"}
            if failure_point == "activation_token_write"
            else {"cause_class": "RuntimeError", "failed_stage": "cli_wait"}
        )
    )
    assert read_minimums == [0]
    assert cleanup_calls == 1
    assert attempt["broker"]["request_count"] == 0
    assert attempt["spend"]["exposure_authority"] == exposure_authority
    expected_retained = (
        "1" if exposure_authority == "conservative_allocation_fallback" else "0"
    )
    assert attempt["spend"]["retained_unknown_reservation_usd"] == expected_retained
    assert attempt["command_outcome"]["return_code"] == 9
    assert attempt["command_outcome"]["completion"]["before_main_activation"] is (
        failure_point != "postactivation"
    )
    assert "private" not in json.dumps(attempt)


def _docker_sidecar(
    tmp_path: Path,
    *,
    parent_key: str = TEST_PARENT_KEY,
    snapshot_root: Path = ROOT,
    backend: calibration.BackendContract = calibration.LITELLM_BACKEND,
) -> calibration.DockerBrokerSidecar:
    pricing = calibration._validate_pricing_document(pricing_document())
    names = calibration._docker_names("cal-test-sidecar", 1)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    return calibration.DockerBrokerSidecar(
        names=names,
        snapshot_root=snapshot_root,
        evidence_root=evidence,
        parent_key=parent_key,
        attempt_token="attempt-token-0123456789-abcdefghijklmnop",
        probe_token="probe-token-0123456789-abcdefghijklmnopqr",
        model="openai/gpt-5.6-sol",
        pricing=pricing,
        max_input_tokens=200000,
        budget_cap=Decimal("6"),
        backend=backend,
        fake_response_cost=Decimal("0.125"),
    )


def _archive_mounted_source(tmp_path: Path) -> Path:
    archive_path = tmp_path / "source.tar"
    member_name = "tools/run_authority_fencing_calibration.py"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(ROOT / member_name, arcname=member_name)
    source_root = tmp_path / "source"
    target = source_root / member_name
    target.parent.mkdir(parents=True)
    with tarfile.open(archive_path, "r") as archive:
        source = archive.extractfile(member_name)
        if source is None:
            raise RuntimeError("calibration source missing from archive")
        target.write_bytes(source.read())
    return source_root


def _main_inspect_document(
    sidecar: calibration.DockerBrokerSidecar, output: Path
) -> dict[str, Any]:
    labels = {
        **sidecar.names.role_labels("main"),
        "com.docker.compose.service": "main",
        "org.tetrabench.calibration.candidate-manifest": "0" * 64,
    }
    return {
        "Config": {
            "ExposedPorts": None,
            "Image": "python:3.12-alpine",
            "Labels": labels,
        },
        "HostConfig": {
            "CapAdd": None,
            "DeviceRequests": [],
            "Devices": [],
            "Dns": list(calibration.MAIN_DNS),
            "IpcMode": "private",
            "NetworkMode": sidecar.names.network,
            "PidMode": "",
            "PortBindings": {},
            "Privileged": False,
            "PublishAllPorts": False,
            "SecurityOpt": ["no-new-privileges"],
        },
        "Id": "main-container-id",
        "Image": "sha256:" + "1" * 64,
        "Mounts": [
            {
                "Destination": f"/logs/{name}",
                "Source": str(output / name),
                "Type": "bind",
            }
            for name in ("agent", "verifier", "artifacts")
        ],
        "NetworkSettings": {
            "Networks": {sidecar.names.network: {}},
            "Ports": {},
        },
    }


def _broker_inspect_documents(
    sidecar: calibration.DockerBrokerSidecar, dns: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    subnet = "10.224.0.0/28"
    gateway = "10.224.0.1"
    sidecar.network_allocation = calibration.DockerNetworkAllocation(
        subnet=subnet,
        candidate_probe=0,
        route_prefixes_checked=0,
        docker_subnets_checked=0,
        route_collision_rejections=0,
        docker_collision_rejections=0,
        create_overlap_retries=0,
    )
    inspected = {
        "Config": {"Labels": sidecar.names.role_labels("broker")},
        "HostConfig": {"Dns": dns},
        "Mounts": [
            {"Destination": "/source", "RW": False},
            {"Destination": "/evidence", "RW": True},
        ],
        "NetworkSettings": {"Networks": {sidecar.names.network: {"Gateway": gateway}}},
    }
    network = {
        "Driver": "bridge",
        "Internal": False,
        "IPAM": {"Config": [{"Gateway": gateway, "Subnet": subnet}]},
        "Labels": sidecar.names.role_labels("network"),
    }
    return inspected, network


@pytest.mark.parametrize(
    "dns",
    [None, [], ["1.1.1.1"], ["100.100.100.100", "1.1.1.1"]],
)
def test_broker_inspect_rejects_changed_resolver_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dns: Any
) -> None:
    sidecar = _docker_sidecar(tmp_path)
    inspected, network = _broker_inspect_documents(sidecar, dns)
    monkeypatch.setattr(
        calibration,
        "_inspect_one",
        lambda kind, _name: inspected if kind == "container" else network,
    )
    monkeypatch.setattr(
        calibration,
        "_docker",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    with pytest.raises(RuntimeError, match="broker resolver policy rejected"):
        sidecar.inspect_secret_boundary()


def test_broker_inspect_records_exact_resolver_policy_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _docker_sidecar(tmp_path)
    inspected, network = _broker_inspect_documents(
        sidecar, list(calibration.BROKER_DNS)
    )
    monkeypatch.setattr(
        calibration,
        "_inspect_one",
        lambda kind, _name: inspected if kind == "container" else network,
    )
    monkeypatch.setattr(
        calibration,
        "_docker",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    evidence = sidecar.inspect_secret_boundary()
    assert evidence["resolver_policy"] == {
        "addresses": ["100.100.100.100"],
        "purpose": "tailnet-upstream",
        "role": "broker",
    }
    assert sidecar.parent_key not in json.dumps(evidence)


def test_openrouter_broker_inspect_requires_public_resolvers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _docker_sidecar(tmp_path, backend=calibration.OPENROUTER_BACKEND)
    inspected, network = _broker_inspect_documents(
        sidecar, list(calibration.OPENROUTER_BACKEND.broker_dns)
    )
    monkeypatch.setattr(
        calibration,
        "_inspect_one",
        lambda kind, _name: inspected if kind == "container" else network,
    )
    monkeypatch.setattr(
        calibration,
        "_docker",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    evidence = sidecar.inspect_secret_boundary()
    assert evidence["resolver_policy"] == {
        "addresses": ["1.1.1.1", "9.9.9.9"],
        "purpose": "public-openrouter-upstream",
        "role": "broker",
    }


@pytest.mark.parametrize(
    "dns",
    [
        None,
        [],
        ["1.1.1.1"],
        ["9.9.9.9", "1.1.1.1"],
        ["1.1.1.1", "8.8.8.8"],
        ["1.1.1.1", "9.9.9.9", "8.8.8.8"],
    ],
)
def test_main_inspect_rejects_changed_resolver_policy(tmp_path: Path, dns: Any) -> None:
    sidecar = _docker_sidecar(tmp_path)
    output = tmp_path / "output"
    for name in ("agent", "verifier", "artifacts"):
        (output / name).mkdir(parents=True, exist_ok=True)
    inspected = _main_inspect_document(sidecar, output)
    inspected["HostConfig"]["Dns"] = dns
    authority = calibration.DockerMainAuthority(
        sidecar.names, sidecar.parent_key, output, sidecar, "0" * 64
    )
    with pytest.raises(RuntimeError, match="Harbor main resolver policy rejected"):
        authority._validate_main(inspected)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("Config", "ExposedPorts", {"62017/tcp": {}}),
        (
            "HostConfig",
            "PortBindings",
            {"62017/tcp": [{"HostIp": "127.0.0.1", "HostPort": "62017"}]},
        ),
        ("HostConfig", "PublishAllPorts", True),
        (
            "NetworkSettings",
            "Ports",
            {"62017/tcp": [{"HostIp": "0.0.0.0", "HostPort": "62017"}]},
        ),
    ],
)
def test_main_inspect_rejects_every_port_publication_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: Any,
) -> None:
    sidecar = _docker_sidecar(tmp_path)
    output = tmp_path / "output"
    for name in ("agent", "verifier", "artifacts"):
        (output / name).mkdir(parents=True, exist_ok=True)
    inspected = copy.deepcopy(_main_inspect_document(sidecar, output))
    inspected[section][field] = value
    monkeypatch.setattr(
        calibration,
        "_inspect_one",
        lambda kind, name: (
            {"Id": inspected["Image"]}
            if kind == "image" and name == inspected["Image"]
            else (_ for _ in ()).throw(AssertionError("unexpected inspect"))
        ),
    )
    authority = calibration.DockerMainAuthority(
        sidecar.names, sidecar.parent_key, output, sidecar, "0" * 64
    )
    with pytest.raises(RuntimeError, match="published port rejected"):
        authority._validate_main(inspected)


@pytest.mark.docker
def test_sidecar_network_probe_simulated_client_inspect_and_cleanup(
    tmp_path: Path,
) -> None:
    sidecar = _docker_sidecar(tmp_path, snapshot_root=_archive_mounted_source(tmp_path))
    try:
        sidecar.start()
        initial_ledger = sidecar.read_ledger()
        assert initial_ledger["probe_consumed"] is False
        assert initial_ledger["request_count"] == 0
        assert initial_ledger["requests"] == []
        sidecar.probe()
        security = sidecar.inspect_secret_boundary()
        assert security["parent_key_absent"] is True
        assert security["mounts"] == {"evidence_rw": True, "source_ro": True}
        assert security["resolver_policy"] == {
            "addresses": ["100.100.100.100"],
            "purpose": "tailnet-upstream",
            "role": "broker",
        }
        assert security["network"]["driver"] == "bridge"
        assert security["network"]["gateway"]
        assert security["network"]["allocation"]["subnet"].startswith("10.")
        assert security["network"]["allocation"]["route_prefixes_checked"] >= 0
        assert security["network"]["allocation"]["docker_subnets_checked"] >= 0
        assert security["network"]["internal"] is False
        assert security["network"]["labels"] == sidecar.names.role_labels("network")
        output = tmp_path / "output"
        for target in ("agent", "verifier", "artifacts"):
            (output / target).mkdir(parents=True, exist_ok=True)
        main = f"tb-main-{calibration.secrets.token_hex(6)}"
        labels = {
            **sidecar.names.role_labels("main"),
            "com.docker.compose.service": "main",
            "org.tetrabench.calibration.candidate-manifest": "0" * 64,
        }
        calibration._docker(
            [
                "create",
                "--name",
                main,
                "--network",
                sidecar.names.network,
                *[
                    value
                    for resolver in calibration.MAIN_DNS
                    for value in ("--dns", resolver)
                ],
                *[
                    value
                    for key, value in labels.items()
                    for value in ("--label", f"{key}={value}")
                ],
                "--mount",
                f"type=bind,src={output / 'agent'},dst=/logs/agent",
                "--mount",
                f"type=bind,src={output / 'verifier'},dst=/logs/verifier",
                "--mount",
                f"type=bind,src={output / 'artifacts'},dst=/logs/artifacts",
                calibration.BROKER_IMAGE,
                "sleep",
                "300",
            ]
        )
        calibration._docker(["start", main])
        pending: Future[calibration.CommandResult] = Future()
        attempt = calibration._started_attempt(
            ordinal=1,
            profile="target",
            model_group="openai/gpt-5.6-sol",
        )
        for phase in ("sidecar_start", "topology_probe", "cli_spawn"):
            calibration._run_phase(attempt, phase, lambda: None)
        activation = calibration.DockerMainAuthority(
            sidecar.names, sidecar.parent_key, output, sidecar, "0" * 64
        ).wait_inspect_activate(pending, attempt)
        assert activation["boundary"].startswith("immutable-docker-config")
        assert len(activation["config_sha256"]) == 64
        assert activation["activation"]["completed_before_harbor_healthcheck"] is True
        script = (
            "import json,sys,urllib.request;"
            "token=sys.stdin.read().strip();"
            "body=json.dumps({'model':'openai/gpt-5.6-sol','input':'fake'}).encode();"
            "request=urllib.request.Request('http://'+sys.argv[1]+':62017/v1/responses',"
            "data=body,headers={'Authorization':'Bearer '+token,"
            "'Content-Type':'application/json'});"
            "response=urllib.request.urlopen(request,timeout=10);"
            "print(response.status,response.read().decode())"
        )
        result = calibration._docker(
            [
                "run",
                "--rm",
                "-i",
                "--network",
                sidecar.names.network,
                "--read-only",
                "--cap-drop",
                "ALL",
                calibration.BROKER_IMAGE,
                "python",
                "-c",
                script,
                sidecar.names.alias,
            ],
            input_bytes=(sidecar.attempt_token + "\n").encode(),
        )
        assert result.stdout.strip() == b'200 {"ok":true}'
        ledger = sidecar.read_ledger()
        assert ledger["probe_consumed"] is True
        assert ledger["known_actual_cost_usd"] == "0.125"
        assert ledger["request_count"] == 1
        assert ledger["requests"][0]["model"] == "openai/gpt-5.6-sol"
        assert ledger["requests"][0]["cost"] == "0.125"
        network = calibration._inspect_one("network", sidecar.names.network)
        peers = {item["Name"] for item in network["Containers"].values()}
        assert sidecar.names.broker in peers
        assert main in peers
        outbound = calibration._docker(
            [
                "run",
                "--rm",
                "--network",
                sidecar.names.network,
                "--read-only",
                "--cap-drop",
                "ALL",
                calibration.BROKER_IMAGE,
                "python",
                "-c",
                (
                    "import http.client;"
                    "connection=http.client.HTTPSConnection('1.1.1.1',timeout=15);"
                    "connection.request('HEAD','/');"
                    "print(connection.getresponse().status);connection.close()"
                ),
            ],
            check=False,
        )
        assert outbound.returncode == 0, outbound.stderr.decode(errors="replace")
        assert outbound.stdout.strip() in {b"200", b"301", b"302"}
    finally:
        cleanup = sidecar.cleanup()
    assert cleanup == {
        "attach_client_reaped": True,
        "broker_absent": True,
        "network_absent": True,
        "stdin_closed": True,
        "tokens_expired": True,
    }


@pytest.mark.docker
def test_safe_attempt_subnet_resolves_public_package_host_with_approved_dns() -> None:
    names = calibration._docker_names(
        f"cal-public-dns-{calibration.secrets.token_hex(6)}", 1
    )
    allocation = calibration._create_attempt_network(names)
    try:
        result = calibration._docker(
            [
                "run",
                "--rm",
                "--network",
                names.network,
                *[
                    value
                    for resolver in calibration.MAIN_DNS
                    for value in ("--dns", resolver)
                ],
                "--read-only",
                "--cap-drop",
                "ALL",
                calibration.BROKER_IMAGE,
                "python",
                "-c",
                (
                    "import socket;"
                    "addresses=sorted({item[4][0] for item in "
                    "socket.getaddrinfo('deb.debian.org',443,type=socket.SOCK_STREAM)});"
                    "print('\\n'.join(addresses))"
                ),
            ],
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert result.stdout.strip()
        assert allocation.subnet.startswith("10.")
    finally:
        calibration._docker(["network", "rm", names.network], check=False)
        assert calibration._wait_resource_absent("network", names.network)


@pytest.mark.docker
def test_parent_sigkill_revokes_broker_and_startup_sweeps_stale_network(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "parent.py"
    helper.write_text(
        """import hashlib,json,sys,time
from decimal import Decimal
from pathlib import Path
sys.path.insert(0,ROOT_REPR)
from tools import run_authority_fencing_calibration as c
models=[]
for _profile, model in c.PROFILES:
    models.append({
            "cache_creation_input_token_cost":'0.00000375',
        'cache_read_input_token_cost':'3E-7',
        'input_cost_per_token':'0.000003',
        'max_input_tokens':200000,
        'max_output_tokens':65536,
        'model_group':model,
        'output_cost_per_token':'0.000015',
    })
pricing=c.PricingSnapshot(tuple(models),hashlib.sha256(c.canonical(models).encode()).hexdigest())
root=Path(__file__).parent
evidence=root/'evidence'; evidence.mkdir(mode=0o700)
names=c._docker_names('cal-parent-death',1)
sidecar=c.DockerBrokerSidecar(names=names,snapshot_root=c.ROOT,evidence_root=evidence,
 parent_key='sk-test-deployed-key-1234',
 attempt_token='attempt-token-0123456789-abcdefghijklmnop',
 probe_token='probe-token-0123456789-abcdefghijklmnopqr',
 model='openai/gpt-5.6-sol',pricing=pricing,max_input_tokens=200000,
 budget_cap=Decimal('1'),fake_response_cost=Decimal('0.125'))
sidecar.start(); sidecar.probe(); sidecar.activate()
print(json.dumps({
            "network":names.network,'broker':names.broker,'alias':names.alias,
 'token':sidecar.attempt_token}),flush=True)
time.sleep(300)
""".replace("ROOT_REPR", repr(str(ROOT)))
    )
    parent = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), str(helper)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    names: dict[str, str] = {}
    try:
        line = parent.stdout.readline()
        if not line:
            assert parent.stderr is not None
            pytest.fail(parent.stderr.read())
        names = json.loads(line)
        started = time.monotonic()
        parent.kill()
        parent.wait(timeout=5)
        while time.monotonic() - started < calibration.PARENT_DEATH_BOUND_SECONDS:
            if (
                calibration._docker(
                    ["container", "inspect", names["broker"]], check=False
                ).returncode
                != 0
            ):
                break
            time.sleep(0.05)
        assert time.monotonic() - started < calibration.PARENT_DEATH_BOUND_SECONDS
        assert (
            calibration._docker(
                ["container", "inspect", names["broker"]], check=False
            ).returncode
            != 0
        )
        stale = calibration._docker(
            [
                "run",
                "--rm",
                "-i",
                "--network",
                names["network"],
                calibration.BROKER_IMAGE,
                "python",
                "-c",
                (
                    "import json,sys,urllib.request;"
                    "token=sys.stdin.read().strip();"
                    "body=json.dumps({'model':'openai/gpt-5.6-sol','input':'x'}).encode();"
                    "request=urllib.request.Request('http://'+sys.argv[1]+"
                    "':62017/v1/responses',data=body,headers="
                    "{'Authorization':'Bearer '+token});"
                    "urllib.request.urlopen(request,timeout=1)"
                ),
                names["alias"],
            ],
            check=False,
            input_bytes=(names["token"] + "\n").encode(),
        )
        assert stale.returncode != 0
        swept = calibration.sweep_stale_calibration_resources()
        assert swept["networks"] >= 1
        assert (
            calibration._docker(
                ["network", "inspect", names["network"]], check=False
            ).returncode
            != 0
        )
    finally:
        parent.kill()
        parent.wait(timeout=5)
        if names:
            calibration._docker(["rm", "--force", names["broker"]], check=False)
            calibration._docker(["network", "rm", names["network"]], check=False)


@pytest.mark.docker
def test_harbor_main_is_inspected_and_activated_before_agent_setup(
    tmp_path: Path,
) -> None:
    sidecar = _docker_sidecar(tmp_path)
    overlay = calibration.create_task_overlay(
        calibration.TASK, tmp_path / "overlay", sidecar.names
    )
    project, config_root = calibration._write_project(
        tmp_path / "execution", overlay.task
    )
    config = config_root / "tetrabench/config.toml"
    config.write_text(
        config.read_text().replace('agent_name = "opencode"', 'agent_name = "oracle"')
    )
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    for name in ("tmp", "cache", "data", "state"):
        (home / name).mkdir(mode=0o700)
    output = tmp_path / "output"
    environment = calibration.child_environment(
        config_root=config_root,
        private_home=home,
        token=sidecar.attempt_token,
        base_url=f"http://{sidecar.names.alias}:{calibration.DEFAULT_BROKER_PORT}/v1",
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        sidecar.start()
        sidecar.probe()
        command: Future[calibration.CommandResult] = executor.submit(
            calibration._bounded_command,
            [
                str(ROOT / ".venv/bin/tetrabench"),
                "run",
                "systems-design",
                "--profile",
                "target",
                "--output",
                str(output),
                "--json",
            ],
            cwd=project,
            env=environment,
            timeout=180,
        )
        attempt = calibration._started_attempt(
            ordinal=1,
            profile="target",
            model_group="openai/gpt-5.6-sol",
        )
        for phase in ("sidecar_start", "topology_probe", "cli_spawn"):
            calibration._run_phase(attempt, phase, lambda: None)
        activation = calibration.DockerMainAuthority(
            sidecar.names,
            sidecar.parent_key,
            output,
            sidecar,
            overlay.candidate_manifest_sha256,
        ).wait_inspect_activate(command, attempt)
        result = command.result(timeout=180)
        document = json.loads(result.stdout)
        assert document["outcome"] == "succeeded"
        assert activation["activation"]["completed_before_harbor_healthcheck"] is True
        assert activation["resolver_policy"] == {
            "addresses": ["1.1.1.1", "9.9.9.9"],
            "purpose": "public-package-resolution",
            "role": "main",
        }
        assert activation["mount_targets"] == [
            "/logs/agent",
            "/logs/artifacts",
            "/logs/verifier",
        ]
        ledger = sidecar.read_ledger()
        assert ledger["request_count"] == 0
        assert ledger["requests"] == []
    finally:
        sidecar.cleanup()
        executor.shutdown(wait=True, cancel_futures=True)


def test_missing_parent_key_fails_without_output_or_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(calibration.PARENT_KEY_ENV, raising=False)
    result = calibration.main([])
    output = json.loads(capsys.readouterr().out)
    assert result == 1
    assert output["admissible"] is False
    assert output["attempt_count"] == 0
    assert output["failure"] == {
        "exception_class": "RuntimeError",
        "stage": "calibration_harness",
        "type": "unexpected_failure",
    }
    assert not any(
        thread.name == "calibration-broker" for thread in threading.enumerate()
    )


def test_startup_sweep_precedes_pricing_and_failure_starts_no_attempt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(calibration.PARENT_KEY_ENV, TEST_PARENT_KEY)
    sweep_calls = 0

    def sweep() -> dict[str, int]:
        nonlocal sweep_calls
        sweep_calls += 1
        return {"containers": 0, "networks": 0}

    monkeypatch.setattr(calibration, "sweep_stale_calibration_resources", sweep)
    monkeypatch.setattr(
        calibration,
        "query_model_pricing",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("pricing rejected")),
    )
    result = calibration.main(["--backend", "litellm", "--debug"])
    output = json.loads(capsys.readouterr().out)
    assert result == 1
    assert output["attempt_count"] == 0
    assert output["failure"] == {
        "exception_class": "ValueError",
        "stage": "calibration_harness",
        "type": "unexpected_failure",
    }
    assert sweep_calls == 1


def test_cleanup_failure_cannot_claim_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = _docker_sidecar(tmp_path)
    sidecar.created_container = True
    sidecar.created_network = True

    def fail(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0 if "inspect" in argv else 1,
            stdout=b"",
            stderr=b"",
        )

    monkeypatch.setattr(calibration, "_docker", fail)
    monkeypatch.setattr(calibration, "_owned_resource_exists", lambda *_args: True)
    with pytest.raises(RuntimeError, match="absence proof failed"):
        sidecar.cleanup()


def test_calibration_tool_is_source_only() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"/tools/run_authority_fencing_calibration.py"' in pyproject
    assert '"/tests/test_authority_fencing_calibration.py"' in pyproject


def test_failed_attempt_and_report_retain_bounded_identity_and_spend_exposure() -> None:
    attempt = calibration._started_attempt(
        ordinal=2,
        profile="target",
        model_group="openai/gpt-5.6-sol",
    )
    attempt["broker"] = {
        "locked_endpoint": "/v1/responses",
        "request_count": 1,
        "requests": [],
    }
    attempt["spend"] = {
        "known_actual_cost_usd": "0.5",
        "retained_unknown_reservation_usd": "1.25",
        "total_exposure_usd": "1.75",
    }
    calibration._mark_attempt_failed(attempt, ValueError("native validation failed"))
    args = calibration.parse_arguments(
        [
            "--debug",
            "--prior-known-cost-usd",
            "0.0166085",
            "--prior-unknown-exposure-usd",
            "0.96086",
        ]
    )
    report = calibration._failure(
        args, RuntimeError("attempt failed"), attempts=[attempt]
    )
    assert report["attempt_count"] == 1
    assert report["attempts"][0] == attempt
    assert attempt["admissible"] is False
    assert attempt["outcome"] == "failed"
    assert attempt["ordinal"] == 2
    assert attempt["profile"] == "target"
    assert attempt["model"] == "openai/gpt-5.6-sol"
    assert attempt["model_group"] == "openai/gpt-5.6-sol"
    assert attempt["broker"]["locked_endpoint"] == "/v1/responses"
    assert attempt["broker"]["request_count"] == 1
    assert "content" not in json.dumps(attempt).lower()
    assert report["spend_exposure"] == {
        "prior_known_cost_usd": "0.0166085",
        "prior_unknown_exposure_usd": "0.96086",
        "current_known_cost_usd": "0.5",
        "current_retained_exposure_usd": "1.25",
        "known_actual_cost_usd": "0.5166085",
        "retained_unknown_reservation_usd": "2.21086",
        "total_usd": "2.7274685",
    }
