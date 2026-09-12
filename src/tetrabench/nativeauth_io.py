"""Sandbox-side bounded private file operations. No third-party dependencies.

Executed as a file uploaded by runtime_auth, not installed in a task image.
Arguments contain paths/actions only. Credentials never enter stdout or argv.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path

LIMIT = 128 * 1024


def check_root(root: Path) -> None:
    if re.fullmatch(r"/tmp/tetrabench-auth-[0-9a-f]{32}", str(root)) is None:  # nosec B108
        raise ValueError
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError


def read_auth(root: Path, source: Path) -> bytes:
    check_root(root)
    if not source.is_relative_to(root):
        raise ValueError
    for parent in source.relative_to(root).parents:
        info = (root / parent).lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError
    return data


def write_auth(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def snapshot(root: Path, source: Path, destination: Path) -> None:
    if destination.parent != root:
        raise ValueError
    write_auth(destination, read_auth(root, source))


def regular_codex_auth(root: Path, *, required: bool = True) -> None:
    """Replace ONLY Harbor 0.22's exact private symlink with a regular file."""
    source = root / "secrets/auth.json"
    target = root / "codex/auth.json"
    check_root(root)
    if not required and not target.exists() and not target.is_symlink():
        return
    if not target.is_symlink():
        read_auth(root, target)
        return
    if os.readlink(target) != str(source):
        raise ValueError
    # Harbor's heredoc/SDK upload can create this file with its default umask.
    # Harden the exact native source under our already-private directory.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise ValueError
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    data = read_auth(root, source)
    temporary = root / "codex/regular-auth.json"
    write_auth(temporary, data)
    os.replace(temporary, target)


def _copy_tree(source: Path, destination: Path, budget: list[int]) -> None:
    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(mode=0o700, exist_ok=True)
        for child in source.iterdir():
            if child.name in {"auth.json", ".credentials.json"}:
                raise ValueError
            _copy_tree(child, destination / child.name, budget)
    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        budget[0] -= info.st_size
        if budget[0] < 0:
            raise ValueError
        shutil.copyfile(source, destination, follow_symlinks=False)
        destination.chmod(0o600)
    else:
        raise ValueError


def export_opencode_evidence(root: Path) -> None:
    """Allowlisted evidence only, never a copy of the auth-bearing XDG root."""
    check_root(root)
    source = root / "data/opencode"
    destination = Path("/logs/agent/opencode/xdg-data/opencode")
    # The pinned OpenCode database and logs share the auth.json directory.
    # Only these native evidence entries are exported, after model return.
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    budget = [256 * 1024 * 1024]
    for name in ("opencode.db", "opencode.db-wal", "opencode.db-shm", "log", "storage"):
        path = source / name
        if path.exists() or path.is_symlink():
            _copy_tree(path, destination / name, budget)


def copy_pi_config(root: Path) -> None:
    check_root(root)
    for name in ("models.json", "settings.json"):
        source = Path("/tmp/harbor-pi-agent") / name  # nosec B108
        if source.exists():
            if not stat.S_ISREG(source.lstat().st_mode) or source.stat().st_nlink != 1:
                raise ValueError
            with source.open("rb") as stream:
                data = stream.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError
            target = root / "pi" / name
            if target.exists() or target.is_symlink():
                read_auth(root, target)
                target.unlink()
            write_auth(target, data)


def verify_metadata_helper(path: Path, digest: str) -> None:
    if (
        re.fullmatch(
            r"/tmp/tetrabench-capability-[0-9a-f]{32}/"  # nosec B108
            r"(?:runtime_metadata|native_control)\.py",
            str(path),
        )
        is None
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
    ):
        raise ValueError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError


def main() -> int:
    try:
        action, path, *args = sys.argv[1:]
        root = Path(path)
        check_root(root)
        if action == "snapshot" and len(args) == 2:
            snapshot(root, Path(args[0]), Path(args[1]))
        elif action == "codex-regular" and not args:
            regular_codex_auth(root)
        elif action == "codex-api" and not args:
            regular_codex_auth(root, required=False)
        elif action == "opencode-evidence" and not args:
            export_opencode_evidence(root)
        elif action == "pi-config" and not args:
            copy_pi_config(root)
        elif action == "remove" and len(args) == 1:
            target = Path(args[0])
            read_auth(root, target)
            target.unlink()
        elif action == "verify-helper" and len(args) == 2:
            verify_metadata_helper(Path(args[0]), args[1])
        else:
            raise ValueError
        return 0
    except Exception:
        print("private native auth file operation failed", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
