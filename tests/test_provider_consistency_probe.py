from __future__ import annotations

import runpy
import threading
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from tetrabench.models import AwsStorageConfig, TigrisStorageConfig
from tetrabench.s3 import UnsafeCoordinationTopologyError

probe_module = runpy.run_path(
    str(Path(__file__).parents[1] / "tools/provider_consistency_probe.py")
)
ProbeFailure = probe_module["ProbeFailure"]
run_probe = probe_module["run_probe"]


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _Backend:
    def __init__(self, location: object) -> None:
        self.location = location
        self.etag: str | None = None
        self.body: bytes | None = None
        self.lock = threading.Lock()
        self.clients: list[_Client] = []
        self.race_client_ids: set[int] = set()
        self.deleted = False
        self.ambiguous_create = False
        self.preexisting_key = False
        self.delete_error: Exception | None = None
        self.linger_after_delete = False

    def client(self) -> _Client:
        client = _Client(self)
        self.clients.append(client)
        return client


class _Client:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def get_bucket_location(self, **_kwargs: Any) -> dict[str, object]:
        return {"LocationConstraint": self.backend.location}

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        if kwargs.get("IfNoneMatch") == "*":
            with self.backend.lock:
                if self.backend.preexisting_key:
                    raise _client_error("PreconditionFailed", 412, "PutObject")
                self.backend.etag = '"created"'
                self.backend.body = kwargs["Body"]
            if self.backend.ambiguous_create:
                raise TimeoutError("create response timed out")
            return {"ETag": '"created"'}
        if kwargs.get("IfMatch") != '"created"':
            raise AssertionError("probe did not reuse the create ETag")
        self.backend.race_client_ids.add(id(self))
        with self.backend.lock:
            if self.backend.etag == '"updated"':
                raise _client_error("PreconditionFailed", 412, "PutObject")
            self.backend.etag = '"updated"'
            self.backend.body = kwargs["Body"]
        return {"ETag": '"updated"'}

    def delete_object(self, **_kwargs: Any) -> None:
        self.backend.deleted = True
        if not self.backend.linger_after_delete:
            self.backend.body = None
            self.backend.etag = None
        if self.backend.delete_error is not None:
            raise self.backend.delete_error

    def head_object(self, **_kwargs: Any) -> dict[str, str]:
        if self.backend.body is None:
            raise _client_error("NotFound", 404, "HeadObject")
        return {"ETag": self.backend.etag or '"unknown"'}

    def get_object(self, **_kwargs: Any) -> dict[str, bytes]:
        if self.backend.body is None:
            raise _client_error("NoSuchKey", 404, "GetObject")
        return {"Body": self.backend.body}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, object]:
        if self.backend.body is None:
            return {}
        return {"Contents": [{"Key": kwargs["Prefix"]}]}


@pytest.mark.parametrize(
    ("config", "location"),
    [
        (
            AwsStorageConfig(provider="aws", bucket="bucket", region="us-west-2"),
            "us-west-2",
        ),
        (TigrisStorageConfig(provider="tigris", bucket="bucket"), "iad"),
    ],
)
def test_probe_uses_synchronized_separate_clients_and_cleans_up(
    config: AwsStorageConfig | TigrisStorageConfig,
    location: str,
) -> None:
    backend = _Backend(location)

    report = run_probe(config, backend.client, sleep=lambda _delay: None)

    assert report["conditional_create"] == "passed"
    assert report["conditional_update_race"] == "passed"
    assert report["immediate_get"] == "passed"
    assert report["immediate_head"] == "passed"
    assert report["immediate_list"] == "passed"
    assert "key" not in report
    assert backend.deleted
    assert backend.body is None
    assert len(backend.race_client_ids) == 2


def test_ambiguous_create_still_deletes_and_preserves_original_error() -> None:
    backend = _Backend("iad")
    backend.ambiguous_create = True

    with pytest.raises(ProbeFailure) as raised:
        run_probe(
            TigrisStorageConfig(provider="tigris", bucket="bucket"),
            backend.client,
            sleep=lambda _delay: None,
        )

    assert isinstance(raised.value.original_error, TimeoutError)
    assert raised.value.cleanup_error is None
    assert backend.deleted
    assert backend.body is None


def test_preexisting_probe_key_is_not_deleted_after_create_precondition() -> None:
    backend = _Backend("iad")
    backend.preexisting_key = True
    backend.etag = '"preexisting"'
    backend.body = b"do-not-delete"

    with pytest.raises(ProbeFailure) as raised:
        run_probe(
            TigrisStorageConfig(provider="tigris", bucket="bucket"),
            backend.client,
            sleep=lambda _delay: None,
        )

    assert isinstance(raised.value.original_error, ClientError)
    assert raised.value.cleanup_error is None
    assert not backend.deleted
    assert backend.body == b"do-not-delete"


def test_delete_failure_is_reported_as_cleanup_error() -> None:
    backend = _Backend("iad")
    backend.delete_error = RuntimeError("delete failed")

    with pytest.raises(ProbeFailure) as raised:
        run_probe(
            TigrisStorageConfig(provider="tigris", bucket="bucket"),
            backend.client,
            sleep=lambda _delay: None,
        )

    assert raised.value.original_error is None
    assert raised.value.cleanup_error is backend.delete_error
    assert backend.deleted


def test_lingering_probe_object_fails_bounded_absence_verification() -> None:
    backend = _Backend("iad")
    backend.linger_after_delete = True
    sleeps: list[float] = []

    with pytest.raises(ProbeFailure) as raised:
        run_probe(
            TigrisStorageConfig(provider="tigris", bucket="bucket"),
            backend.client,
            cleanup_verification_attempts=3,
            cleanup_delay_seconds=0.25,
            sleep=sleeps.append,
        )

    assert raised.value.original_error is None
    assert isinstance(raised.value.cleanup_error, RuntimeError)
    assert "remained visible" in str(raised.value.cleanup_error)
    assert sleeps == [0.25, 0.25]


def test_probe_preserves_original_and_cleanup_errors_separately() -> None:
    backend = _Backend("iad")
    backend.ambiguous_create = True
    backend.delete_error = RuntimeError("delete failed")

    with pytest.raises(ProbeFailure) as raised:
        run_probe(
            TigrisStorageConfig(provider="tigris", bucket="bucket"),
            backend.client,
            sleep=lambda _delay: None,
        )

    assert isinstance(raised.value.original_error, TimeoutError)
    assert raised.value.cleanup_error is backend.delete_error


def test_unsafe_topology_performs_no_create_or_cleanup_mutation() -> None:
    backend = _Backend("global")

    with pytest.raises(UnsafeCoordinationTopologyError, match="Global"):
        run_probe(
            TigrisStorageConfig(provider="tigris", bucket="bucket"),
            backend.client,
            sleep=lambda _delay: None,
        )

    assert len(backend.clients) == 1
    assert not backend.deleted
    assert backend.body is None
