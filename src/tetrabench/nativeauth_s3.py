"""Opt-in private S3 CAS authority for ephemeral native auth consumers.

This is NOT the run's S3Store or a Modal Secret snapshot. The operator must
approve a separate private bucket/key policy and a dedicated runtime binding.
The same strongly consistent topology gate as run admission applies. A failed
CAS response is ambiguous and never retried by this module; claimed profiles
have no timeout or automatic takeover. Live IAM/encryption and Modal sandbox
writeback remain deployment evidence, not properties established by unit tests.

AWS writers need GetBucketPublicAccessBlock, GetBucketPolicyStatus, GetBucketAcl,
GetBucketLocation and scoped GetObject/PutObject (plus KMS permissions when used).
AWS requires all public-access blocks and owner-only ACLs. Tigris uses native
bucket policy status, bucket/object ACLs and explicit private object writes.
Tigris keys therefore need GetBucketLocation/GetBucketAcl/GetBucketPolicyStatus,
GetObjectAcl and scoped GetObject/PutObject/PutObjectAcl. Missing or unsupported
ACL metadata stays blocked; managed encryption is not evidence of private access.
An operator may explicitly trust the approved bucket's Tigris organization admins.
Only their exact native Group/FULL_CONTROL grant is then accepted alongside the
owner grant; this does not trust all authenticated users or other organizations.
The operator owns organization selection and membership review. ACLs do not carry
an organization ID, and these checks do not prove cross-account IAM isolation.
Tigris documents the grant separately from public AllUsers access at
https://www.tigrisdata.com/blog/mcp-server-sharing/#challenges-i-faced-along-the-way
and organization authority at
https://www.tigrisdata.com/docs/account-management/organizations/.
Sources: https://www.tigrisdata.com/docs/api/s3/ and
https://www.tigrisdata.com/docs/objects/acl/. Deployment permissions and actual
metadata responses remain live prerequisites, not consequences of the config.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from tetrabench.auth_config import NativeAuthReference
from tetrabench.auth_sessions import (
    MAX_STATE_BYTES,
    AuthBusyError,
    AuthStateError,
    SessionState,
    StateRead,
)
from tetrabench.models import ResolvedAwsStorageConfig, ResolvedTigrisStorageConfig
from tetrabench.s3 import S3Client, S3Store, UnsafeCoordinationTopologyError


class NativeAuthS3Client(S3Client, Protocol):
    def get_public_access_block(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_bucket_policy_status(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_bucket_acl(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_object_acl(self, **kwargs: Any) -> Mapping[str, Any]: ...


class S3SessionStore:
    """One conditional private object per native profile, not a run database.

    ``client`` must be a dedicated client with total_max_attempts=1. SDK retry
    after a successful-but-unacknowledged conditional mutation can hide its true
    outcome. Application callers likewise must never replay a claim or finish.
    """

    requires_consumer_id = True

    def __init__(
        self,
        client: NativeAuthS3Client,
        storage: ResolvedAwsStorageConfig | ResolvedTigrisStorageConfig,
        *,
        binding: str,
        artifact_buckets: Iterable[str],
        approved_private_backend: bool,
        kms_key_id: str | None = None,
        trust_organization_admins: bool = False,
    ):
        NativeAuthReference(profile="validate", binding=binding, generation=1)
        if not approved_private_backend:
            raise AuthStateError("private credential backend approval is required")
        if storage.bucket in set(artifact_buckets):
            raise AuthStateError(
                "credential authority must not share a run artifact bucket"
            )
        retries = getattr(
            getattr(getattr(client, "meta", None), "config", None), "retries", {}
        )
        if not isinstance(retries, dict) or retries.get("total_max_attempts") != 1:
            raise AuthStateError(
                "credential CAS requires a dedicated no-retry S3 client"
            )
        endpoint = getattr(getattr(client, "meta", None), "endpoint_url", "")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise AuthStateError("private credential transport must use HTTPS")
        self.binding = binding
        self._client = client
        self._bucket = storage.bucket
        self._provider = storage.provider
        if type(trust_organization_admins) is not bool or (
            trust_organization_admins and self._provider != "tigris"
        ):
            raise AuthStateError("organization admin trust is a Tigris-only boolean")
        self._trust_organization_admins = trust_organization_admins
        if self._provider == "tigris" and (
            endpoint.rstrip("/") != "https://t3.storage.dev" or kms_key_id is not None
        ):
            raise AuthStateError(
                "Tigris auth requires its native endpoint and managed encryption, "
                "not AWS KMS"
            )
        self._bucket_owner: str | None = None
        prefix = f"{storage.prefix}/" if storage.prefix else ""
        self._prefix = f"{prefix}native-auth/v1/{binding}/"
        self._topology = S3Store(storage, client)
        self._encryption = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_id}
            if kms_key_id
            else {"ServerSideEncryption": "AES256"}
        )

    def _key(self, profile: str) -> str:
        NativeAuthReference(profile=profile, binding=self.binding, generation=1)
        return f"{self._prefix}{profile}.json"

    def _gate(self) -> None:
        try:
            self._topology.require_coordination_safe()
            self._verify_privacy()
        except (BotoCoreError, ClientError, UnsafeCoordinationTopologyError):
            raise AuthStateError(
                "private credential backend topology is not proven safe"
            ) from None

    @staticmethod
    def _metadata(response: Mapping[str, Any]) -> None:
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise AuthStateError("credential bucket privacy metadata is incomplete")

    def _verify_privacy(self) -> None:
        """Fresh authenticated provider observations, not an operator boolean alone.

        The writer needs read access to these controls, not permission to change
        them. External administrators can change policy after observation; no S3
        primitive atomically combines a policy check with an object PUT.
        """
        try:
            if self._provider == "aws":
                block = self._client.get_public_access_block(Bucket=self._bucket)
                self._metadata(block)
                config = block.get("PublicAccessBlockConfiguration", {})
                if any(
                    config.get(key) is not True
                    for key in (
                        "BlockPublicAcls",
                        "IgnorePublicAcls",
                        "BlockPublicPolicy",
                        "RestrictPublicBuckets",
                    )
                ):
                    raise AuthStateError(
                        "credential bucket must enable all public-access blocks"
                    )
            try:
                policy = self._client.get_bucket_policy_status(Bucket=self._bucket)
            except ClientError as error:
                if (
                    self._provider != "aws"
                    or error.response.get("Error", {}).get("Code")
                    != "NoSuchBucketPolicy"
                ):
                    raise
            else:
                self._metadata(policy)
                if policy.get("PolicyStatus", {}).get("IsPublic") is not False:
                    raise AuthStateError(
                        "credential bucket policy is not verified private"
                    )
            self._bucket_owner = self._private_acl(
                self._client.get_bucket_acl(Bucket=self._bucket)
            )
        except (BotoCoreError, ClientError, AttributeError, TypeError, KeyError):
            raise AuthStateError(
                "credential bucket privacy could not be verified; "
                "no credential transfer"
            ) from None

    def _private_acl(self, acl: Mapping[str, Any]) -> str:
        self._metadata(acl)
        owner, grants = acl.get("Owner", {}).get("ID"), acl.get("Grants")
        if (
            not isinstance(owner, str)
            or not owner
            or len(owner) > 256
            or not isinstance(grants, list)
            or not 1 <= len(grants) <= 100
        ):
            raise AuthStateError("credential ACL metadata is incomplete")
        owner_granted = False
        for grant in grants:
            if not isinstance(grant, Mapping):
                raise AuthStateError("credential ACL metadata is incomplete")
            grantee = grant.get("Grantee", {})
            if not isinstance(grantee, Mapping):
                raise AuthStateError("credential ACL metadata is incomplete")
            owner_grant = (
                grantee.get("Type") == "CanonicalUser"
                and grantee.get("ID") == owner
                and grant.get("Permission") == "FULL_CONTROL"
            )
            admin_grant = (
                self._provider == "tigris"
                and self._trust_organization_admins
                and grantee.get("Type") == "Group"
                and grantee.get("URI") == "https://groups.tigris.dev/org/admins"
                and grantee.get("ID") is None
                and grant.get("Permission") == "FULL_CONTROL"
            )
            if not (owner_grant or admin_grant):
                raise AuthStateError("credential ACL contains an unapproved grant")
            owner_granted |= owner_grant
        if not owner_granted:
            raise AuthStateError("credential ACL must grant its owner full control")
        return owner

    def _verify_object_privacy(self, profile: str) -> None:
        acl = self._client.get_object_acl(Bucket=self._bucket, Key=self._key(profile))
        if self._private_acl(acl) != self._bucket_owner:
            raise AuthStateError(
                "credential object ownership differs from its private bucket"
            )

    def _verify_encryption(self, response: Mapping[str, Any]) -> None:
        if self._provider == "tigris" and response.get("ServerSideEncryption") is None:
            # Tigris documents managed encryption as always on; this is not an
            # AWS encryption-header claim, nor evidence of private access.
            return
        if (
            response.get("ServerSideEncryption")
            != self._encryption["ServerSideEncryption"]
        ):
            raise AuthStateError("private credential object encryption is not verified")
        if (
            "SSEKMSKeyId" in self._encryption
            and response.get("SSEKMSKeyId") != self._encryption["SSEKMSKeyId"]
        ):
            raise AuthStateError("private credential KMS key does not match approval")

    def read(self, profile: str) -> StateRead | None:
        self._gate()
        try:
            if self._provider == "tigris":
                self._verify_object_privacy(profile)
            response = self._client.get_object(
                Bucket=self._bucket, Key=self._key(profile)
            )
            stream = response["Body"]
            try:
                data = stream.read(MAX_STATE_BYTES + 1)
            finally:
                stream.close()
            if not isinstance(data, bytes) or len(data) > MAX_STATE_BYTES:
                raise AuthStateError("private credential object exceeds size limit")
            state = SessionState.decode(data)
            if state.binding != self.binding or state.profile != profile:
                raise AuthStateError("private credential object binding mismatch")
            etag = response.get("ETag")
            if not isinstance(etag, str) or not etag or len(etag) > 256:
                raise AuthStateError(
                    "private credential object has no bounded CAS version"
                )
            self._verify_encryption(response)
            return StateRead(state, etag)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                "NoSuchKey",
                "404",
                "NotFound",
            }:
                return None
            raise AuthStateError("private credential read failed") from None
        except (BotoCoreError, OSError, KeyError, TypeError, AttributeError):
            raise AuthStateError("private credential read failed") from None

    def compare_and_swap(
        self, profile: str, version: str | None, state: SessionState
    ) -> StateRead:
        self._gate()
        if state.profile != profile or state.binding != self.binding:
            raise AuthStateError("private credential CAS binding mismatch")
        condition = {"IfNoneMatch": "*"} if version is None else {"IfMatch": version}
        private_object = {"ACL": "private"} if self._provider == "tigris" else {}
        data = state.encode()
        if len(data) > MAX_STATE_BYTES:
            raise AuthStateError("private credential state exceeds size limit")
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=self._key(profile),
                Body=data,
                ContentType="application/json",
                CacheControl="no-store",
                **self._encryption,
                **condition,
                **private_object,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
                "412",
                "409",
            }:
                raise AuthBusyError(
                    "credential CAS lost; do not replay the operation"
                ) from None
            raise AuthStateError(
                "credential write outcome ambiguous; do not replay"
            ) from None
        except (BotoCoreError, OSError):
            raise AuthStateError(
                "credential write outcome ambiguous; do not replay"
            ) from None
        etag = response.get("ETag")
        self._verify_encryption(response)
        if self._provider == "tigris":
            try:
                self._verify_object_privacy(profile)
            except (
                BotoCoreError,
                ClientError,
                KeyError,
                TypeError,
                AttributeError,
                AuthStateError,
            ):
                raise AuthStateError(
                    "Tigris private object acknowledgement is unproven; do not replay"
                ) from None
        if not isinstance(etag, str) or not etag or len(etag) > 256:
            raise AuthStateError(
                "credential write acknowledgement missing; do not replay"
            )
        return StateRead(state, etag)
