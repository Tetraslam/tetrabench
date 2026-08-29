"""Verified immutable S3 transport and publication."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from tetrabench.canonical_json import MAX_CANONICAL_JSON_BYTES, sha256_hex
from tetrabench.models import (
    AwsStorageConfig,
    ResolvedAwsStorageConfig,
    ResolvedStorageConfig,
    ResolvedTigrisStorageConfig,
    TigrisStorageConfig,
)
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.records import (
    AdmissionRecord,
    AttemptEvent,
    ConflictRunState,
    ContentObject,
    RequestRecord,
    RunReadState,
    TerminalRecord,
    interpret_terminal_records,
    validate_run_id,
)
from tetrabench.storage import (
    admission_key,
    content_object_key,
    event_key,
    request_key,
    terminal_key,
    validate_s3_key,
)

type StorageConfig = (
    AwsStorageConfig
    | TigrisStorageConfig
    | ResolvedAwsStorageConfig
    | ResolvedTigrisStorageConfig
)

_SHA256_METADATA_KEY = "sha256"
_JSON_MEDIA_TYPE = "application/json"
_READ_CHUNK_SIZE = 1024 * 1024
DEFAULT_MULTIPART_THRESHOLD = 64 * 1024 * 1024
DEFAULT_MULTIPART_CHUNK_SIZE = 16 * 1024 * 1024


class S3Client(Protocol):
    """The boto3 S3 client surface used by ``S3Store``."""

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def upload_fileobj(self, **kwargs: Any) -> None: ...

    def head_bucket(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def list_objects_v2(self, **kwargs: Any) -> Mapping[str, Any]: ...


class S3IntegrityError(RuntimeError):
    """Stored object identity does not match its immutable key or descriptor."""


class S3ConflictError(RuntimeError):
    """An immutable run record conflicts with visible durable state."""


class S3CasConflictError(RuntimeError):
    """A conditional admission write lost to another writer."""


@dataclass(frozen=True, slots=True)
class AdmissionRead:
    """One admission body paired with the opaque ETag that read it."""

    record: AdmissionRecord
    etag: str


def create_s3_client(config: StorageConfig) -> S3Client:
    """Construct a boto3 client without resolving or serializing credentials."""
    kwargs: dict[str, Any] = {"region_name": config.region}
    if config.provider == "tigris":
        kwargs.update(
            endpoint_url="https://t3.storage.dev",
            config=Config(s3={"addressing_style": "virtual"}),
        )
    return cast(S3Client, boto3.client("s3", **kwargs))


def create_s3_store(config: StorageConfig) -> S3Store:
    """Construct a store using boto3's ambient credential provider chain."""
    return S3Store(config, create_s3_client(config))


def _checksum_sha256(digest: str) -> str:
    return base64.b64encode(bytes.fromhex(digest)).decode("ascii")


def _is_not_found(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def _is_cas_conflict(error: ClientError, *, allow_not_found: bool) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    conditional = code in {
        "409",
        "412",
        "ConditionalRequestConflict",
        "PreconditionFailed",
    } or status in {409, 412}
    not_found = code in {"404", "NoSuchKey", "NotFound"} or status == 404
    return conditional or (allow_not_found and not_found)


def _exponential_backoff(retry: int, delay_seconds: float) -> float:
    return delay_seconds * (2**retry)


def _file_identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_ctime_ns,
        stat.st_mtime_ns,
    )


def _hash_stream(stream: BinaryIO, chunk_size: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(chunk_size):
        if not isinstance(chunk, bytes):
            raise TypeError("content file did not return bytes")
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


@dataclass
class _RunObservation:
    requests: dict[str, RequestRecord] = field(default_factory=dict)
    request_digests: set[str] = field(default_factory=set)
    events: dict[tuple[str, int], dict[str, AttemptEvent]] = field(default_factory=dict)
    event_hashes: dict[tuple[str, int], set[str]] = field(default_factory=dict)
    terminals: dict[str, TerminalRecord] = field(default_factory=dict)
    terminal_digests: set[str] = field(default_factory=set)
    invalid: dict[str, str] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)

    def conflict_reasons(self) -> tuple[str, ...]:
        reasons = [
            f"invalid immutable record {key}: {reason}"
            for key, reason in sorted(self.invalid.items())
        ]
        reasons.extend(
            f"immutable record body remained unavailable: {key}"
            for key in sorted(self.pending)
        )
        if len(self.request_digests) > 1:
            reasons.append("multiple request digests are visible for the run")
        for (attempt_id, sequence), hashes in sorted(self.event_hashes.items()):
            if len(hashes) > 1:
                reasons.append(
                    "multiple event hashes are visible for "
                    f"attempt {attempt_id!r} sequence {sequence}"
                )
        if len(self.terminal_digests) > 1:
            reasons.append("multiple valid terminal records are visible")
        return tuple(reasons)


class S3Store:
    """One provider-neutral immutable S3 store around an injected client.

    Every observation is bounded. A successful publication or read describes
    records visible during that window; a lagged conflicting record can make a
    later observation conflict.
    """

    def __init__(
        self,
        config: StorageConfig,
        client: S3Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        backoff: Callable[[int, float], float] = _exponential_backoff,
        verification_attempts: int = 3,
        verification_delay_seconds: float = 0.1,
        multipart_threshold: int = DEFAULT_MULTIPART_THRESHOLD,
        multipart_chunk_size: int = DEFAULT_MULTIPART_CHUNK_SIZE,
    ) -> None:
        if verification_attempts <= 0:
            raise ValueError("verification_attempts must be positive")
        if verification_delay_seconds < 0:
            raise ValueError("verification_delay_seconds must be non-negative")
        if multipart_threshold <= 0 or multipart_chunk_size <= 0:
            raise ValueError("multipart sizes must be positive")
        self._client = client
        self._bucket = config.bucket
        self._prefix = config.prefix
        if config.provider == "aws":
            self._storage = ResolvedAwsStorageConfig.model_validate(
                config.model_dump(mode="python")
            )
        else:
            self._storage = ResolvedTigrisStorageConfig.model_validate(
                config.model_dump(mode="python")
            )
        self._sleep = sleep
        self._backoff = backoff
        self._verification_attempts = verification_attempts
        self._verification_delay_seconds = verification_delay_seconds
        self._multipart_threshold = multipart_threshold
        self._transfer_config = TransferConfig(
            multipart_threshold=multipart_threshold,
            multipart_chunksize=multipart_chunk_size,
            max_concurrency=4,
            use_threads=True,
        )

    @property
    def storage(self) -> ResolvedStorageConfig:
        """Return the resolved storage identity used for all object keys."""
        return self._storage

    def check_read_access(self) -> None:
        """Check bucket and namespace access without mutating provider state."""
        self._client.head_bucket(Bucket=self._bucket)
        namespace = f"{self._prefix}/" if self._prefix else ""
        self._client.list_objects_v2(
            Bucket=self._bucket,
            Prefix=namespace,
            MaxKeys=1,
        )

    def publish_content(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ContentObject:
        """Publish verified in-memory bytes under their content digest."""
        if not isinstance(data, bytes):
            raise TypeError("content data must be bytes")
        digest = sha256_hex(data)
        descriptor = ContentObject(
            sha256=digest,
            key=content_object_key(digest, prefix=self._prefix),
            size=len(data),
            media_type=media_type,
        )
        self._publish_stream(
            descriptor.key,
            data,
            sha256=descriptor.sha256,
            size=descriptor.size,
            media_type=descriptor.media_type,
        )
        return descriptor

    def publish_content_file(
        self,
        path: Path,
        *,
        media_type: str = "application/octet-stream",
        chunk_size: int = _READ_CHUNK_SIZE,
    ) -> ContentObject:
        """Hash and upload a file with bounded reads and managed multipart."""
        with path.open("rb") as stream:
            return self.publish_content_stream(
                stream,
                media_type=media_type,
                chunk_size=chunk_size,
            )

    def publish_content_stream(
        self,
        stream: BinaryIO,
        *,
        media_type: str = "application/octet-stream",
        chunk_size: int = _READ_CHUNK_SIZE,
    ) -> ContentObject:
        """Publish a held regular-file descriptor without reopening its path."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("content descriptor is not a regular file")
        stream.seek(0)
        sha256, size = _hash_stream(stream, chunk_size)
        after_hash = os.fstat(stream.fileno())
        if (
            _file_identity(before) != _file_identity(after_hash)
            or size != before.st_size
        ):
            raise ValueError("content file changed while hashing")
        descriptor = ContentObject(
            sha256=sha256,
            key=content_object_key(sha256, prefix=self._prefix),
            size=size,
            media_type=media_type,
        )
        stream.seek(0)
        if size >= self._multipart_threshold:
            self._publish_managed_file(stream, descriptor)
        else:
            self._publish_stream(
                descriptor.key,
                stream,
                sha256=descriptor.sha256,
                size=descriptor.size,
                media_type=descriptor.media_type,
            )
        after_upload = os.fstat(stream.fileno())
        if _file_identity(after_hash) != _file_identity(after_upload):
            raise ValueError("content file changed while uploading")
        return descriptor

    def verify_content(self, descriptor: ContentObject) -> None:
        """GET-stream and rehash a content object against its descriptor."""
        expected_key = content_object_key(descriptor.sha256, prefix=self._prefix)
        if descriptor.key != expected_key:
            raise S3IntegrityError("content object is outside the configured namespace")
        self._read_verified_object(
            descriptor.key,
            sha256=descriptor.sha256,
            size=descriptor.size,
            media_type=descriptor.media_type,
            exact_service_checksum=False,
            collect=False,
        )

    def read_content(self, descriptor: ContentObject) -> bytes:
        """Return verified content bytes for bounded controller materialization."""
        expected_key = content_object_key(descriptor.sha256, prefix=self._prefix)
        if descriptor.key != expected_key:
            raise S3IntegrityError("content object is outside the configured namespace")
        data = self._read_verified_object(
            descriptor.key,
            sha256=descriptor.sha256,
            size=descriptor.size,
            media_type=descriptor.media_type,
            max_size=descriptor.size,
            exact_service_checksum=False,
        )
        assert isinstance(data, bytes)
        return data

    def publish_request(self, request: RequestRecord) -> str:
        """Publish first, then discover visible request conflicts boundedly."""
        for item in request.context_manifest.files:
            self.verify_content(item.content)
        data = canonical_model_bytes(request)
        digest = sha256_hex(data)
        self._publish_stream(
            request_key(request.run_id, digest, prefix=self._prefix),
            data,
            sha256=digest,
            size=len(data),
            media_type=_JSON_MEDIA_TYPE,
        )
        self._raise_visible_run_conflict(
            request.run_id,
            known_request_digests=(digest,),
        )
        return digest

    def read_request(
        self, run_id: str, request_sha256: str, request_object_key: str
    ) -> RequestRecord:
        """Fetch and validate one immutable request by its complete identity."""
        run_id = validate_run_id(run_id)
        expected_key = request_key(run_id, request_sha256, prefix=self._prefix)
        if request_object_key != expected_key:
            raise S3IntegrityError("request key does not match the requested identity")
        data = self._read_record_bytes(expected_key, request_sha256)
        request = parse_canonical_model(data, RequestRecord)
        if request.run_id != run_id:
            raise S3IntegrityError("request body run ID does not match its key")
        for item in request.context_manifest.files:
            self.verify_content(item.content)
        return request

    def create_admission(self, admission: AdmissionRecord) -> AdmissionRead:
        """Create the fixed coordination record only when the key is absent."""
        if admission.revision != 0 or admission.state != "prepared":
            raise ValueError("admission creation requires revision-zero prepared state")
        return self._put_admission(admission, IfNoneMatch="*")

    def read_admission(self, run_id: str) -> AdmissionRead | None:
        """Read and validate the fixed admission record with its opaque ETag."""
        run_id = validate_run_id(run_id)
        key = admission_key(run_id, prefix=self._prefix)
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except ClientError as error:
            if _is_not_found(error):
                return None
            raise
        body = response.get("Body")
        if not hasattr(body, "read"):
            raise S3IntegrityError("admission body is not readable")
        try:
            data = body.read(MAX_CANONICAL_JSON_BYTES + 1)
            if not isinstance(data, bytes):
                raise S3IntegrityError("admission body did not return bytes")
            if body.read(1):
                raise S3IntegrityError("admission body is oversized")
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if len(data) > MAX_CANONICAL_JSON_BYTES:
            raise S3IntegrityError("admission body is oversized")
        digest = sha256_hex(data)
        self._validate_object_response(
            response,
            key=key,
            sha256=digest,
            size=len(data),
            media_type=_JSON_MEDIA_TYPE,
            exact_service_checksum=True,
        )
        etag = response.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise S3IntegrityError("admission read omitted its ETag")
        record = parse_canonical_model(data, AdmissionRecord)
        if record.run_id != run_id:
            raise S3IntegrityError("admission body run ID does not match its key")
        return AdmissionRead(record=record, etag=etag)

    def update_admission(
        self,
        expected: AdmissionRead,
        replacement: AdmissionRecord,
    ) -> AdmissionRead:
        """CAS one admission revision using the ETag returned by a read/write."""
        replacement = AdmissionRecord.model_validate(replacement.model_dump())
        previous = expected.record
        if replacement.run_id != previous.run_id:
            raise ValueError("admission run ID cannot change")
        if replacement.revision != previous.revision + 1:
            raise ValueError("admission update must add exactly one revision")
        if replacement.history[:-1] != previous.history:
            raise ValueError("admission update must preserve revision history")
        for field_name in ("request_sha256", "plan_sha256", "created_at"):
            if getattr(replacement, field_name) != getattr(previous, field_name):
                raise ValueError(f"admission {field_name} cannot change")
        return self._put_admission(replacement, IfMatch=expected.etag)

    def _put_admission(
        self, admission: AdmissionRecord, **condition: str
    ) -> AdmissionRead:
        key = admission_key(admission.run_id, prefix=self._prefix)
        data = canonical_model_bytes(admission)
        digest = sha256_hex(data)
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentLength=len(data),
                ContentType=_JSON_MEDIA_TYPE,
                Metadata={_SHA256_METADATA_KEY: digest},
                ChecksumSHA256=_checksum_sha256(digest),
                **condition,
            )
        except ClientError as error:
            if _is_cas_conflict(error, allow_not_found="IfMatch" in condition):
                raise S3CasConflictError(
                    f"admission CAS conflict for run {admission.run_id!r}"
                ) from error
            raise
        etag = response.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise S3IntegrityError("admission write omitted its ETag")
        return AdmissionRead(record=admission, etag=etag)

    def publish_event(self, event: AttemptEvent) -> str:
        """Publish first, then discover visible attempt-sequence conflicts."""
        data = canonical_model_bytes(event)
        digest = sha256_hex(data)
        self._publish_stream(
            event_key(
                event.run_id,
                event.attempt_id,
                event.sequence,
                digest,
                prefix=self._prefix,
            ),
            data,
            sha256=digest,
            size=len(data),
            media_type=_JSON_MEDIA_TYPE,
        )
        self._raise_visible_run_conflict(
            event.run_id,
            known_event_hashes=((event.attempt_id, event.sequence, digest),),
        )
        return digest

    def publish_terminal(self, terminal: TerminalRecord) -> str:
        """Validate visible run/dependency state and write the terminal last."""
        data = canonical_model_bytes(terminal)
        digest = sha256_hex(data)
        observation = self._observe_run(
            terminal.run_id,
            attempts=self._verification_attempts,
            delay_seconds=self._verification_delay_seconds,
        )
        request_data = self._read_verified_object(
            request_key(
                terminal.run_id,
                terminal.request_sha256,
                prefix=self._prefix,
            ),
            sha256=terminal.request_sha256,
            media_type=_JSON_MEDIA_TYPE,
            max_size=MAX_CANONICAL_JSON_BYTES,
            exact_service_checksum=True,
        )
        request = parse_canonical_model(request_data, RequestRecord)
        if request.run_id != terminal.run_id:
            raise S3ConflictError("terminal request dependency has the wrong run ID")
        for item in request.context_manifest.files:
            self.verify_content(item.content)
        observation.request_digests.add(terminal.request_sha256)
        reasons = list(observation.conflict_reasons())
        if observation.terminal_digests and observation.terminal_digests != {digest}:
            reasons.append("a different terminal record is already visible")
        if reasons:
            raise S3ConflictError("; ".join(reasons))
        for artifact in terminal.artifacts:
            self.verify_content(artifact.content)

        self._publish_stream(
            terminal_key(terminal.run_id, digest, prefix=self._prefix),
            data,
            sha256=digest,
            size=len(data),
            media_type=_JSON_MEDIA_TYPE,
        )
        self._raise_visible_run_conflict(
            terminal.run_id,
            known_request_digests=(terminal.request_sha256,),
            known_terminal_digests=(digest,),
        )
        return digest

    def read_run_state(
        self,
        run_id: str,
        *,
        attempts: int = 3,
        delay_seconds: float = 0.1,
    ) -> RunReadState:
        """Read all visible run records across a bounded observation window."""
        run_id = validate_run_id(run_id)
        observation = self._observe_run(
            run_id,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
        reasons = observation.conflict_reasons()
        if reasons:
            return ConflictRunState(
                run_id=run_id,
                terminal_sha256s=tuple(sorted(observation.terminal_digests)),
                reasons=reasons,
            )
        return interpret_terminal_records(
            run_id, tuple(sorted(observation.terminals.items()))
        )

    def read_attempt_events(self, run_id: str) -> tuple[AttemptEvent, ...]:
        """Return all visible, valid attempt events or fail on any conflict."""
        run_id = validate_run_id(run_id)
        observation = self._observe_run(
            run_id,
            attempts=self._verification_attempts,
            delay_seconds=self._verification_delay_seconds,
        )
        reasons = observation.conflict_reasons()
        if reasons:
            raise S3ConflictError("; ".join(reasons))
        events = [
            event
            for by_digest in observation.events.values()
            for event in by_digest.values()
        ]
        return tuple(sorted(events, key=lambda item: (item.attempt_id, item.sequence)))

    def _publish_managed_file(
        self, stream: BinaryIO, descriptor: ContentObject
    ) -> None:
        validate_s3_key(descriptor.key)
        if self._reuse_existing(
            descriptor.key,
            sha256=descriptor.sha256,
            size=descriptor.size,
            media_type=descriptor.media_type,
            exact_service_checksum=False,
        ):
            return
        self._client.upload_fileobj(
            Fileobj=stream,
            Bucket=self._bucket,
            Key=descriptor.key,
            ExtraArgs={
                "ContentType": descriptor.media_type,
                "Metadata": {_SHA256_METADATA_KEY: descriptor.sha256},
                "ChecksumAlgorithm": "SHA256",
            },
            Config=self._transfer_config,
        )
        self._verify_head_with_retry(
            descriptor.key,
            sha256=descriptor.sha256,
            size=descriptor.size,
            media_type=descriptor.media_type,
            exact_service_checksum=False,
        )

    def _publish_stream(
        self,
        key: str,
        body: bytes | BinaryIO,
        *,
        sha256: str,
        size: int,
        media_type: str,
    ) -> None:
        validate_s3_key(key)
        if self._reuse_existing(
            key,
            sha256=sha256,
            size=size,
            media_type=media_type,
            exact_service_checksum=True,
        ):
            return
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentLength=size,
            ContentType=media_type,
            Metadata={_SHA256_METADATA_KEY: sha256},
            ChecksumSHA256=_checksum_sha256(sha256),
        )
        self._verify_head_with_retry(
            key,
            sha256=sha256,
            size=size,
            media_type=media_type,
            exact_service_checksum=True,
        )

    def _reuse_existing(
        self,
        key: str,
        *,
        sha256: str,
        size: int,
        media_type: str,
        exact_service_checksum: bool,
    ) -> bool:
        try:
            self._read_verified_object(
                key,
                sha256=sha256,
                size=size,
                media_type=media_type,
                exact_service_checksum=exact_service_checksum,
                collect=False,
            )
        except ClientError as error:
            if _is_not_found(error):
                return False
            raise
        except S3IntegrityError as error:
            raise S3ConflictError(
                f"existing immutable object conflicts: {key}"
            ) from error
        return True

    def _verify_head_with_retry(
        self,
        key: str,
        *,
        sha256: str,
        size: int | None = None,
        media_type: str | None = None,
        exact_service_checksum: bool,
    ) -> None:
        last_error: ClientError | None = None
        for attempt in range(self._verification_attempts):
            try:
                self._verify_head(
                    key,
                    sha256=sha256,
                    size=size,
                    media_type=media_type,
                    exact_service_checksum=exact_service_checksum,
                )
                return
            except ClientError as error:
                if not _is_not_found(error):
                    raise
                last_error = error
            if attempt + 1 < self._verification_attempts:
                self._sleep(self._backoff(attempt, self._verification_delay_seconds))
        assert last_error is not None
        raise last_error

    def _verify_head(
        self,
        key: str,
        *,
        sha256: str,
        size: int | None = None,
        media_type: str | None = None,
        exact_service_checksum: bool,
    ) -> None:
        response = self._client.head_object(
            Bucket=self._bucket,
            Key=key,
            ChecksumMode="ENABLED",
        )
        self._validate_object_response(
            response,
            key=key,
            sha256=sha256,
            size=size,
            media_type=media_type,
            exact_service_checksum=exact_service_checksum,
        )

    def _read_verified_object(
        self,
        key: str,
        *,
        sha256: str,
        size: int | None = None,
        media_type: str | None = None,
        max_size: int | None = None,
        exact_service_checksum: bool,
        collect: bool = True,
    ) -> bytes:
        response: Mapping[str, Any] | None = None
        last_error: ClientError | None = None
        for attempt in range(self._verification_attempts):
            try:
                response = self._client.get_object(
                    Bucket=self._bucket,
                    Key=key,
                    ChecksumMode="ENABLED",
                )
                break
            except ClientError as error:
                if not _is_not_found(error):
                    raise
                last_error = error
            if attempt + 1 < self._verification_attempts:
                self._sleep(self._backoff(attempt, self._verification_delay_seconds))
        if response is None:
            assert last_error is not None
            raise last_error
        self._validate_object_response(
            response,
            key=key,
            sha256=sha256,
            size=size,
            media_type=media_type,
            exact_service_checksum=exact_service_checksum,
        )
        body = response.get("Body")
        if not hasattr(body, "read"):
            raise S3IntegrityError(f"stored object body is not readable: {key}")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        read_size = 0
        limit = size if size is not None else max_size
        try:
            while True:
                request_size = _READ_CHUNK_SIZE
                if limit is not None:
                    request_size = min(request_size, max(1, limit + 1 - read_size))
                chunk = body.read(request_size)
                if not isinstance(chunk, bytes):
                    raise S3IntegrityError(
                        f"stored object body did not return bytes: {key}"
                    )
                if not chunk:
                    break
                read_size += len(chunk)
                if limit is not None and read_size > limit:
                    raise S3IntegrityError(f"stored object body is oversized: {key}")
                digest.update(chunk)
                if collect:
                    chunks.append(chunk)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if size is not None and read_size != size:
            raise S3IntegrityError(f"stored content length does not match: {key}")
        if response.get("ContentLength") != read_size:
            raise S3IntegrityError(
                f"stored body length does not match HEAD data: {key}"
            )
        if digest.hexdigest() != sha256:
            raise S3IntegrityError(f"stored body SHA-256 does not match: {key}")
        return b"".join(chunks)

    @staticmethod
    def _validate_object_response(
        response: Mapping[str, Any],
        *,
        key: str,
        sha256: str,
        size: int | None,
        media_type: str | None,
        exact_service_checksum: bool,
    ) -> None:
        metadata = response.get("Metadata", {})
        if (
            not isinstance(metadata, Mapping)
            or metadata.get(_SHA256_METADATA_KEY) != sha256
        ):
            raise S3IntegrityError(f"stored SHA-256 metadata does not match: {key}")
        if exact_service_checksum and response.get(
            "ChecksumSHA256"
        ) != _checksum_sha256(sha256):
            raise S3IntegrityError(f"stored SHA-256 checksum does not match: {key}")
        if size is not None and response.get("ContentLength") != size:
            raise S3IntegrityError(f"stored content length does not match: {key}")
        if media_type is not None and response.get("ContentType") != media_type:
            raise S3IntegrityError(f"stored content type does not match: {key}")

    def _raise_visible_run_conflict(
        self,
        run_id: str,
        *,
        known_request_digests: tuple[str, ...] = (),
        known_event_hashes: tuple[tuple[str, int, str], ...] = (),
        known_terminal_digests: tuple[str, ...] = (),
    ) -> None:
        observation = self._observe_run(
            run_id,
            attempts=self._verification_attempts,
            delay_seconds=self._verification_delay_seconds,
            known_request_digests=known_request_digests,
            known_event_hashes=known_event_hashes,
            known_terminal_digests=known_terminal_digests,
        )
        reasons = observation.conflict_reasons()
        if reasons:
            raise S3ConflictError("; ".join(reasons))

    def _observe_run(
        self,
        run_id: str,
        *,
        attempts: int,
        delay_seconds: float,
        known_request_digests: tuple[str, ...] = (),
        known_event_hashes: tuple[tuple[str, int, str], ...] = (),
        known_terminal_digests: tuple[str, ...] = (),
    ) -> _RunObservation:
        if attempts <= 0:
            raise ValueError("attempts must be positive")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        observation = _RunObservation(
            request_digests=set(known_request_digests),
            terminal_digests=set(known_terminal_digests),
        )
        for attempt_id, sequence, digest in known_event_hashes:
            observation.event_hashes.setdefault((attempt_id, sequence), set()).add(
                digest
            )
        seen_keys: set[str] = set()
        prefixes = {
            "request": self._run_prefix(run_id, "requests"),
            "event": self._run_prefix(run_id, "events"),
            "terminal": self._run_prefix(run_id, "terminals"),
        }
        for attempt in range(attempts):
            for kind, prefix in prefixes.items():
                try:
                    keys = self._list_keys(prefix)
                except S3IntegrityError as error:
                    observation.invalid[f"listing:{prefix}"] = str(error)
                    continue
                for key in keys:
                    if key not in seen_keys:
                        observation.pending.add(key)
                    seen_keys.add(key)
                    if key not in observation.pending:
                        continue
                    try:
                        self._read_run_record(observation, kind, run_id, prefix, key)
                    except ClientError as error:
                        if not _is_not_found(error):
                            raise
                    except (S3IntegrityError, TypeError, ValueError) as error:
                        observation.invalid[key] = str(error)
                        observation.pending.discard(key)
                    else:
                        observation.pending.discard(key)
            if attempt + 1 < attempts:
                self._sleep(self._backoff(attempt, delay_seconds))
        return observation

    def _read_run_record(
        self,
        observation: _RunObservation,
        kind: str,
        run_id: str,
        prefix: str,
        key: str,
    ) -> None:
        if kind == "request":
            digest = self._key_digest(key, prefix)
            observation.request_digests.add(digest)
            data = self._read_record_bytes(key, digest)
            record = parse_canonical_model(data, RequestRecord)
            if record.run_id != run_id:
                raise ValueError("request body run ID does not match its key")
            for item in record.context_manifest.files:
                self.verify_content(item.content)
            observation.requests[digest] = record
            return
        if kind == "terminal":
            digest = self._key_digest(key, prefix)
            observation.terminal_digests.add(digest)
            data = self._read_record_bytes(key, digest)
            terminal = parse_canonical_model(data, TerminalRecord)
            if terminal.run_id != run_id:
                raise ValueError("terminal body run ID does not match its key")
            request_data = self._read_record_bytes(
                request_key(run_id, terminal.request_sha256, prefix=self._prefix),
                terminal.request_sha256,
            )
            request = parse_canonical_model(request_data, RequestRecord)
            if request.run_id != run_id:
                raise ValueError("terminal request dependency has the wrong run ID")
            for item in request.context_manifest.files:
                self.verify_content(item.content)
            # A terminal's directly referenced request may be GET-visible while
            # absent from LIST. Merge it with every listed request identity so
            # a lagged second request remains a visible conflict.
            observation.request_digests.add(terminal.request_sha256)
            observation.requests[terminal.request_sha256] = request
            for artifact in terminal.artifacts:
                self.verify_content(artifact.content)
            observation.terminals[digest] = terminal
            return
        attempt_id, sequence, digest = self._event_key_parts(key, prefix)
        observation.event_hashes.setdefault((attempt_id, sequence), set()).add(digest)
        data = self._read_record_bytes(key, digest)
        event = parse_canonical_model(data, AttemptEvent)
        if (
            event.run_id != run_id
            or event.attempt_id != attempt_id
            or event.sequence != sequence
        ):
            raise ValueError("event body identity does not match its key")
        observation.events.setdefault((attempt_id, sequence), {})[digest] = event

    def _read_record_bytes(self, key: str, digest: str) -> bytes:
        return self._read_verified_object(
            key,
            sha256=digest,
            media_type=_JSON_MEDIA_TYPE,
            max_size=MAX_CANONICAL_JSON_BYTES,
            exact_service_checksum=True,
        )

    @staticmethod
    def _key_digest(key: str, prefix: str) -> str:
        suffix = key.removeprefix(prefix)
        if key == prefix or "/" in suffix or not suffix.endswith(".json"):
            raise ValueError("record key does not match the immutable layout")
        digest = suffix.removesuffix(".json")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("record key does not contain a SHA-256 digest")
        return digest

    @staticmethod
    def _event_key_parts(key: str, prefix: str) -> tuple[str, int, str]:
        suffix = key.removeprefix(prefix)
        parts = suffix.split("/")
        if key == prefix or len(parts) != 2 or not parts[1].endswith(".json"):
            raise ValueError("event key does not match the immutable layout")
        attempt_id, filename = parts
        sequence_text, separator, digest_json = filename.partition("-")
        digest = digest_json.removesuffix(".json")
        if (
            not separator
            or len(sequence_text) != 16
            or not sequence_text.isdecimal()
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("event key does not contain a valid sequence and digest")
        return attempt_id, int(sequence_text), digest

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if continuation_token is not None:
                kwargs["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**kwargs)
            contents = response.get("Contents", ())
            if not isinstance(contents, (list, tuple)):
                raise S3IntegrityError("S3 listing returned invalid contents")
            for item in contents:
                if not isinstance(item, Mapping) or not isinstance(
                    item.get("Key"), str
                ):
                    raise S3IntegrityError("S3 listing returned an invalid object key")
                keys.append(item["Key"])
            if not response.get("IsTruncated", False):
                break
            continuation_token = response.get("NextContinuationToken")
            if not isinstance(continuation_token, str) or not continuation_token:
                raise S3IntegrityError(
                    "truncated S3 listing omitted continuation token"
                )
        return sorted(set(keys))

    def _run_prefix(self, run_id: str, record_type: str) -> str:
        marker = request_key(run_id, "0" * 64, prefix=self._prefix)
        requests = f"requests/{'0' * 64}.json"
        return marker[: -len(requests)] + f"{record_type}/"
