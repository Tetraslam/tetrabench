"""Explicit, bounded, read-only verification of a terminal's inputs and artifacts.

The report describes streamed bytes during this operation, not a permanent
storage guarantee. Both authority observations retain the normal conflict window.
Serial streaming keeps memory bounded to one S3 read chunk and avoids multiplying
provider retries. Equal key/hash/size descriptors share one verification, even
when logical paths or media types differ.
"""

from __future__ import annotations

from typing import Literal, Protocol

from botocore.exceptions import ClientError
from pydantic import Field

from tetrabench.artifact_policy import ArtifactLimits
from tetrabench.artifacts import ArtifactPullRefusedError, _validate_inventory
from tetrabench.lifecycle import (
    AuthoritativeBindingError,
    BindingStore,
    terminal_admission_conflicts,
    validate_request_plan_storage_binding,
)
from tetrabench.models import FrozenRecord, NonEmptyString, Sha256
from tetrabench.records import (
    ConflictRunState,
    ContentObject,
    RunId,
    RunReadState,
    TerminalRunState,
    validate_run_id,
)
from tetrabench.s3 import AdmissionRead, S3IntegrityError


class VerificationStore(BindingStore, Protocol):
    def read_run_state(self, run_id: str) -> RunReadState: ...
    def read_admission(self, run_id: str) -> AdmissionRead | None: ...
    def verify_content(self, descriptor: ContentObject) -> None: ...


class ArtifactVerificationReport(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    state: Literal["verified", "failed", "refused"]
    terminal_sha256: Sha256 | None = None
    input_references: int = Field(default=0, ge=0)
    artifact_references: int = Field(default=0, ge=0)
    objects_total: int = Field(default=0, ge=0)
    bytes_total: int = Field(default=0, ge=0)
    objects_verified: int = Field(default=0, ge=0)
    bytes_verified: int = Field(default=0, ge=0)
    missing: tuple[ContentObject, ...] = ()
    corrupt: tuple[ContentObject, ...] = ()
    reasons: tuple[NonEmptyString, ...] = ()


class _AuditRefused(Exception):
    pass


class ArtifactVerificationService:
    def __init__(
        self, store: VerificationStore, *, limits: ArtifactLimits | None = None
    ) -> None:
        self._store = store
        self._limits = limits or ArtifactLimits()

    def _terminal(self, run_id: str) -> TerminalRunState:
        durable = self._store.read_run_state(run_id)
        if isinstance(durable, ConflictRunState):
            if any("S3 listing incomplete:" in reason for reason in durable.reasons):
                raise _AuditRefused(
                    "run record listing is incomplete: listing budget exceeded"
                )
            raise _AuditRefused("run has conflicting immutable records")
        if not isinstance(durable, TerminalRunState):
            raise _AuditRefused("run has no authoritative terminal inventory")
        observed = self._store.read_admission(run_id)
        conflicts = terminal_admission_conflicts(
            self._store, durable, observed.record if observed is not None else None
        )
        if conflicts:
            raise _AuditRefused("terminal and admission authority failed validation")
        return durable

    def verify(self, run_id: str) -> ArtifactVerificationReport:
        """Audit a complete inventory; provider/auth/transport errors propagate.

        Missing objects and integrity mismatches are reported separately. Counts
        and bytes are unique descriptors, and verified bytes exclude failed reads.
        A refused report must never be interpreted as a successful payload audit.
        """
        run_id = validate_run_id(run_id)
        report = ArtifactVerificationReport(run_id=run_id, state="refused")
        try:
            durable = self._terminal(run_id)
            request = validate_request_plan_storage_binding(
                self._store,
                run_id=run_id,
                request_sha256=durable.terminal.request_sha256,
            )
            artifacts = _validate_inventory(durable.terminal.artifacts, self._limits)
            descriptors: dict[str, ContentObject] = {}
            for descriptor in (
                *(item.content for item in request.context_manifest.files),
                *(item.content for item in artifacts),
            ):
                previous = descriptors.get(descriptor.key)
                if previous is not None and (
                    previous.sha256 != descriptor.sha256
                    or previous.size != descriptor.size
                ):
                    raise _AuditRefused("content descriptors disagree on size or hash")
                descriptors[descriptor.key] = descriptor
            report = ArtifactVerificationReport(
                run_id=run_id,
                state="refused",
                terminal_sha256=durable.terminal_sha256,
                input_references=len(request.context_manifest.files),
                artifact_references=len(artifacts),
                objects_total=len(descriptors),
                bytes_total=sum(item.size for item in descriptors.values()),
            )
            missing: list[ContentObject] = []
            corrupt: list[ContentObject] = []
            verified = 0
            verified_bytes = 0
            for descriptor in sorted(descriptors.values(), key=lambda item: item.key):
                try:
                    self._store.verify_content(descriptor)
                except ClientError as error:
                    code = str(error.response.get("Error", {}).get("Code", ""))
                    if code not in {"404", "NoSuchKey", "NotFound"}:
                        raise
                    missing.append(descriptor)
                except S3IntegrityError:
                    corrupt.append(descriptor)
                else:
                    verified += 1
                    verified_bytes += descriptor.size
            report = report.model_copy(
                update={
                    "objects_verified": verified,
                    "bytes_verified": verified_bytes,
                    "missing": tuple(missing),
                    "corrupt": tuple(corrupt),
                }
            )
            after = self._terminal(run_id)
            if after.terminal_sha256 != durable.terminal_sha256:
                raise _AuditRefused("terminal identity changed during verification")
            return report.model_copy(
                update={"state": "failed" if missing or corrupt else "verified"}
            )
        except _AuditRefused as error:
            return report.model_copy(update={"reasons": (str(error),)})
        except (
            ArtifactPullRefusedError,
            AuthoritativeBindingError,
            S3IntegrityError,
            TypeError,
            ValueError,
        ) as error:
            return report.model_copy(
                update={"reasons": (f"invalid audit records: {type(error).__name__}",)}
            )
