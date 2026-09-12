from __future__ import annotations

import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from tetrabench.auth_config import NativeAuthReference
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthStateError,
    seed_session,
)
from tetrabench.auth_sessions import (
    claim_session as _claim_session,
)
from tetrabench.models import ResolvedAwsStorageConfig, ResolvedTigrisStorageConfig
from tetrabench.nativeauth_s3 import NativeAuthS3Client, S3SessionStore


class FakePrivateS3:
    def __init__(self):
        self.meta = SimpleNamespace(
            config=SimpleNamespace(retries={"total_max_attempts": 1}),
            endpoint_url="https://s3.us-east-1.amazonaws.com",
        )
        self.lock = threading.Lock()
        self.objects = {}
        self.writes = []
        self.fail_after_commit = False
        self.location = "us-east-1"
        self.public = False
        self.public_block = True
        self.public_acl = False
        self.privacy_denied = False
        self.public_object = False
        self.object_reads = 0

    def get_public_access_block(self, **kwargs):
        if self.privacy_denied:
            raise ClientError(
                {"Error": {"Code": "AccessDenied"}}, "GetPublicAccessBlock"
            )
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "PublicAccessBlockConfiguration": {
                key: self.public_block
                for key in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            },
        }

    def get_bucket_policy_status(self, **kwargs):
        if self.privacy_denied:
            raise ClientError(
                {"Error": {"Code": "AccessDenied"}}, "GetBucketPolicyStatus"
            )
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "PolicyStatus": {"IsPublic": self.public},
        }

    def get_bucket_acl(self, **kwargs):
        grantee = (
            {"Type": "Group", "URI": "http://acs.amazonaws.com/groups/global/AllUsers"}
            if self.public_acl
            else {"Type": "CanonicalUser", "ID": "SYNTHETIC_OWNER"}
        )
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "Owner": {"ID": "SYNTHETIC_OWNER"},
            "Grants": [{"Grantee": grantee, "Permission": "FULL_CONTROL"}],
        }

    def get_bucket_location(self, **kwargs):
        return {"LocationConstraint": self.location}

    def get_object(self, **kwargs):
        self.object_reads += 1
        with self.lock:
            row = self.objects.get(kwargs["Key"])
            if row is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {
                "Body": io.BytesIO(row["Body"]),
                "ETag": row["ETag"],
                "ServerSideEncryption": row["ServerSideEncryption"],
            }

    def get_object_acl(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObjectAcl")
        acl = self.get_bucket_acl()
        if self.public_object:
            acl["Grants"] = [
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
                    },
                    "Permission": "READ",
                }
            ]
        return acl

    def put_object(self, **kwargs):
        with self.lock:
            old = self.objects.get(kwargs["Key"])
            if (kwargs.get("IfNoneMatch") == "*" and old) or (
                "IfMatch" in kwargs
                and (old is None or old["ETag"] != kwargs["IfMatch"])
            ):
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
                )
            etag = uuid.uuid4().hex
            self.objects[kwargs["Key"]] = {**kwargs, "ETag": etag}
            self.writes.append(kwargs)
            if self.fail_after_commit:
                raise EndpointConnectionError(
                    endpoint_url="https://SYNTHETIC_SECRET_ERROR.invalid"
                )
            return {
                "ETag": etag,
                "ServerSideEncryption": kwargs["ServerSideEncryption"],
            }


def native(marker="SYNTHETIC_ACCESS"):
    return json.dumps(
        {
            "openai": {
                "type": "oauth",
                "access": marker,
                "refresh": "SYNTHETIC_REFRESH",
                "expires": 4000000000000,
            }
        }
    ).encode()


def store(client, *, approved=True, bucket="private-auth"):
    config = ResolvedAwsStorageConfig(
        provider="aws",
        bucket=bucket,
        prefix="private",
        region="us-east-1",
    )
    return S3SessionStore(
        client,
        config,
        binding="dedicated-controller",
        artifact_buckets=["run-artifacts"],
        approved_private_backend=approved,
    )


def reference():
    return NativeAuthReference(
        profile="openai-eval", binding="dedicated-controller", generation=1
    )


def claim_session(authority, ref, harness):
    return _claim_session(
        authority, ref, harness, consumer_id="fc-SYNTHETIC-CONTROLLER"
    )


def test_private_cas_no_artifact_tree_and_no_stale_secret_snapshot():
    client = FakePrivateS3()
    authority = store(client)
    ref = reference()
    seed_session(authority, ref, "opencode", native())
    claim = claim_session(authority, ref, "opencode")
    claim.finish(native("SYNTHETIC_ROTATED"), consumer_stopped=True)
    successor = claim_session(store(client), ref, "opencode")
    assert b"SYNTHETIC_ROTATED" in successor.snapshot.state.native
    assert len(client.objects) == 1
    for write in client.writes:
        assert write["Bucket"] == "private-auth"
        assert write["Key"].startswith("private/native-auth/v1/")
        assert write["ServerSideEncryption"] == "AES256"
        assert write["CacheControl"] == "no-store"
        assert "runs/" not in write["Key"]
        assert "objects/" not in write["Key"]


def test_distributed_concurrent_consumers_use_cas_not_file_locks():
    client = FakePrivateS3()
    seed_session(store(client), reference(), "opencode", native())
    barrier = threading.Barrier(6)

    def acquire(_):
        authority = store(client)
        barrier.wait()
        try:
            claim_session(authority, reference(), "opencode")
            return "claimed"
        except AuthBusyError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(acquire, range(6)))
    assert outcomes.count("claimed") == 1


def test_ambiguous_claim_never_exposes_copy_or_retries():
    client = FakePrivateS3()
    authority = store(client)
    seed_session(authority, reference(), "opencode", native())
    client.fail_after_commit = True
    before = len(client.writes)
    with pytest.raises(AuthStateError, match="ambiguous") as error:
        claim_session(authority, reference(), "opencode")
    assert "SYNTHETIC" not in str(error.value)
    assert len(client.writes) == before + 1
    client.fail_after_commit = False
    with pytest.raises(AuthBusyError):
        claim_session(store(client), reference(), "opencode")


def test_failed_writeback_cannot_clone_reuse_old_refresh_state():
    client = FakePrivateS3()
    authority = store(client)
    seed_session(authority, reference(), "opencode", native())
    claim = claim_session(authority, reference(), "opencode")
    client.fail_after_commit = True
    with pytest.raises(AuthStateError):
        claim.finish(native("SYNTHETIC_ROTATED"), consumer_stopped=True)
    with pytest.raises(AuthStateError):
        claim.finish(native(), consumer_stopped=True)
    client.fail_after_commit = False
    assert (
        b"SYNTHETIC_ROTATED"
        in claim_session(authority, reference(), "opencode").snapshot.state.native
    )


def test_backend_requires_approval_separate_bucket_topology_and_no_sdk_retries():
    client = FakePrivateS3()
    with pytest.raises(AuthStateError, match="approval"):
        store(client, approved=False)
    with pytest.raises(AuthStateError, match="artifact"):
        store(client, bucket="run-artifacts")
    client.location = "global"
    with pytest.raises(AuthStateError, match="topology"):
        store(client).read("profile")
    client.meta.config.retries["total_max_attempts"] = 3
    with pytest.raises(AuthStateError, match="no-retry"):
        store(client)


def test_remote_claim_needs_queryable_runtime_owner_and_tls():
    client = FakePrivateS3()
    authority = store(client)
    seed_session(authority, reference(), "opencode", native())
    with pytest.raises(AuthStateError, match="consumer ID"):
        _claim_session(authority, reference(), "opencode")
    client.meta.endpoint_url = "http://synthetic.invalid"
    with pytest.raises(AuthStateError, match="HTTPS"):
        store(client)


@pytest.mark.parametrize("setting", ["public", "public_acl", "privacy_denied"])
def test_authenticated_privacy_gate_blocks_initial_publication(setting):
    client = FakePrivateS3()
    setattr(client, setting, True)
    with pytest.raises(AuthStateError):
        seed_session(store(client), reference(), "opencode", native())
    assert not client.writes


def test_public_access_block_and_policy_are_rechecked_not_cached():
    client = FakePrivateS3()
    authority = store(client)
    seed_session(authority, reference(), "opencode", native())
    client.public_block = False
    with pytest.raises(AuthStateError, match="public-access"):
        claim_session(authority, reference(), "opencode")
    assert len(client.writes) == 1


def tigris_store(client, *, trust_organization_admins=False):
    client.meta.endpoint_url = "https://t3.storage.dev"
    client.location = "iad"
    return S3SessionStore(
        cast(NativeAuthS3Client, client),
        ResolvedTigrisStorageConfig(provider="tigris", bucket="private-auth"),
        binding="dedicated-controller",
        artifact_buckets=[],
        approved_private_backend=True,
        trust_organization_admins=trust_organization_admins,
    )


def test_tigris_verifies_native_bucket_and_object_acls(monkeypatch):
    client = FakePrivateS3()
    authority = tigris_store(client)

    def unsupported_aws_api(**kwargs):
        pytest.fail("Tigris must not depend on AWS PublicAccessBlock")

    monkeypatch.setattr(client, "get_public_access_block", unsupported_aws_api)
    seed_session(authority, reference(), "opencode", native())
    acquired = claim_session(authority, reference(), "opencode")
    acquired.finish(native("SYNTHETIC_ROTATED"), consumer_stopped=True)
    assert all(write["ACL"] == "private" for write in client.writes)
    assert authority.read(reference().profile) is not None
    client.public_object = True
    before = client.object_reads
    with pytest.raises(AuthStateError, match="ACL"):
        authority.read(reference().profile)
    assert client.object_reads == before


@pytest.mark.parametrize("flag", ["public", "public_acl", "privacy_denied"])
def test_tigris_unknown_or_public_bucket_refuses_first_publication(flag):
    client = FakePrivateS3()
    setattr(client, flag, True)
    with pytest.raises(AuthStateError):
        seed_session(tigris_store(client), reference(), "opencode", native())
    assert not client.writes


def org_admin_grant():
    # Native response shape shown in Tigris's official MCP sharing example.
    return {
        "Grantee": {"Type": "Group", "URI": "https://groups.tigris.dev/org/admins"},
        "Permission": "FULL_CONTROL",
    }


def add_acl_grant(client, monkeypatch, grant):
    original = client.get_bucket_acl

    def bucket_acl(**kwargs):
        acl = original(**kwargs)
        acl["Grants"].append(grant)
        return acl

    monkeypatch.setattr(client, "get_bucket_acl", bucket_acl)


def test_tigris_org_admins_require_explicit_trust_for_reads_and_first_write(
    monkeypatch,
):
    client = FakePrivateS3()
    authority = tigris_store(client)
    add_acl_grant(client, monkeypatch, org_admin_grant())
    with pytest.raises(AuthStateError, match="ACL"):
        seed_session(authority, reference(), "opencode", native())
    assert not client.writes and not client.object_reads
    seed_session(
        tigris_store(client, trust_organization_admins=True),
        reference(),
        "opencode",
        native(),
    )
    with pytest.raises(AuthStateError, match="ACL"):
        authority.read(reference().profile)
    assert not client.object_reads


def test_trusted_tigris_org_admins_preserve_private_cas_and_native_writeback(
    monkeypatch,
):
    client = FakePrivateS3()
    add_acl_grant(client, monkeypatch, org_admin_grant())
    authority = tigris_store(client, trust_organization_admins=True)
    seed_session(authority, reference(), "opencode", native())
    claim = claim_session(authority, reference(), "opencode")
    claim.finish(native("SYNTHETIC_ROTATED"), consumer_stopped=True)
    current = authority.read(reference().profile)
    assert current is not None and b"SYNTHETIC_ROTATED" in current.state.native
    assert client.writes[0]["IfNoneMatch"] == "*"
    assert all("IfMatch" in write for write in client.writes[1:])
    assert all(write["ACL"] == "private" for write in client.writes)
    assert all(write["ServerSideEncryption"] == "AES256" for write in client.writes)
    assert all(write["CacheControl"] == "no-store" for write in client.writes)


@pytest.mark.parametrize("public", [True, None, 0, "false"])
def test_org_admin_trust_still_requires_actual_private_policy(monkeypatch, public):
    client = FakePrivateS3()
    add_acl_grant(client, monkeypatch, org_admin_grant())
    client.public = public
    with pytest.raises(AuthStateError, match="policy"):
        seed_session(
            tigris_store(client, trust_organization_admins=True),
            reference(),
            "opencode",
            native(),
        )
    assert not client.writes and not client.object_reads


def test_org_admin_trust_does_not_cache_private_policy(monkeypatch):
    client = FakePrivateS3()
    add_acl_grant(client, monkeypatch, org_admin_grant())
    authority = tigris_store(client, trust_organization_admins=True)
    seed_session(authority, reference(), "opencode", native())
    current = authority.read(reference().profile)
    assert current is not None
    client.public = True
    before = client.object_reads
    with pytest.raises(AuthStateError, match="policy"):
        authority.read(reference().profile)
    with pytest.raises(AuthStateError, match="policy"):
        authority.compare_and_swap(reference().profile, current.version, current.state)
    assert client.object_reads == before and len(client.writes) == 1


@pytest.mark.parametrize(
    "code", ["AccessDenied", "NotImplemented", "NoSuchBucketPolicy"]
)
def test_org_admin_trust_never_substitutes_for_policy_metadata(monkeypatch, code):
    client = FakePrivateS3()
    add_acl_grant(client, monkeypatch, org_admin_grant())

    def unavailable(**kwargs):
        raise ClientError({"Error": {"Code": code}}, "GetBucketPolicyStatus")

    monkeypatch.setattr(client, "get_bucket_policy_status", unavailable)
    with pytest.raises(AuthStateError):
        seed_session(
            tigris_store(client, trust_organization_admins=True),
            reference(),
            "opencode",
            native(),
        )
    assert not client.writes and not client.object_reads


@pytest.mark.parametrize("where", ["bucket", "object"])
@pytest.mark.parametrize(
    "grantee",
    [
        {"Type": "Group", "URI": "http://acs.amazonaws.com/groups/global/AllUsers"},
        {
            "Type": "Group",
            "URI": "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
        },
        {"Type": "Group", "URI": "https://groups.tigris.dev/org/members"},
        {"Type": "Group", "URI": "http://groups.tigris.dev/org/admins"},
        {"Type": "Group", "URI": "https://groups.tigris.dev/org/admins/"},
        {"Type": "Group", "URI": "https://groups.tigris.dev.evil.invalid/org/admins"},
        {"Type": "Group", "URI": "https://groups.tigris.dev/org/%61dmins"},
        {"Type": "CanonicalUser", "ID": "SYNTHETIC_OTHER_OWNER"},
        {
            "Type": "Group",
            "URI": "https://groups.tigris.dev/org/admins",
            "ID": "SYNTHETIC_OTHER_OWNER",
        },
    ],
)
def test_org_admin_trust_rejects_every_other_grantee(monkeypatch, where, grantee):
    client = FakePrivateS3()
    add_acl_grant(client, monkeypatch, org_admin_grant())
    authority = tigris_store(client, trust_organization_admins=True)
    seed_session(authority, reference(), "opencode", native())
    method = f"get_{where}_acl"
    original = getattr(client, method)

    def unsafe(**kwargs):
        acl = original(**kwargs)
        acl["Grants"].append({"Grantee": grantee, "Permission": "FULL_CONTROL"})
        return acl

    monkeypatch.setattr(client, method, unsafe)
    before = client.object_reads
    with pytest.raises(AuthStateError, match="ACL"):
        authority.read(reference().profile)
    assert client.object_reads == before
    assert len(client.writes) == 1


@pytest.mark.parametrize("fault", ["missing_owner_grant", "admin_read_only"])
def test_tigris_org_admin_exception_requires_exact_grants(monkeypatch, fault):
    client = FakePrivateS3()
    original = client.get_bucket_acl

    def unsafe(**kwargs):
        acl = original(**kwargs)
        admin = org_admin_grant()
        if fault == "missing_owner_grant":
            acl["Grants"] = [admin]
        else:
            admin["Permission"] = "READ"
            acl["Grants"].append(admin)
        return acl

    monkeypatch.setattr(client, "get_bucket_acl", unsafe)
    with pytest.raises(AuthStateError, match="ACL"):
        seed_session(
            tigris_store(client, trust_organization_admins=True),
            reference(),
            "opencode",
            native(),
        )
    assert not client.writes and not client.object_reads


def test_org_admin_trust_still_requires_matching_object_owner(monkeypatch):
    client = FakePrivateS3()
    add_acl_grant(client, monkeypatch, org_admin_grant())
    authority = tigris_store(client, trust_organization_admins=True)
    seed_session(authority, reference(), "opencode", native())
    current = authority.read(reference().profile)
    assert current is not None
    original = client.get_object_acl

    def different_owner(**kwargs):
        acl = original(**kwargs)
        acl["Owner"]["ID"] = "SYNTHETIC_OTHER_OWNER"
        acl["Grants"][0]["Grantee"]["ID"] = "SYNTHETIC_OTHER_OWNER"
        return acl

    monkeypatch.setattr(client, "get_object_acl", different_owner)
    before = client.object_reads
    with pytest.raises(AuthStateError, match="ownership differs"):
        authority.read(reference().profile)
    assert client.object_reads == before
    with pytest.raises(AuthStateError, match="acknowledgement is unproven"):
        authority.compare_and_swap(reference().profile, current.version, current.state)
    assert len(client.writes) == 2


def test_tigris_admin_group_is_never_accepted_for_aws(monkeypatch):
    client = FakePrivateS3()
    add_acl_grant(client, monkeypatch, org_admin_grant())
    with pytest.raises(AuthStateError, match="ACL"):
        store(client).read(reference().profile)
    with pytest.raises(AuthStateError, match="Tigris-only"):
        S3SessionStore(
            cast(NativeAuthS3Client, client),
            ResolvedAwsStorageConfig(
                provider="aws", bucket="private-auth", region="us-east-1"
            ),
            binding="dedicated-controller",
            artifact_buckets=[],
            approved_private_backend=True,
            trust_organization_admins=True,
        )
    assert not client.writes and not client.object_reads


def test_failed_cli_logout_keeps_remote_claim_and_tracks_ambiguous_owner(
    tmp_path, monkeypatch
):
    from tetrabench.auth import auth_logout
    from tetrabench.auth_config import AuthSpec
    from tetrabench.nativeauth import NativeResult

    client = FakePrivateS3()
    authority = store(client)
    seed_session(authority, reference(), "opencode", native())

    def process(argv, **kwargs):
        if "--version" in argv:
            return NativeResult(0, b"1.18.30")
        if "logout" in argv:
            return NativeResult(1, b"SYNTHETIC_SECRET_ERROR")
        return NativeResult(0, b"OpenAI oauth")

    monkeypatch.setattr("tetrabench.auth.run_native", process)
    with pytest.raises(AuthStateError):
        auth_logout(
            "opencode",
            AuthSpec(mode="chatgpt_oauth", reference=reference()),
            store=authority,
            executable="synthetic-native",
            runtime_parent=tmp_path / "runtime",
            environment={},
            artifact_roots=[],
        )
    current = authority.read("openai-eval")
    assert current is not None and current.state.phase == "claimed"
    operation = json.loads(
        (tmp_path / "runtime/operations" / f"{current.state.owner}.json").read_bytes()
    )
    assert operation["state"] == "ambiguous"
    assert "SYNTHETIC_SECRET_ERROR" not in json.dumps(operation)
