"""Durable cancellation requests and one owner-side asyncio cancellation."""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal

from tetrabench.models import FrozenRecord
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.run_reference import (
    RunReference,
    process_identity,
    write_private_record,
)


class OwnerControl(FrozenRecord):
    schema_version: Literal[1] = 1
    protocol: Literal["cooperative-v1"] = "cooperative-v1"
    reference: RunReference
    lock_identity: tuple[int, int]


class CancellationRequest(FrozenRecord):
    schema_version: Literal[1] = 1
    reference: RunReference
    state: Literal["requested", "observed"]


def _read[RecordT: FrozenRecord](path: Path, record: type[RecordT]) -> RecordT | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("local control record is not a regular file")
        data = stream.read(64 * 1024 + 1)
    if len(data) > 64 * 1024:
        raise ValueError("local control record exceeds its bound")
    return parse_canonical_model(data, record)


def read_owner_control(output: Path) -> OwnerControl | None:
    return _read(output / "owner-control.json", OwnerControl)


def read_owner_stopped(output: Path) -> OwnerControl | None:
    return _read(output / "owner-stopped.json", OwnerControl)


def initialize_owner(reference: RunReference, lock_identity: tuple[int, int]) -> None:
    output = Path(reference.output_directory or "")
    write_private_record(
        output / "owner-control.json",
        canonical_model_bytes(
            OwnerControl(reference=reference, lock_identity=lock_identity)
        ),
        expected_parent=reference.output_identity,
    )


def read_cancellation(
    reference: RunReference, *, observed: bool = False
) -> CancellationRequest | None:
    name = "cancel-observed.json" if observed else "cancel.json"
    request = _read(Path(reference.output_directory or "") / name, CancellationRequest)
    if request is not None and request.reference != reference:
        raise ValueError("local cancellation request identity changed")
    return request


def publish_cancellation(reference: RunReference, *, observed: bool = False) -> None:
    name = "cancel-observed.json" if observed else "cancel.json"
    write_private_record(
        Path(reference.output_directory or "") / name,
        canonical_model_bytes(
            CancellationRequest(
                reference=reference, state="observed" if observed else "requested"
            )
        ),
        expected_parent=reference.output_identity,
    )


class OwnerCancellation:
    """Keep the SIGINT wrapper installed through asyncio.run's final task draining.

    The signal handler only queues a callback. External cancellers never send a
    signal, so they cannot outrun Ctrl-C's observation record or interrupt cleanup.
    The owner publishes observations without taking the external request lock.
    """

    def __init__(self, control: OwnerControl) -> None:
        self.reference = control.reference
        self.cancelled = False
        self._signal_requested = False
        self._active = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def _on_sigint(self, _signum, _frame) -> None:
        self._signal_requested = True
        if self._loop is not None and self._active:
            self._loop.call_soon_threadsafe(self._cancel_once)

    def _cancel_once(self) -> None:
        if not self._active or self.cancelled:
            return
        self.cancelled = True
        # Failure to persist an observation must not prevent cleanup. The
        # cooperative protocol still precludes all external signal delivery.
        with suppress(OSError):
            publish_cancellation(self.reference, observed=True)
        if self._task is not None:
            self._task.cancel()

    async def _watch(self) -> None:
        while True:
            try:
                pending = read_cancellation(self.reference) is not None
            except (OSError, ValueError):
                # Corrupt control input stops execution; it never authorizes a
                # second cancellation while the native cleanup is running.
                pending = True
            if pending or self._signal_requested:
                self._cancel_once()
            await asyncio.sleep(0.05)

    async def _run[ResultT](
        self, operation: Callable[[], Awaitable[ResultT]]
    ) -> ResultT:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._active = True
        watcher = asyncio.create_task(self._watch())
        try:
            if self._signal_requested or read_cancellation(self.reference) is not None:
                self._cancel_once()
                raise asyncio.CancelledError
            return await operation()
        finally:
            self._active = False
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    def run[ResultT](self, operation: Callable[[], Awaitable[ResultT]]) -> ResultT:
        if threading.current_thread() is not threading.main_thread():
            raise ValueError("local cancellation ownership requires the main thread")
        if self.reference.process != process_identity(os.getpid()):
            raise ValueError("local execution process identity changed")
        output = Path(self.reference.output_directory or "")
        metadata = output.stat()
        if (
            output != output.resolve(strict=True)
            or (metadata.st_dev, metadata.st_ino) != self.reference.output_identity
        ):
            raise ValueError(
                "local execution output identity or canonical path changed"
            )
        previous = signal.signal(signal.SIGINT, self._on_sigint)
        try:
            try:
                return asyncio.run(self._run(operation))
            except asyncio.CancelledError:
                if self.cancelled:
                    raise KeyboardInterrupt from None
                raise
        finally:
            self._active = False
            self._loop = None
            signal.signal(signal.SIGINT, previous)
