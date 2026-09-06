"""Immutable local routing hints, never remote terminal or admission authority."""

from __future__ import annotations

import ctypes
import os
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from platformdirs import user_state_path
from pydantic import Field, model_validator

from tetrabench.models import (
    FrozenRecord,
    RecordIdentifier,
    ResolvedStorageConfig,
    Sha256,
)
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.receipts import ReceiptStore, RunIdentityConflictError

_ADMISSIONS = threading.local()


class ProcessIdentity(FrozenRecord):
    pid: int = Field(gt=0)
    start_ticks: str
    boot_id: str
    hostname: str
    uid: int = Field(ge=0)


def process_identity(pid: int) -> ProcessIdentity:
    # comm may contain spaces and parentheses; fields after its final ')' are fixed.
    process = Path(f"/proc/{pid}")
    fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
    return ProcessIdentity(
        pid=pid,
        start_ticks=fields[19],
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        hostname=socket.gethostname(),
        uid=process.stat().st_uid,
    )


def open_process_handle(pid: int) -> int:
    """Use libc's pidfd API when the standalone Python build omits its wrappers."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "pidfd_open", None)
    if function is None:
        raise ValueError("safe local cancellation requires Linux pidfds")
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    fd = function(pid, 0)
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return fd


def send_process_signal(fd: int, signum: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "pidfd_send_signal", None)
    if function is None:
        raise ValueError("safe local cancellation requires Linux pidfds")
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(fd, signum, None, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


class RunReference(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RecordIdentifier
    engine: RecordIdentifier
    request_sha256: Sha256
    storage: ResolvedStorageConfig | None = None
    app_name: str | None = None
    function_name: str | None = None
    environment_name: str | None = None
    output_directory: str | None = None
    output_identity: tuple[int, int] | None = None
    process: ProcessIdentity | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> RunReference:
        if self.engine == "modal" and (
            self.storage is None
            or not self.app_name
            or not self.function_name
            or not self.environment_name
        ):
            raise ValueError(
                "Modal run reference requires storage and controller binding"
            )
        if self.engine == "docker" and (
            not self.output_directory or self.process is None
        ):
            raise ValueError(
                "Docker run reference requires output and process identity"
            )
        if (
            self.output_directory is not None
            and not Path(self.output_directory).is_absolute()
        ):
            raise ValueError("run output reference must be absolute")
        return self


def write_private_record(
    path: Path,
    data: bytes,
    *,
    expected_parent: tuple[int, int] | None = None,
) -> None:
    """Atomic, private replacement of one local execution observation."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        metadata = os.fstat(directory)
        if (
            expected_parent is not None
            and (metadata.st_dev, metadata.st_ino) != expected_parent
        ):
            raise OSError("local output directory identity changed")
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory)


class RunReferenceStore:
    """One reference/receipt namespace, using the receipt store's admission lock.

    Custom layouts must use the paired ``receipts`` store for receipt writes;
    the default layout is sibling ``run-references`` and ``receipts`` directories.
    """

    def __init__(
        self, root: Path | None = None, *, receipts: ReceiptStore | None = None
    ) -> None:
        self.root = root or user_state_path("tetrabench") / "run-references"
        self.receipts = receipts or ReceiptStore(
            self.root.parent / "receipts", reference_root=self.root
        )
        if self.receipts.reference_root.resolve() != self.root.resolve():
            raise ValueError(
                "receipt and reference stores belong to different namespaces"
            )
        self._files = ReceiptStore(self.root)

    @contextmanager
    def admission(
        self,
        run_id: str,
        *,
        engine: str,
        request_sha256: str,
        require_new: bool = False,
        tolerate_unreadable_reference: bool = False,
        tolerate_unreadable_receipt: bool = False,
    ) -> Iterator[None]:
        """Check both record families while excluding all cooperative writers.

        Hold through reference publication or receipt replacement, not merely
        through this check. Valid Modal evidence for the same request is reusable.
        Corrupt hints may be ignored only for receipt evidence/explicit recovery;
        they are never overwritten here and never establish execution authority.
        """
        with self.receipts.lock(run_id):
            key = (
                os.getpid(),
                str(self.receipts.path_for(run_id).with_suffix(".lock").resolve()),
            )
            pending = getattr(_ADMISSIONS, "pending", None)
            if pending is None:
                pending = _ADMISSIONS.pending = {}
            identity = (engine, request_sha256)
            outer = pending.get(key)
            if outer is not None and outer != identity:
                raise RunIdentityConflictError(
                    "run ID is being admitted by another engine or request"
                )
            try:
                reference = self.read(run_id)
            except (OSError, ValueError):
                if not tolerate_unreadable_reference:
                    raise
                reference = None
            if reference is not None and (
                require_new
                or reference.engine != engine
                or reference.request_sha256 != request_sha256
            ):
                raise RunIdentityConflictError(
                    f"run ID already has another engine binding: {run_id}"
                )
            try:
                receipt = self.receipts.read(run_id)
            except (OSError, ValueError):
                if not tolerate_unreadable_receipt:
                    raise
                receipt = None
            if receipt is not None and (
                require_new
                or engine != "modal"
                or receipt.run_id != run_id
                or receipt.request_sha256 != request_sha256
            ):
                raise RunIdentityConflictError(
                    f"run ID already belongs to a Modal receipt: {run_id}"
                )
            if outer is None:
                pending[key] = identity
            try:
                yield
            finally:
                if outer is None:
                    del pending[key]

    def read(self, run_id: str) -> RunReference | None:
        path = self._files.path_for(run_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        reference = parse_canonical_model(data, RunReference)
        if reference.run_id != run_id:
            raise ValueError("run reference filename and identity disagree")
        return reference

    def create(self, reference: RunReference) -> None:
        with self.admission(
            reference.run_id,
            engine=reference.engine,
            request_sha256=reference.request_sha256,
        ):
            existing = self.read(reference.run_id)
            if existing is not None:
                if existing != reference:
                    raise RunIdentityConflictError(
                        "run ID already has another engine binding"
                    )
                return
            self._files._ensure_root()
            write_private_record(
                self._files.path_for(reference.run_id), canonical_model_bytes(reference)
            )

    def list(self) -> tuple[RunReference, ...]:
        return tuple(
            reference
            for path in sorted(self.root.glob("*.json"))
            if (reference := self.read(path.stem)) is not None
        )
