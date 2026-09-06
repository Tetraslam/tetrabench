"""Native Docker evidence scoped to a run's private Compose working directories."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess  # nosec B404
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

from tetrabench.local_control import read_owner_control, read_owner_stopped
from tetrabench.models import FrozenRecord, NonEmptyString
from tetrabench.plan import canonical_model_bytes, parse_canonical_model
from tetrabench.run_reference import write_private_record


class DockerBinding(FrozenRecord):
    schema_version: Literal[1] = 1
    daemon_id: NonEmptyString


def _docker(*args: str) -> str:
    try:
        # Native Docker CLI: fixed executable, no shell, identifiers passed as data.
        completed = subprocess.run(  # nosec B603, B607
            ["docker", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Docker lifecycle inspection or cleanup failed") from error
    if len(completed.stdout) > 8 * 1024 * 1024:
        raise RuntimeError("Docker lifecycle response exceeds its bound")
    return completed.stdout.strip()


def bind_docker(output: Path) -> None:
    """Called immediately before native Job.create, never by validation or planning."""
    binding = DockerBinding(daemon_id=_docker("info", "--format", "{{.ID}}"))
    write_private_record(output / "docker-binding.json", canonical_model_bytes(binding))


@contextmanager
def execution_owner(output: Path) -> Iterator[tuple[int, int]]:
    fd = os.open(
        output / "execution.lock",
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    acquired = False
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        metadata = os.fstat(fd)
        yield metadata.st_dev, metadata.st_ino
    finally:
        # A killed owner releases flock too. Only a normal context unwind writes
        # this witness; a released lock alone cannot prove the execution finished.
        try:
            with suppress(OSError, ValueError):
                control = read_owner_control(output)
                held = os.fstat(fd)
                if (
                    acquired
                    and control is not None
                    and control.lock_identity == (held.st_dev, held.st_ino)
                ):
                    write_private_record(
                        output / "owner-stopped.json",
                        canonical_model_bytes(control),
                        expected_parent=control.reference.output_identity,
                    )
        finally:
            os.close(fd)


def owner_active(output: Path) -> bool | None:
    """None means this older run lacks the lifetime witness, not that it is stopped."""
    try:
        fd = os.open(output / "execution.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        control = read_owner_control(output)
        metadata = os.fstat(fd)
        if (
            control is not None
            and (metadata.st_dev, metadata.st_ino) != control.lock_identity
        ):
            raise ValueError("execution lock identity changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False if control is not None else None
    finally:
        os.close(fd)


def _owned_containers(output: Path, binding: DockerBinding) -> tuple[str, ...]:
    if _docker("info", "--format", "{{.ID}}") != binding.daemon_id:
        raise ValueError(
            "Docker daemon changed; select the run's original Docker context"
        )
    ids = _docker(
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        "label=com.docker.compose.project.working_dir",
        "--format",
        "{{.ID}}",
    ).splitlines()
    if len(ids) > 10000 or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
        raise ValueError("invalid or excessive Docker container inventory")
    owned = []
    context = output / "context"
    for container_id in ids:
        # Docker's reserved Compose labels bind both agent and separate-verifier
        # containers (including sidecars) to this run's exclusive fixture copy.
        labels = json.loads(
            _docker("inspect", "--format", "{{json .Config.Labels}}", container_id)
        )
        if not isinstance(labels, dict):
            raise ValueError("Docker returned invalid container labels")
        working_dir = labels.get("com.docker.compose.project.working_dir")
        if not isinstance(working_dir, str):
            continue
        path = Path(working_dir)
        if (
            path.is_absolute()
            and path == Path(os.path.normpath(working_dir))
            and path.is_relative_to(context)
            and path != context
        ):
            if not labels.get("com.docker.compose.project") or not labels.get(
                "com.docker.compose.service"
            ):
                raise ValueError("owned Docker container lacks native Compose identity")
            owned.append(container_id)
    return tuple(owned)


def _compose_client_active(output: Path) -> bool:
    """An orphaned native Compose client can create children after Python exits."""
    context = output / "context"
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.geteuid():
                continue
            with (process / "cmdline").open("rb") as stream:
                command = stream.read(1024 * 1024 + 1)
            if len(command) > 1024 * 1024:
                raise ValueError("process command line exceeds its bound")
            args = os.fsdecode(command).split("\0")
            if not args or Path(args[0]).name not in {"docker", "docker-compose"}:
                continue
            if "--project-directory" in args:
                index = args.index("--project-directory") + 1
                if index >= len(args):
                    raise ValueError("native Compose client has no project directory")
                path = Path(args[index])
                if path.is_relative_to(context):
                    return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


def _verify_output(output: Path, identity: tuple[int, int]) -> None:
    if output != output.resolve(strict=True):
        raise ValueError("local output has a symlink ancestor or traversal")
    metadata = output.stat()
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise ValueError("local output identity changed during cleanup")


@contextmanager
def _stopped_owner(output: Path, identity: tuple[int, int]) -> Iterator[int | None]:
    control = read_owner_control(output)
    if control is None:
        yield None
        return
    if (
        control.reference.output_directory != str(output)
        or control.reference.output_identity != identity
    ):
        raise ValueError("local cleanup control and output identity disagree")
    fd = os.open(output / "execution.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (metadata.st_dev, metadata.st_ino) != control.lock_identity:
            raise ValueError("execution lock identity changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield fd
    finally:
        os.close(fd)


def _verify_lock(output: Path, fd: int) -> None:
    held = os.fstat(fd)
    visible = (output / "execution.lock").lstat()
    if (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino):
        raise ValueError("execution lock was replaced during cleanup")


def cleanup_containers(
    output: Path, *, remove: bool = False, identity: tuple[int, int] | None = None
) -> bool:
    """Prove two empty container inventories after the local execution owner exits.

    This covers child compute, not shared images or user-declared persistent volumes.
    Missing old binding/owner evidence cannot prove cleanup. No container is touched
    while Harbor may still create children or while its shielded teardown is active.
    """
    output = output.expanduser().absolute()
    metadata = output.stat()
    identity = identity or (metadata.st_dev, metadata.st_ino)
    _verify_output(output, identity)
    with _stopped_owner(output, identity) as lock_fd:
        if lock_fd is None or _compose_client_active(output):
            return False
        try:
            stopped = read_owner_stopped(output)
            closed_scope = stopped is not None and stopped == read_owner_control(output)
        except (OSError, ValueError):
            closed_scope = False
        if not closed_scope and not remove:
            return False
        try:
            binding = parse_canonical_model(
                (output / "docker-binding.json").read_bytes(), DockerBinding
            )
        except FileNotFoundError:
            return False
        for _ in range(2):
            _verify_output(output, identity)
            _verify_lock(output, lock_fd)
            if _compose_client_active(output):
                return False
            containers = _owned_containers(output, binding)
            if containers:
                if not remove:
                    return False
                for container_id in containers:
                    _verify_output(output, identity)
                    _verify_lock(output, lock_fd)
                    if _compose_client_active(output):
                        return False
                    if _docker("info", "--format", "{{.ID}}") != binding.daemon_id:
                        raise ValueError("Docker daemon changed before removal")
                    _docker("rm", "--force", container_id)
                if _owned_containers(output, binding):
                    return False
            time.sleep(0.05)
        _verify_output(output, identity)
        _verify_lock(output, lock_fd)
        return closed_scope and not _compose_client_active(output)


def observe_cleanup(output: Path, *, identity: tuple[int, int] | None = None) -> bool:
    """Read failures do not erase a valid native result or establish cleanup."""
    try:
        return cleanup_containers(output, identity=identity)
    except (OSError, RuntimeError, ValueError):
        return False
