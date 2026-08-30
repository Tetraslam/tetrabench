from __future__ import annotations

import dataclasses
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

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
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


@contextmanager
def fake_upstream(
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b'{"ok":true}',
    status: int = 200,
    response_gate: threading.Event | None = None,
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
                document={"model": "openai/gpt-5.6-sol", "stream": stream},
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


def test_bridge_probe_succeeds_through_substituted_connector_without_upstream() -> None:
    topology = calibration.DockerBridgeTopology(
        gateway="127.0.0.1",
        interface="test-bridge",
        subnet="127.0.0.0/8",
    )

    def connector(host: str, port: int, token: str) -> subprocess.CompletedProcess[str]:
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(
            "GET",
            "/__tetrabench_probe",
            headers={"Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{response.status}\n", stderr=""
        )

    evidence = calibration.probe_docker_bridge_reachability(
        topology=topology,
        port=calibration.DEFAULT_BROKER_PORT,
        connector=connector,
    )
    assert evidence == {
        "gateway": "127.0.0.1",
        "interface": "test-bridge",
        "port": calibration.DEFAULT_BROKER_PORT,
        "reachable": True,
        "subnet": "127.0.0.0/8",
        "temporary_ufw_commands": {
            "add": (
                "sudo ufw allow in on test-bridge from 127.0.0.0/8 "
                "to 127.0.0.1 port 62017 proto tcp"
            ),
            "delete": (
                "sudo ufw delete allow in on test-bridge from 127.0.0.0/8 "
                "to 127.0.0.1 port 62017 proto tcp"
            ),
            "removal_required_in_finally": True,
        },
    }


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
            body = b'{"model":"openai/gpt-5.6-sol"}'
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


def test_docker_bridge_discovery_fails_closed_for_rootless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout='["name=rootless"]\n', stderr=""
        )

    monkeypatch.setattr(calibration.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="rootless"):
        calibration.discover_docker_bridge_gateway()


@pytest.mark.docker
def test_default_bridge_probe_is_no_upstream_and_blockage_fails_closed() -> None:
    topology = calibration.discover_docker_bridge_topology()
    try:
        evidence = calibration.probe_docker_bridge_reachability(
            topology=topology,
            port=calibration.DEFAULT_BROKER_PORT,
        )
    except RuntimeError as error:
        assert str(error) == "Docker bridge broker reachability probe failed"
    else:
        assert evidence == {
            "gateway": topology.gateway,
            "interface": topology.interface,
            "port": calibration.DEFAULT_BROKER_PORT,
            "reachable": True,
            "subnet": topology.subnet,
            "temporary_ufw_commands": calibration._topology_evidence(
                topology,
                port=calibration.DEFAULT_BROKER_PORT,
                reachable=True,
            )["temporary_ufw_commands"],
        }


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


def test_pricing_preflight_failure_occurs_after_topology_probe_without_attempt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(calibration.PARENT_KEY_ENV, "fake-parent")
    monkeypatch.setattr(
        calibration,
        "discover_docker_bridge_topology",
        lambda: calibration.DockerBridgeTopology(
            gateway="192.0.2.1", interface="docker0", subnet="192.0.2.0/24"
        ),
    )
    monkeypatch.setattr(
        calibration,
        "probe_docker_bridge_reachability",
        lambda **_kwargs: {
            "gateway": "192.0.2.1",
            "interface": "docker0",
            "port": calibration.DEFAULT_BROKER_PORT,
            "subnet": "192.0.2.0/24",
        },
    )
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
    assert not any(
        thread.name == "calibration-broker" for thread in threading.enumerate()
    )


def test_blocked_topology_stops_before_authenticated_pricing_or_model_upstream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(calibration.PARENT_KEY_ENV, "fake-parent")
    monkeypatch.setattr(
        calibration,
        "discover_docker_bridge_topology",
        lambda: calibration.DockerBridgeTopology(
            gateway="192.0.2.1", interface="docker0", subnet="192.0.2.0/24"
        ),
    )
    monkeypatch.setattr(
        calibration,
        "probe_docker_bridge_reachability",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Docker bridge broker reachability probe failed")
        ),
    )
    pricing_calls = 0

    def query(**_kwargs: Any) -> calibration.PricingSnapshot:
        nonlocal pricing_calls
        pricing_calls += 1
        raise AssertionError("authenticated upstream must not run")

    monkeypatch.setattr(calibration, "query_model_pricing", query)
    result = calibration.main([])
    output = json.loads(capsys.readouterr().out)
    assert result == 1
    assert pricing_calls == 0
    assert output["admissible"] is False
    assert output["attempt_count"] == 0
    assert output["pricing"] is None
    assert output["total_authoritative_cost_usd"] == "0"
    assert output["spend_exposure"] == {
        "known_actual_cost_usd": "0",
        "retained_unknown_reservation_usd": "0",
        "total_usd": "0",
    }
    assert output["topology"] == {
        "gateway": "192.0.2.1",
        "interface": "docker0",
        "port": calibration.DEFAULT_BROKER_PORT,
        "reachable": False,
        "subnet": "192.0.2.0/24",
        "temporary_ufw_commands": {
            "add": (
                "sudo ufw allow in on docker0 from 192.0.2.0/24 to 192.0.2.1 "
                "port 62017 proto tcp"
            ),
            "delete": (
                "sudo ufw delete allow in on docker0 from 192.0.2.0/24 "
                "to 192.0.2.1 port 62017 proto tcp"
            ),
            "removal_required_in_finally": True,
        },
    }
    assert "fake-parent" not in json.dumps(output)
    assert output["error"] == (
        "RuntimeError: Docker bridge broker reachability probe failed"
    )


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
