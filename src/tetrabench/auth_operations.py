"""Private native-CLI lifetime evidence. No argv, output, or credentials retained."""

from __future__ import annotations

import fcntl
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tetrabench.auth_sessions import (
    AuthStateError,
    _check_file,
    private_directory,
    read_private,
    write_private,
)
from tetrabench.run_reference import ProcessIdentity, process_identity


class NativeChild(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    identity: ProcessIdentity
    reaped: bool = False
    return_code: int | None = None


class CliLifetime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[2] = 2
    owner: str = Field(pattern=r"^cli-(?:logout|metadata)-[0-9a-f]{32}$")
    operation: Literal["native-logout", "native-metadata"] = "native-logout"
    process: ProcessIdentity
    lock_device: int
    lock_inode: int
    state: Literal["running", "ambiguous", "stopped"] = "running"
    launching: bool = False
    children: tuple[NativeChild, ...] = ()


def group_quiescent(group: int) -> bool:
    """Inspect only kernel process metadata, never cmdline or environment bytes."""
    try:
        count = 0
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                count += 1
                if count > 65_536:
                    return False
                try:
                    fields = (
                        (Path(entry.path) / "stat")
                        .read_text()
                        .rsplit(")", 1)[1]
                        .split()
                    )
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                    return False
        return True
    except (OSError, ValueError, IndexError):
        return False


class CliOperation:
    def __init__(self, path: Path, record: CliLifetime):
        self.path, self.record = path, record

    def _save(self, **updates) -> None:
        self.record = self.record.model_copy(update=updates)
        write_private(self.path, self.record.model_dump_json().encode())

    def launching(self) -> None:
        if self.record.launching or len(self.record.children) >= 32:
            raise AuthStateError("native CLI launch evidence is incomplete")
        self._save(launching=True)

    def started(self, pid: int) -> None:
        identity = process_identity(pid)
        self._save(
            launching=False,
            children=(*self.record.children, NativeChild(identity=identity)),
        )

    def reaped(self, pid: int, return_code: int) -> None:
        if not group_quiescent(pid):
            raise AuthStateError("native CLI process group is not proven stopped")
        children = tuple(
            child.model_copy(update={"reaped": True, "return_code": return_code})
            if child.identity.pid == pid
            else child
            for child in self.record.children
        )
        self._save(children=children)


_operation: ContextVar[CliOperation | None] = ContextVar(
    "private_native_cli_operation", default=None
)


def current_cli_operation() -> CliOperation | None:
    return _operation.get()


@contextmanager
def cli_logout_lifetime(parent: Path) -> Iterator[str]:
    with cli_operation_lifetime(parent, operation="native-logout") as owner:
        yield owner


@contextmanager
def cli_operation_lifetime(
    parent: Path, *, operation: Literal["native-logout", "native-metadata"]
) -> Iterator[str]:
    directory = private_directory(parent / "operations", create=True)
    owner = "cli-" + operation.removeprefix("native-") + "-" + uuid.uuid4().hex
    path = directory / (owner + ".json")
    lock = directory / (owner + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _check_file(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        current = CliOperation(
            path,
            CliLifetime(
                owner=owner,
                operation=operation,
                process=process_identity(os.getpid()),
                lock_device=info.st_dev,
                lock_inode=info.st_ino,
            ),
        )
        current._save()
        token = _operation.set(current)
        try:
            yield owner
        except BaseException:
            current._save(state="ambiguous")
            raise
        else:
            current._save(state="stopped")
        finally:
            _operation.reset(token)
    finally:
        os.close(fd)


def _absent(identity: ProcessIdentity) -> bool:
    # A reused PID is deliberately refused, not inferred to mean the old owner
    # stopped. Fresh bootstrap must not depend on a weaker identity comparison.
    try:
        process_identity(identity.pid)
    except (FileNotFoundError, ProcessLookupError):
        return True
    return False


def prove_cli_operation_stopped(owner: str, parent: Path) -> bool:
    if re.fullmatch(r"cli-(?:logout|metadata)-[0-9a-f]{32}", owner) is None:
        return False
    try:
        directory = private_directory(parent / "operations")
        path = directory / (owner + ".json")
        data = read_private(path)
        record = CliLifetime.model_validate_json(data)
        if record.owner != owner or record.launching or not record.children:
            return False
        current = process_identity(os.getpid())
        identities = [record.process, *(child.identity for child in record.children)]
        if any(
            (item.boot_id, item.hostname, item.uid)
            != (current.boot_id, current.hostname, current.uid)
            for item in identities
        ):
            return False
        fd = os.open(directory / (owner + ".lock"), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            _check_file(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (record.lock_device, record.lock_inode):
                return False
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for _ in range(2):
                if not all(_absent(item) for item in identities):
                    return False
                if not all(
                    group_quiescent(child.identity.pid) for child in record.children
                ):
                    return False
                if read_private(path) != data:
                    return False
                time.sleep(0.02)
            return True
        finally:
            os.close(fd)
    except (OSError, ValueError, AuthStateError):
        return False
