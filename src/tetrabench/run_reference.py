"""Immutable local routing hints, never remote terminal or admission authority."""

from __future__ import annotations

import ctypes
import os
import socket
import uuid
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
from tetrabench.receipts import ReceiptConflictError, ReceiptStore


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
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_state_path("tetrabench") / "run-references"
        self._files = ReceiptStore(self.root)

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
        with self._files.lock(reference.run_id):
            existing = self.read(reference.run_id)
            if existing is not None:
                if existing != reference:
                    raise ReceiptConflictError(
                        "run ID already has another engine binding"
                    )
                return
            write_private_record(
                self._files.path_for(reference.run_id), canonical_model_bytes(reference)
            )

    def list(self) -> tuple[RunReference, ...]:
        return tuple(
            reference
            for path in sorted(self.root.glob("*.json"))
            if (reference := self.read(path.stem)) is not None
        )
