from __future__ import annotations

import copy
import dataclasses
import http.client
import json
import shutil
import socket
import subprocess
import sys
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
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import run_authority_fencing_calibration as calibration  # noqa: E402


class FakeUpstreamServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    status: int
    body: bytes
    response_headers: list[tuple[str, str]]
    response_gate: threading.Event | None
    include_content_length: bool
    close_response: bool


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
        try:
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
        response = server.body
        self.send_response(server.status)
        for name, value in server.response_headers:
            self.send_header(name, value)
        if server.include_content_length:
            self.send_header("Content-Length", str(len(response)))
        if server.close_response:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
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
    include_content_length: bool = True,
    close_response: bool = False,
):
    server = FakeUpstreamServer(("127.0.0.1", 0), FakeUpstreamHandler)
    server.requests = []
    server.status = status
    server.body = body
    server.response_headers = headers or [
        ("Content-Type", "application/json"),
        ("X-Litellm-Response-Cost", "0.125"),
    ]
    server.response_gate = response_gate
    server.include_content_length = include_content_length
    server.close_response = close_response
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@contextmanager
def running_broker(
    upstream: FakeUpstreamServer,
    *,
    timeout: int = 30,
    ledger: calibration.SpendLedger | None = None,
    max_input_tokens: int = calibration.MAX_BODY_BYTES,
):
    broker = calibration.CalibrationBroker(
        host="127.0.0.1",
        port=0,
        parent_key="parent-only-secret",
        model="openai/gpt-5.6-sol",
        ledger=ledger or calibration.SpendLedger(),
        max_input_tokens=max_input_tokens,
        timeout=timeout,
        upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.stop()


@contextmanager
def running_probe_broker(probe_token: str = "probe-only-token"):
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
                    "max_output_tokens": 16384,
                },
            }
            for _profile, model in calibration.PROFILES
        ]
        + [{"model_name": "unrelated", "model_info": {"api_key": "secret"}}]
    }


def test_profiles_are_exact_ordered_and_candidate_only(tmp_path: Path) -> None:
    project, config_root = calibration._write_project(tmp_path, calibration.TASK)
    config = (config_root / "tetrabench/config.toml").read_text()
    assert calibration.PROFILES == (
        ("target", "openai/gpt-5.6-sol"),
        ("alternate", "anthropic/claude-sonnet-5"),
    )
    assert config.count("[profiles.") == 8
    assert 'model_name = "openai/openai/gpt-5.6-sol"' in config
    assert 'model_name = "openai/anthropic/claude-sonnet-5"' in config
    assert config.count('agent_name = "opencode"') == 2
    assert config.count("attempts = 1") == 2
    assert config.count("concurrency = 1") == 2
    catalog = (project / "benchmarks/catalog.toml").read_text()
    assert catalog.count('id = "authority-fencing"') == 1
    assert 'reward_policy = "binary"' in catalog


def test_authenticated_model_info_selects_exact_groups_and_redacts_snapshot() -> None:
    document = pricing_document()
    with fake_upstream(
        headers=[("Content-Type", "application/json")],
        body=json.dumps(document).encode(),
    ) as upstream:
        snapshot = calibration.query_model_pricing(
            parent_key="parent-secret",
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
    assert upstream.requests == [
        {"authorization": "Bearer parent-secret", "path": "/model/info"}
    ]
    assert [item["model_group"] for item in snapshot.models] == [
        model for _profile, model in calibration.PROFILES
    ]
    serialized = json.dumps(dataclasses.asdict(snapshot))
    assert "secret" not in serialized
    assert len(snapshot.sha256) == 64


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
            parent_key="parent-secret",
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
                parent_key="parent-secret",
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
                parent_key="parent-secret",
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
                parent_key="parent-secret",
                upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
            )
    with fake_upstream(body=b"not-json") as upstream:
        with pytest.raises(ValueError, match="invalid JSON"):
            calibration.query_model_pricing(
                parent_key="parent-secret",
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
        ("max_output_tokens", 8191),
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
    for field in (
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cached_tokens",
    ):
        invalid = json.loads(json.dumps(record))
        invalid["trajectory"]["final_metrics"][field] = None
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
        ("alternate", "openai/anthropic/claude-sonnet-5"),
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
        calibration.PARENT_KEY_ENV: "parent",
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
            assert observed["authorization"] == "Bearer parent-only-secret"
            assert observed["path"] == "/v1/responses"
            assert observed["body"]["model"] == "openai/gpt-5.6-sol"
            assert observed["body"]["max_output_tokens"] == 8192
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
            assert upstream.requests[0]["body"][field] == 8192


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


@pytest.mark.parametrize(
    ("stream", "expected_content_type"),
    [(False, "application/json"), (True, "text/event-stream")],
)
def test_child_content_type_is_derived_only_from_validated_stream_mode(
    stream: bool, expected_content_type: str
) -> None:
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
                    "stream": stream,
                },
            )
    assert status == 200
    assert returned_headers["content-type"] == expected_content_type
    assert "content-encoding" not in returned_headers
    assert returned_headers["content-length"] == str(len(b'{"ok":true}'))


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
        ({"document": {"model": "anthropic/claude-sonnet-5"}}, 400),
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
            monkeypatch.setattr(calibration, "MAX_BODY_BYTES", 8)
            status, _headers, _body = request(broker)
            assert status == 400
            monkeypatch.setattr(calibration, "MAX_BODY_BYTES", 512 << 10)
            monkeypatch.setattr(calibration, "MAX_HEADER_BYTES", 32)
            status, _headers, _body = request(broker, headers={"X-Oversized": "x" * 64})
            assert status == 400
            assert upstream.requests == []


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
    token = "probe-only-token"
    with running_probe_broker(token) as broker:
        assert probe_request(broker, "Bearer wrong-token") == 401
        assert broker.state.probe_consumed is False
        assert probe_request(broker, f"Bearer {token}") == 204
        assert broker.state.probe_consumed is True


def test_probe_missing_then_valid_preserves_one_shot_token() -> None:
    token = "probe-only-token"
    with running_probe_broker(token) as broker:
        assert probe_request(broker, None) == 401
        assert broker.state.probe_consumed is False
        assert probe_request(broker, f"Bearer {token}") == 204
        assert broker.state.probe_consumed is True


def test_concurrent_valid_probes_have_exactly_one_success() -> None:
    token = "probe-only-token"
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
    token = "probe-only-token"
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
    token = "probe-only-token"
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
    token = "probe-only-token"
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


def test_cost_cap_and_exact_reservations_are_atomic() -> None:
    ledger = calibration.SpendLedger()
    first = ledger.reserve(Decimal("20"))
    ledger.settle(first, Decimal("20"))
    second = ledger.reserve(Decimal("5"))
    with pytest.raises(RuntimeError, match="remaining budget"):
        ledger.reserve(Decimal("0.01"))
    ledger.settle(second, Decimal("5"))
    assert ledger.cost == Decimal("25")
    assert ledger.reserved == 0


def test_arbitrary_settlement_cannot_exceed_its_reservation_or_cap() -> None:
    ledger = calibration.SpendLedger()
    reservation = ledger.reserve(Decimal("1"))
    with pytest.raises(RuntimeError, match="exceeded exact reservation"):
        ledger.settle(reservation, Decimal("25"))
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
            outcomes.append(ledger.reserve(Decimal("13")))
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
    assert ledger.reserved == Decimal("13")


def test_reservation_failure_does_not_commit_endpoint_or_request_count() -> None:
    ledger = calibration.SpendLedger()
    ledger.reserve(calibration.MAX_TOTAL_COST)
    state = calibration.BrokerState(
        parent_key="parent",
        token="token",
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
        parent_key="parent",
        token="token",
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
            outcomes.append(state.begin_request("/v1/responses", Decimal("13")))
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
    assert ledger.reserved == Decimal("13")
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
            parent_key="parent",
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
            parent_key="parent",
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
        parent_key="parent",
        token=None,
        probe_token="probe-token",
        model="model",
        max_input_tokens=100,
        deadline=time.monotonic() + 30,
        ledger=calibration.SpendLedger(),
    )
    attempt_headers = probe_headers("attempt-token-0123456789-abcdefghijklmnop")
    with pytest.raises(PermissionError):
        state.authorize(attempt_headers)
    state.consume_probe(probe_headers("probe-token"))
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
        parent_key="parent",
        token="attempt-token-0123456789-abcdefghijklmnop",
        probe_token="probe-token",
        model="model",
        max_input_tokens=100,
        deadline=1_000,
        ledger=calibration.SpendLedger(),
        shutdown_event=shutdown,
    )
    state.last_heartbeat = 100 - calibration.HEARTBEAT_LEASE_SECONDS

    class Upstream:
        closed = False

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
    ) -> http.client.HTTPConnection:
        assert parsed.hostname is not None
        connection = GatedConnection(parsed.hostname, parsed.port, timeout=5)
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
            parent_key="parent",
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
        assert upstream.requests[0]["authorization"] == "Bearer parent"
        state.last_heartbeat = time.monotonic() - calibration.HEARTBEAT_LEASE_SECONDS
        with pytest.raises(PermissionError, match="heartbeat rejected"):
            state.heartbeat((1, 2))
        assert connection.sock is None
        assert connection.auto_open == 0
        assert state.upstreams == set()
        with pytest.raises(http.client.NotConnected):
            connection.request("POST", "/v1/responses", body=b"{}", headers={})
        assert len(upstream.requests) == 1


def test_expiry_during_established_inflight_request_settles_reserved_work() -> None:
    response_gate = threading.Event()
    ledger = calibration.SpendLedger()
    with fake_upstream(response_gate=response_gate) as upstream:
        with running_broker(upstream, ledger=ledger) as broker:
            outcome: list[tuple[int, dict[str, str], bytes] | BaseException] = []

            def invoke() -> None:
                try:
                    outcome.append(request(broker))
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
            response_gate.set()
            client.join(timeout=5)
            assert not client.is_alive()
            assert len(outcome) == 1
            assert len(upstream.requests) == 1
            assert ledger.cost == Decimal("0.125")
            assert ledger.reserved == 0
            assert ledger.fatal is None
            deadline = time.monotonic() + 5
            while not broker.state.records and time.monotonic() < deadline:
                time.sleep(0.01)
            assert broker.state.records[0].settlement == "settled"
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
        parent_key="parent",
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

    def docker(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if argv[:2] == ["network", "create"]:
            exists[sidecar.names.network] = True
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv and argv[0] == "create":
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
    ],
)
def test_parser_rejects_noncanonical_attempt_or_listener_contract(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        calibration.parse_arguments(argv)
    assert error.value.code == 2


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


def _docker_sidecar(
    tmp_path: Path,
    *,
    parent_key: str = "parent-secret-never-in-docker-config-0123456789",
) -> calibration.DockerBrokerSidecar:
    pricing = calibration._validate_pricing_document(pricing_document())
    names = calibration._docker_names("cal-test-sidecar", 1)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    return calibration.DockerBrokerSidecar(
        names=names,
        snapshot_root=ROOT,
        evidence_root=evidence,
        parent_key=parent_key,
        attempt_token="attempt-token-0123456789-abcdefghijklmnop",
        probe_token="probe-token-0123456789-abcdefghijklmnopqr",
        model="openai/gpt-5.6-sol",
        pricing=pricing,
        max_input_tokens=200000,
        budget_cap=Decimal("1"),
        fake_response_cost=Decimal("0.125"),
    )


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
    sidecar = _docker_sidecar(tmp_path)
    try:
        sidecar.start()
        sidecar.probe()
        security = sidecar.inspect_secret_boundary()
        assert security["config_parent_key_absent"] is True
        assert security["logs_parent_key_absent"] is True
        assert security["mounts"] == {"evidence_rw": True, "source_ro": True}
        assert security["network"]["driver"] == "bridge"
        assert security["network"]["gateway"]
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
        activation = calibration.DockerMainAuthority(
            sidecar.names, sidecar.parent_key, output, sidecar, "0" * 64
        ).wait_inspect_activate(pending)
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
        'max_output_tokens':16384,
        'model_group':model,
        'output_cost_per_token':'0.000015',
    })
pricing=c.PricingSnapshot(tuple(models),hashlib.sha256(c.canonical(models).encode()).hexdigest())
root=Path(__file__).parent
evidence=root/'evidence'; evidence.mkdir(mode=0o700)
names=c._docker_names('cal-parent-death',1)
sidecar=c.DockerBrokerSidecar(names=names,snapshot_root=c.ROOT,evidence_root=evidence,
 parent_key='parent-secret-never-in-docker-config-0123456789',
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
        activation = calibration.DockerMainAuthority(
            sidecar.names,
            sidecar.parent_key,
            output,
            sidecar,
            overlay.candidate_manifest_sha256,
        ).wait_inspect_activate(command)
        result = command.result(timeout=180)
        document = json.loads(result.stdout)
        assert document["outcome"] == "succeeded"
        assert activation["activation"]["completed_before_harbor_healthcheck"] is True
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
    assert output["error"] == (
        "RuntimeError: calibration gateway key environment is required"
    )
    assert not any(
        thread.name == "calibration-broker" for thread in threading.enumerate()
    )


def test_startup_sweep_precedes_pricing_and_failure_starts_no_attempt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(calibration.PARENT_KEY_ENV, "fake-parent")
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
    result = calibration.main(["--debug"])
    output = json.loads(capsys.readouterr().out)
    assert result == 1
    assert output["attempt_count"] == 0
    assert output["error"] == "ValueError: pricing rejected"
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
    args = calibration.parse_arguments(["--debug"])
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
        "known_actual_cost_usd": "0.5",
        "retained_unknown_reservation_usd": "1.25",
        "total_usd": "1.75",
    }
