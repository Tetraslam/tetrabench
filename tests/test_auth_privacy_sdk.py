from __future__ import annotations

import boto3
from botocore.config import Config
from botocore.stub import ANY, Stubber
from test_nativeauth_s3 import native, reference

from tetrabench.auth_sessions import seed_session
from tetrabench.models import ResolvedAwsStorageConfig, ResolvedTigrisStorageConfig
from tetrabench.nativeauth_s3 import S3SessionStore


def test_first_publication_uses_real_sdk_privacy_api_contracts(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="SYNTHETIC_ACCESS",
        aws_secret_access_key="SYNTHETIC_SECRET",
        config=Config(
            retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True
        ),
    )
    authority = S3SessionStore(
        client,
        ResolvedAwsStorageConfig(
            provider="aws", region="us-east-1", bucket="private-auth"
        ),
        binding="dedicated-controller",
        artifact_buckets=[],
        approved_private_backend=True,
    )
    metadata = {"HTTPStatusCode": 200}
    with Stubber(client) as stub:
        stub.add_response("get_bucket_location", {}, {"Bucket": "private-auth"})
        for index in range(2):
            stub.add_response(
                "get_public_access_block",
                {
                    "ResponseMetadata": metadata,
                    "PublicAccessBlockConfiguration": {
                        name: True
                        for name in (
                            "BlockPublicAcls",
                            "IgnorePublicAcls",
                            "BlockPublicPolicy",
                            "RestrictPublicBuckets",
                        )
                    },
                },
                {"Bucket": "private-auth"},
            )
            stub.add_response(
                "get_bucket_policy_status",
                {"ResponseMetadata": metadata, "PolicyStatus": {"IsPublic": False}},
                {"Bucket": "private-auth"},
            )
            stub.add_response(
                "get_bucket_acl",
                {
                    "ResponseMetadata": metadata,
                    "Owner": {"ID": "SYNTHETIC_OWNER"},
                    "Grants": [
                        {
                            "Grantee": {
                                "Type": "CanonicalUser",
                                "ID": "SYNTHETIC_OWNER",
                            },
                            "Permission": "FULL_CONTROL",
                        }
                    ],
                },
                {"Bucket": "private-auth"},
            )
            if index == 0:
                stub.add_client_error(
                    "get_object",
                    service_error_code="NoSuchKey",
                    http_status_code=404,
                    expected_params={
                        "Bucket": "private-auth",
                        "Key": "native-auth/v1/dedicated-controller/openai-eval.json",
                    },
                )
            else:
                stub.add_response(
                    "put_object",
                    {"ETag": '"SYNTHETIC_ETAG"', "ServerSideEncryption": "AES256"},
                    {
                        "Bucket": "private-auth",
                        "Key": "native-auth/v1/dedicated-controller/openai-eval.json",
                        "Body": ANY,
                        "ContentType": "application/json",
                        "CacheControl": "no-store",
                        "ServerSideEncryption": "AES256",
                        "IfNoneMatch": "*",
                    },
                )
        seed_session(authority, reference(), "opencode", native())
        stub.assert_no_pending_responses()


def test_tigris_uses_documented_bucket_and_object_acl_contracts(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    client = boto3.client(
        "s3",
        endpoint_url="https://t3.storage.dev",
        region_name="auto",
        aws_access_key_id="SYNTHETIC_ACCESS",
        aws_secret_access_key="SYNTHETIC_SECRET",
        config=Config(retries={"total_max_attempts": 1}),
    )
    authority = S3SessionStore(
        client,
        ResolvedTigrisStorageConfig(provider="tigris", bucket="private-auth"),
        binding="dedicated-controller",
        artifact_buckets=[],
        approved_private_backend=True,
    )
    bucket = {"Bucket": "private-auth"}
    object_key = {
        **bucket,
        "Key": "native-auth/v1/dedicated-controller/openai-eval.json",
    }
    metadata = {"HTTPStatusCode": 200}
    acl = {
        "ResponseMetadata": metadata,
        "Owner": {"ID": "SYNTHETIC_OWNER"},
        "Grants": [
            {
                "Grantee": {"Type": "CanonicalUser", "ID": "SYNTHETIC_OWNER"},
                "Permission": "FULL_CONTROL",
            }
        ],
    }
    with Stubber(client) as stub:
        stub.add_response("get_bucket_location", {"LocationConstraint": "iad"}, bucket)
        for phase in ("read", "create"):
            stub.add_response(
                "get_bucket_policy_status",
                {"ResponseMetadata": metadata, "PolicyStatus": {"IsPublic": False}},
                bucket,
            )
            stub.add_response("get_bucket_acl", acl, bucket)
            if phase == "read":
                stub.add_client_error(
                    "get_object_acl",
                    service_error_code="NoSuchKey",
                    http_status_code=404,
                    expected_params=object_key,
                )
            else:
                # Tigris-managed at-rest encryption is documented independently
                # of AWS response headers. Privacy is verified through ACLs.
                stub.add_response(
                    "put_object",
                    {"ETag": '"SYNTHETIC_ETAG"'},
                    {
                        **object_key,
                        "Body": ANY,
                        "ContentType": "application/json",
                        "CacheControl": "no-store",
                        "ServerSideEncryption": "AES256",
                        "ACL": "private",
                        "IfNoneMatch": "*",
                    },
                )
                stub.add_response("get_object_acl", acl, object_key)
        seed_session(authority, reference(), "opencode", native())
        stub.assert_no_pending_responses()
