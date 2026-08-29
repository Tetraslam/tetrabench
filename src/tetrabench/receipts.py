"""Atomic canonical local submission receipts.

Receipts are recovery aids. Immutable S3 records remain the publication
authority, and Modal FunctionCall remains the active execution authority.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from platformdirs import user_state_path
from pydantic import Field, model_validator

from tetrabench.models import FrozenRecord, NonEmptyString, RecordIdentifier, Sha256
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.records import RunId


class SubmissionTransition(FrozenRecord):
    sequence: Annotated[int, Field(ge=0, le=(1 << 53) - 1)]
    type: Literal["admission-observed", "spawn-returned"]


class ControllerCallReceipt(FrozenRecord):
    call_id: NonEmptyString
    source: Literal["spawn-return"] = "spawn-return"


class PhysicalSubmissionAttempt(FrozenRecord):
    attempt_id: RecordIdentifier
    transitions: tuple[SubmissionTransition, ...]
    controller_calls: tuple[ControllerCallReceipt, ...] = ()

    @model_validator(mode="after")
    def validate_history(self) -> PhysicalSubmissionAttempt:
        expected = ("admission-observed", "spawn-returned")
        events = tuple(item.type for item in self.transitions)
        if not events or events != expected[: len(events)]:
            raise ValueError("submission transitions must be an ordered event prefix")
        if tuple(item.sequence for item in self.transitions) != tuple(
            range(len(self.transitions))
        ):
            raise ValueError("submission transition sequence must be contiguous")
        if len(self.controller_calls) > 1:
            raise ValueError("one spawn attempt can record only one controller call")
        if events[-1] == "spawn-returned" and not self.controller_calls:
            raise ValueError("spawn-returned requires controller call evidence")
        if self.controller_calls and events[-1] != "spawn-returned":
            raise ValueError("controller call evidence requires spawn-returned")
        return self


class SubmissionReceipt(FrozenRecord):
    schema_version: Literal[2]
    run_id: RunId
    request_sha256: Sha256
    plan_sha256: Sha256
    context_manifest_sha256: Sha256
    attempts: tuple[PhysicalSubmissionAttempt, ...]

    @model_validator(mode="after")
    def validate_history(self) -> SubmissionReceipt:
        if not self.attempts:
            raise ValueError("receipt requires a physical submission attempt")
        attempt_ids = [item.attempt_id for item in self.attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("physical submission attempt IDs must be unique")
        return self


def default_receipt_root() -> Path:
    return user_state_path("tetrabench") / "receipts"


class ReceiptConflictError(RuntimeError):
    """A local receipt update would discard or rewrite durable evidence."""


class ReceiptStore:
    """Canonical receipt files replaced atomically with file and directory fsync."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_receipt_root()

    def path_for(self, run_id: str) -> Path:
        from tetrabench.records import validate_run_id

        return self.root / f"{validate_run_id(run_id)}.json"

    def read(self, run_id: str) -> SubmissionReceipt | None:
        path = self.path_for(run_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        return parse_canonical_model(data, SubmissionReceipt)

    def list(self) -> tuple[SubmissionReceipt, ...]:
        if not self.root.exists():
            return ()
        receipts = [
            parse_canonical_model(path.read_bytes(), SubmissionReceipt)
            for path in sorted(self.root.glob("*.json"))
        ]
        return tuple(receipts)

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        """Serialize one run's local transition and spawn decision."""
        import fcntl

        receipt_path = self.path_for(run_id)
        self._ensure_root()
        lock_path = receipt_path.with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def write(self, receipt: SubmissionReceipt) -> None:
        existing = self.read(receipt.run_id)
        if existing is not None:
            self._validate_append_only(existing, receipt)
        self._ensure_root()
        data = canonical_model_bytes(receipt)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{receipt.run_id}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path_for(receipt.run_id))
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _ensure_root(self) -> None:
        missing: list[Path] = []
        current = self.root
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            parent = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _validate_append_only(
        existing: SubmissionReceipt,
        replacement: SubmissionReceipt,
    ) -> None:
        identity = (
            "run_id",
            "request_sha256",
            "plan_sha256",
            "context_manifest_sha256",
        )
        if any(
            getattr(existing, field) != getattr(replacement, field)
            for field in identity
        ):
            raise ReceiptConflictError("receipt identity cannot change")
        if replacement.attempts[: len(existing.attempts)] != existing.attempts:
            if (
                len(replacement.attempts) == len(existing.attempts)
                and replacement.attempts[:-1] == existing.attempts[:-1]
            ):
                old = existing.attempts[-1]
                new = replacement.attempts[-1]
                if (
                    old.attempt_id != new.attempt_id
                    or new.transitions[: len(old.transitions)] != old.transitions
                    or new.controller_calls[: len(old.controller_calls)]
                    != old.controller_calls
                ):
                    raise ReceiptConflictError("submission evidence is append-only")
            else:
                raise ReceiptConflictError("physical attempt history is append-only")


def append_submission_attempt(
    receipt: SubmissionReceipt,
    attempt: PhysicalSubmissionAttempt,
) -> SubmissionReceipt:
    return SubmissionReceipt.model_validate(
        receipt.model_dump() | {"attempts": (*receipt.attempts, attempt)}
    )


def record_spawn_return(
    receipt: SubmissionReceipt,
    call: ControllerCallReceipt,
) -> SubmissionReceipt:
    attempt = receipt.attempts[-1]
    if tuple(item.type for item in attempt.transitions) != ("admission-observed",):
        raise ReceiptConflictError(
            "latest receipt attempt is not awaiting spawn return"
        )
    updated_attempt = attempt.model_copy(
        update={
            "transitions": (
                *attempt.transitions,
                SubmissionTransition(sequence=1, type="spawn-returned"),
            ),
            "controller_calls": (call,),
        }
    )
    return SubmissionReceipt.model_validate(
        receipt.model_dump() | {"attempts": (*receipt.attempts[:-1], updated_attempt)}
    )
