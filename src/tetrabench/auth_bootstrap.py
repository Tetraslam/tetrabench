"""Private local profile creation. The config lock never spans native login."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess  # nosec B404
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import tomlkit

from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    LocalSessionStore,
    SessionState,
    StateRead,
    _check_file,
    private_directory,
    read_private,
    write_private,
)


def ensure_private_directory(path: Path) -> Path:
    for _ in range(8):
        try:
            return private_directory(path, create=True)
        except FileExistsError:
            pass  # A concurrent creator must still pass all ownership checks.
    raise AuthBusyError("private directory creation changed concurrently")


def require_local_filesystem(path: Path) -> None:
    """Automatic local bootstrap cannot attest that an NFS/FUSE home is local."""
    try:
        executable = shutil.which("stat", path=os.defpath)
        if executable is None:
            raise ValueError
        # statfs-backed coreutils handles Btrfs subvolumes, whose st_dev may
        # differ from mountinfo. No shell or credential values cross this call.
        result = subprocess.run(  # nosec B603
            [executable, "--file-system", "--format=%T", "--", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "LC_ALL": "C"},
            timeout=5,
            check=False,
        )
        if result.returncode or result.stdout.strip() not in {
            b"ext2/ext3",
            b"xfs",
            b"btrfs",
            b"tmpfs",
            b"overlayfs",
            b"zfs",
            b"f2fs",
        }:
            raise ValueError
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise AuthError(
            "private auth bootstrap requires a verified local Linux filesystem "
            "and coreutils stat"
        ) from None


@contextmanager
def config_lock(path: Path, *, timeout: float = 3) -> Iterator[None]:
    ensure_private_directory(path.parent)
    require_local_filesystem(path.parent)
    lock = path.with_name(path.name + ".lock")
    try:
        try:
            fd = os.open(
                lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        else:
            os.fchmod(fd, 0o600)
        try:
            _check_file(fd)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AuthBusyError(
                            "private auth configuration is busy; no login started"
                        ) from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)
    except OSError:
        raise AuthError("cannot lock private auth configuration") from None


def _state_root(environment: Mapping[str, str]) -> Path:
    home = Path(environment.get("HOME", str(Path.home())))
    return (
        Path(environment.get("XDG_STATE_HOME", str(home / ".local/state")))
        / "tetrabench/auth"
    )


def default_runtime_directory(environment: Mapping[str, str]) -> Path:
    runtime = environment.get("XDG_RUNTIME_DIR")
    return (
        Path(runtime) / "tetrabench/auth"
        if runtime
        else _state_root(environment) / "runtime"
    )


def bootstrap_profile(
    path: Path, name: str, agent: str, backend, environment: Mapping[str, str]
):
    # Import lazily: auth_profiles owns schemas; this module owns only writes.
    from tetrabench.auth_profiles import (
        LocalAuthBackend,
        NativeAuthProfile,
        parse_auth_config_file,
        profile_runtime_directory,
    )

    with config_lock(path):
        exists = path.exists() or path.is_symlink()
        if exists:
            data = read_private(path)
            config = parse_auth_config_file(data)
            current = config.profiles.get(name)
            if current is not None:
                if current.harness != agent or (
                    backend is not None and current.backend != backend
                ):
                    raise AuthError(
                        "existing auth profile cannot be rebound; "
                        "choose a new profile name"
                    )
                return config
            document = tomlkit.parse(data.decode())
        else:
            document = tomlkit.document()
            document["schema_version"] = 1
            document["runtime_directory"] = str(default_runtime_directory(environment))
            document["profiles"] = tomlkit.table()
        binding = "auth-" + uuid.uuid4().hex
        if backend is None:
            backend = LocalAuthBackend(
                kind="local",
                approved_private_backend=True,
                state_directory=str(
                    _state_root(environment) / "profiles" / name / binding
                ),
            )
        profile = NativeAuthProfile.model_validate(
            {"harness": agent, "binding": binding, "backend": backend}
        )
        document["profiles"][name] = profile.model_dump(mode="json", exclude_none=True)
        encoded = tomlkit.dumps(document).encode()
        config = parse_auth_config_file(encoded)
        # These are local metadata operations, not network or browser work.
        for directory in [
            profile_runtime_directory(config, environment=environment),
            *(
                [Path(backend.state_directory)]
                if isinstance(backend, LocalAuthBackend)
                else []
            ),
        ]:
            ensure_private_directory(directory)
            require_local_filesystem(directory)
        write_private(path, encoded)
        read_private(path)  # Require a readable mode-0600 result before login.
        return config


class _LocalReader(LocalSessionStore):
    """Reuse the authority's atomic-file decoder without creating files or locks."""

    def __init__(self, root: Path, binding: str):
        self.root = private_directory(root)
        self.binding = binding

    def read(self, profile: str) -> StateRead | None:
        return self._read(profile)

    def compare_and_swap(
        self, profile: str, version: str | None, state: SessionState
    ) -> StateRead:
        raise AuthError("selected profile resolution is read-only")


def read_local_current(root: Path, binding: str, name: str) -> StateRead | None:
    if not root.exists() and not root.is_symlink():
        return None
    return _LocalReader(root, binding).read(name)
