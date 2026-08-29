"""Opt-in live conditional-write probe for an existing AWS or Tigris bucket."""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError

from tetrabench.models import AwsStorageConfig, TigrisStorageConfig
from tetrabench.s3 import S3Store, create_s3_client


class ProbeFailure(RuntimeError):
    """Probe work failed, cleanup failed, or both failed independently."""

    def __init__(
        self,
        *,
        original_error: Exception | None,
        cleanup_error: Exception | None,
    ) -> None:
        failures = []
        if original_error is not None:
            failures.append(f"probe={type(original_error).__name__}")
        if cleanup_error is not None:
            failures.append(f"cleanup={type(cleanup_error).__name__}")
        super().__init__("provider consistency probe failed: " + ", ".join(failures))
        self.original_error = original_error
        self.cleanup_error = cleanup_error


def _lost_cas(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {
        "409",
        "412",
        "ConditionalRequestConflict",
        "PreconditionFailed",
    } or status in {409, 412}


def _not_found(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _ambiguous_create_error(error: Exception) -> bool:
    """Return whether transport may have lost a successful create response."""
    if isinstance(error, ClientError):
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return isinstance(status, int) and status >= 500
    return isinstance(
        error,
        (ConnectionClosedError, ReadTimeoutError, ConnectionError, TimeoutError),
    )


def _listed(client: Any, bucket: str, key: str) -> bool:
    response = client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
    return any(item.get("Key") == key for item in response.get("Contents", []))


def _cleanup_probe_key(
    config,
    client: Any,
    key: str,
    *,
    attempts: int,
    sleep: Callable[[float], None],
    delay_seconds: float,
) -> Exception | None:
    errors: list[Exception] = []
    try:
        client.delete_object(Bucket=config.bucket, Key=key)
    except Exception as error:
        errors.append(error)

    verification_error: Exception | None = None
    absent = False
    for attempt in range(attempts):
        head_absent = False
        try:
            client.head_object(Bucket=config.bucket, Key=key)
        except ClientError as error:
            if _not_found(error):
                head_absent = True
            else:
                verification_error = error
        except Exception as error:
            verification_error = error
        else:
            verification_error = RuntimeError(
                "probe object remained visible after cleanup"
            )
        try:
            listed = _listed(client, config.bucket, key)
        except Exception as error:
            verification_error = error
            listed = True
        if head_absent and not listed:
            absent = True
            break
        if listed:
            verification_error = RuntimeError(
                "probe object remained visible in LIST after cleanup"
            )
        if attempt + 1 < attempts:
            sleep(delay_seconds)

    if not absent:
        errors.append(
            verification_error
            or RuntimeError("probe object absence could not be verified")
        )
    if not errors:
        return None
    if len(errors) == 1:
        return errors[0]
    return ExceptionGroup("probe cleanup failed", errors)


def run_probe(
    config,
    client_factory: Callable[[], Any],
    *,
    cleanup_verification_attempts: int = 3,
    cleanup_delay_seconds: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Verify topology, exclusive create, and a synchronized two-client CAS race."""
    if cleanup_verification_attempts <= 0:
        raise ValueError("cleanup verification attempts must be positive")
    if cleanup_delay_seconds < 0:
        raise ValueError("cleanup verification delay must be non-negative")

    topology = S3Store(config, client_factory()).require_coordination_safe()
    key = f"{config.prefix + '/' if config.prefix else ''}probes/{uuid.uuid4().hex}"
    cleanup_required = False
    create_client: Any | None = None
    report: dict[str, object] | None = None
    original_error: Exception | None = None
    try:
        create_client = client_factory()
        response = create_client.put_object(
            Bucket=config.bucket,
            Key=key,
            Body=b"0",
            IfNoneMatch="*",
        )
        cleanup_required = True
        etag = response["ETag"]
        get_response = create_client.get_object(Bucket=config.bucket, Key=key)
        body = get_response["Body"]
        created_body = body.read() if hasattr(body, "read") else body
        if created_body != b"0":
            raise RuntimeError("immediate GET did not return created bytes")
        head_response = create_client.head_object(Bucket=config.bucket, Key=key)
        if head_response.get("ETag") != etag:
            raise RuntimeError("immediate HEAD did not return created ETag")
        if not _listed(create_client, config.bucket, key):
            raise RuntimeError("immediate LIST did not return created key")
        writers = (client_factory(), client_factory())
        if writers[0] is writers[1]:
            raise RuntimeError("conditional race requires two separate clients")
        barrier = threading.Barrier(2)

        def update(client: Any, body: bytes) -> str:
            barrier.wait(timeout=5)
            try:
                client.put_object(
                    Bucket=config.bucket,
                    Key=key,
                    Body=body,
                    IfMatch=etag,
                )
            except ClientError as error:
                if _lost_cas(error):
                    return "lost"
                raise
            return "won"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(update, writers[0], b"1"),
                executor.submit(update, writers[1], b"2"),
            )
            outcomes = tuple(future.result() for future in futures)
        if sorted(outcomes) != ["lost", "won"]:
            raise RuntimeError("conditional update race did not produce one winner")
        report = {
            "admission_safe": True,
            "bucket_location": topology.bucket_location,
            "conditional_create": "passed",
            "conditional_update_race": "passed",
            "immediate_get": "passed",
            "immediate_head": "passed",
            "immediate_list": "passed",
            "location_type": topology.location_type,
            "provider": topology.provider,
        }
    except Exception as error:
        original_error = error
        cleanup_required = _ambiguous_create_error(error)

    cleanup_error = None
    if cleanup_required:
        assert create_client is not None
        cleanup_error = _cleanup_probe_key(
            config,
            create_client,
            key,
            attempts=cleanup_verification_attempts,
            sleep=sleep,
            delay_seconds=cleanup_delay_seconds,
        )
    if original_error is not None or cleanup_error is not None:
        raise ProbeFailure(
            original_error=original_error,
            cleanup_error=cleanup_error,
        ) from original_error
    assert report is not None
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("aws", "tigris"), required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region")
    parser.add_argument("--prefix", default="tetrabench-consistency-probes")
    parser.add_argument(
        "--allow-mutation",
        action="store_true",
        help="create, race-update, delete, and verify absence of one random object",
    )
    args = parser.parse_args()
    if not args.allow_mutation:
        parser.error("live consistency probing requires --allow-mutation")
    if args.provider == "aws":
        if not args.region:
            parser.error("AWS probing requires --region")
        config = AwsStorageConfig(
            provider="aws",
            bucket=args.bucket,
            region=args.region,
            prefix=args.prefix,
        )
    else:
        if args.region:
            parser.error("Tigris uses SDK region auto; omit --region")
        config = TigrisStorageConfig(
            provider="tigris",
            bucket=args.bucket,
            prefix=args.prefix,
        )
    report = run_probe(config, lambda: create_s3_client(config))
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
