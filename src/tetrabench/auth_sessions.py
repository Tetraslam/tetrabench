"""Private credential-state authority, independent of run/artifact storage.

A claim never expires. After a crash or ambiguous write, no credential copy may
be reused automatically. An operator must prove the previous consumer stopped
and reseed using a NEW native login. File locking is local-machine only; it is
not a distributed lock and must never be used on Modal Volume/NFS mounts.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol

from tetrabench.auth_config import NativeAuthReference

MAX_AUTH_BYTES = 128 * 1024
MAX_STATE_BYTES = 192 * 1024
_OWNER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_AUTH_FAILURE_ACTIONS = {
    "auth-runtime": "check-auth-profile-and-native-runtime",
    "auth-path-not-absolute": "select-an-absolute-private-auth-directory",
    "auth-directory-missing": "create-the-approved-private-auth-directory",
    "auth-directory-identity": "select-an-owned-directory-without-symlinks",
    "auth-parent-writable": "select-private-auth-staging-outside-writable-ancestors",
    "auth-directory-permissions": "require-owned-mode-0700-auth-directory",
    "auth-output-overlap": "move-auth-staging-outside-run-output-and-volumes",
    "auth-runtime-location": "select-private-ephemeral-home-or-trusted-temp-storage",
}


class AuthError(RuntimeError):
    """Safe public error. Never attach native output or credential values."""

    def __init__(self, message: str, *, reason: str = "auth-runtime"):
        super().__init__(message)
        self.reason = (
            reason
            if isinstance(reason, str) and reason in _AUTH_FAILURE_ACTIONS
            else "auth-runtime"
        )


def auth_failure_diagnostic(error: BaseException) -> dict[str, str] | None:
    """Fixed, allowlisted reason/action only; never stringify provider errors."""
    if not isinstance(error, AuthError):
        return None
    reason = getattr(error, "reason", "auth-runtime")
    if not isinstance(reason, str) or reason not in _AUTH_FAILURE_ACTIONS:
        reason = "auth-runtime"
    return {"reason": reason, "action": _AUTH_FAILURE_ACTIONS[reason]}


class AuthBusyError(AuthError):
    pass


class AuthStateError(AuthError):
    pass


def private_directory(path: Path, *, create: bool = False) -> Path:
    """Reject symlinks at every component and unsafe ownership/modes."""
    if not path.is_absolute() or ".." in path.parts:
        raise AuthStateError(
            "auth directory must be an absolute normalized path",
            reason="auth-path-not-absolute",
        )
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise AuthStateError(
                    "private auth directory does not exist",
                    reason="auth-directory-missing",
                ) from None
            current.mkdir(mode=0o700)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
            raise AuthStateError(
                "unsafe auth directory ownership or type",
                reason="auth-directory-identity",
            )
        if info.st_mode & 0o022 and not (
            info.st_uid == 0 and info.st_mode & stat.S_ISVTX and current != path
        ):
            raise AuthStateError(
                "auth directory has writable ancestors", reason="auth-parent-writable"
            )
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AuthStateError(
            "private auth directory must be owned mode 0700",
            reason="auth-directory-permissions",
        )
    return path


def _check_file(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise AuthStateError("auth file must be owned single-link regular mode 0600")


def read_private(path: Path, *, limit: int = MAX_AUTH_BYTES) -> bytes:
    private_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            _check_file(stream.fileno())
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise AuthStateError("auth state exceeds size limit")
        return data
    except OSError:
        raise AuthStateError("cannot read private auth file") from None


def write_private(path: Path, data: bytes) -> None:
    private_directory(path.parent)
    if len(data) > MAX_STATE_BYTES:
        raise AuthStateError("auth state exceeds size limit")
    if path.exists() or path.is_symlink():
        read_private(path, limit=MAX_STATE_BYTES)
    temporary = path.parent / (".auth-write-" + uuid.uuid4().hex)
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError:
        raise AuthStateError(
            "private auth write failed; ownership is ambiguous"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def private_json(data: bytes) -> dict:
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if len(data) > MAX_STATE_BYTES:
            raise ValueError
        result = json.loads(data, object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise AuthStateError("invalid private auth document") from None


@dataclass(frozen=True, repr=False)
class SessionState:
    profile: str
    harness: str
    binding: str
    generation: int
    revision: int
    phase: Literal["ready", "claimed", "logged_out"]
    owner: str | None = None
    native: bytes = field(default=b"", repr=False)
    consumer_run_id: str | None = None

    def encode(self) -> bytes:
        return json.dumps(
            {
                "schema_version": 1,
                "profile": self.profile,
                "harness": self.harness,
                "binding": self.binding,
                "generation": self.generation,
                "revision": self.revision,
                "phase": self.phase,
                "owner": self.owner,
                "native": base64.b64encode(self.native).decode("ascii"),
                "consumer_run_id": self.consumer_run_id,
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def decode(cls, data: bytes) -> SessionState:
        value = private_json(data)
        try:
            schema_version = value.pop("schema_version")
            if type(schema_version) is not int or schema_version != 1:
                raise ValueError
            native = base64.b64decode(value.pop("native"), validate=True)
            state = cls(native=native, **value)
            if state.consumer_run_id is not None:
                NativeAuthReference(
                    profile=state.consumer_run_id, binding=state.binding, generation=1
                )
            NativeAuthReference(
                profile=state.profile,
                binding=state.binding,
                generation=state.generation,
            )
            if (
                state.harness not in {"codex", "opencode", "pi"}
                or type(state.revision) is not int
                or state.revision < 0
                or state.phase not in {"ready", "claimed", "logged_out"}
                or len(native) > MAX_AUTH_BYTES
                or (state.phase == "claimed") != (state.owner is not None)
                or (
                    state.owner is not None
                    and (
                        not isinstance(state.owner, str)
                        or _OWNER_ID.fullmatch(state.owner) is None
                    )
                )
                or (state.phase == "logged_out" and native)
            ):
                raise ValueError
            return state
        except (ValueError, TypeError, KeyError):
            raise AuthStateError("invalid credential session state") from None


@dataclass(frozen=True, repr=False)
class StateRead:
    state: SessionState
    version: str


class SessionStore(Protocol):
    """CAS must be linearizable, private, durable and never silently retried."""

    binding: str

    def read(self, profile: str) -> StateRead | None: ...
    def compare_and_swap(
        self, profile: str, version: str | None, state: SessionState
    ) -> StateRead: ...


class LocalSessionStore:
    """Explicit single-machine authority. Never construct for distributed mounts."""

    def __init__(self, root: Path, *, binding: str, local_filesystem: bool):
        if not local_filesystem:
            raise AuthStateError(
                "distributed filesystem credential authority is unproven"
            )
        NativeAuthReference(profile="validate", binding=binding, generation=1)
        self.root = private_directory(root, create=True)
        self.binding = binding

    def _path(self, profile: str) -> Path:
        NativeAuthReference(profile=profile, binding=self.binding, generation=1)
        return self.root / f"{profile}.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        private_directory(self.root)
        try:
            fd = os.open(
                self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
        except OSError:
            raise AuthStateError("cannot lock private auth authority") from None
        try:
            _check_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self, profile: str) -> StateRead | None:
        path = self._path(profile)
        if not path.exists() and not path.is_symlink():
            return None
        data = private_json(read_private(path, limit=MAX_STATE_BYTES))
        try:
            version = data["version"]
            if not isinstance(version, str) or len(version) != 32:
                raise ValueError
            state = SessionState.decode(json.dumps(data["state"]).encode())
            if state.profile != profile or state.binding != self.binding:
                raise ValueError
            return StateRead(state, version)
        except (KeyError, ValueError, TypeError):
            raise AuthStateError("private authority binding mismatch") from None

    def read(self, profile: str) -> StateRead | None:
        with self._lock():
            return self._read(profile)

    def compare_and_swap(
        self, profile: str, version: str | None, state: SessionState
    ) -> StateRead:
        with self._lock():
            current = self._read(profile)
            if (current.version if current else None) != version:
                raise AuthBusyError(
                    "credential authority changed; do not retry this claim"
                )
            if state.profile != profile or state.binding != self.binding:
                raise AuthStateError("credential authority binding mismatch")
            new_version = uuid.uuid4().hex
            write_private(
                self._path(profile),
                json.dumps(
                    {
                        "version": new_version,
                        "state": private_json(state.encode()),
                    },
                    separators=(",", ":"),
                ).encode(),
            )
            return StateRead(state, new_version)


def _bound(store: SessionStore, ref: NativeAuthReference, harness: str) -> StateRead:
    if store.binding != ref.binding:
        raise AuthStateError("auth reference belongs to a different runtime binding")
    current = store.read(ref.profile)
    if current is None:
        raise AuthStateError("native auth profile is not initialized")
    state = current.state
    if (
        state.binding != ref.binding
        or state.harness != harness
        or state.generation != ref.generation
        or state.profile != ref.profile
    ):
        raise AuthStateError("stale credential generation or native harness binding")
    return current


def check_seed_target(
    store: SessionStore,
    ref: NativeAuthReference,
    harness: str,
    *,
    previous_generation: int | None = None,
    stopped_owner: Callable[[str], bool] | None = None,
) -> StateRead | None:
    """Check before browser login, then again before publishing its result."""
    if store.binding != ref.binding:
        raise AuthStateError("auth reference belongs to a different runtime binding")
    current = store.read(ref.profile)
    if current:
        old = current.state
        if (
            old.generation != previous_generation
            or ref.generation != old.generation + 1
            or old.harness != harness
            or old.binding != ref.binding
        ):
            raise AuthStateError("reseed requires the next credential generation")
        if old.owner and (
            stopped_owner is None or stopped_owner(old.owner) is not True
        ):
            raise AuthBusyError("previous credential consumer is not proven stopped")
    elif previous_generation is not None or ref.generation != 1:
        raise AuthStateError("new credential profile must start at generation one")
    return current


def seed_session(
    store: SessionStore,
    ref: NativeAuthReference,
    harness: str,
    native: bytes,
    *,
    previous_generation: int | None = None,
    stopped_owner: Callable[[str], bool] | None = None,
) -> None:
    """Publish a fresh native login; never import an old interactive auth copy.

    Recovery needs an authoritative stopped-owner check supplied by the runtime
    integration. Passing time or acquiring a filesystem lock is not such proof.
    """
    from tetrabench.nativeauth import inspect_native_store

    inspect_native_store(harness, native)
    current = check_seed_target(
        store,
        ref,
        harness,
        previous_generation=previous_generation,
        stopped_owner=stopped_owner,
    )
    if current and current.state.native == native:
        raise AuthStateError("reseed requires a fresh native login, not the old state")
    state = SessionState(
        ref.profile, harness, ref.binding, ref.generation, 0, "ready", native=native
    )
    store.compare_and_swap(ref.profile, current.version if current else None, state)


@dataclass(repr=False)
class SessionClaim:
    store: SessionStore
    snapshot: StateRead
    closed: bool = False

    def checkpoint(self, native: bytes) -> None:
        """Durably retain native refresh while ownership remains claimed."""
        from tetrabench.nativeauth import inspect_native_store

        if self.closed:
            raise AuthStateError("credential claim is closed")
        inspect_native_store(self.snapshot.state.harness, native)
        state = replace(
            self.snapshot.state,
            native=native,
            revision=self.snapshot.state.revision + 1,
        )
        try:
            self.snapshot = self.store.compare_and_swap(
                state.profile,
                self.snapshot.version,
                state,
            )
        except BaseException:
            self.closed = True
            raise

    def preserve_blocked(self, native: bytes) -> None:
        """Retain observed writeback without claiming refresh completed safely."""
        from tetrabench.nativeauth import inspect_native_store

        if self.closed:
            raise AuthStateError("credential claim is already closed")
        inspect_native_store(self.snapshot.state.harness, native)
        self.closed = True
        state = replace(self.snapshot.state, native=native)
        self.store.compare_and_swap(state.profile, self.snapshot.version, state)

    def finish(self, native: bytes, *, consumer_stopped: bool) -> None:
        from tetrabench.nativeauth import inspect_native_store

        if self.closed or not consumer_stopped:
            raise AuthStateError("credential release requires stopped native consumer")
        inspect_native_store(self.snapshot.state.harness, native)
        # Mark closed before CAS. An ambiguous CAS is never replayed.
        self.closed = True
        state = replace(
            self.snapshot.state,
            revision=self.snapshot.state.revision + 1,
            phase="ready",
            owner=None,
            native=native,
        )
        self.store.compare_and_swap(state.profile, self.snapshot.version, state)

    def logout(self, *, consumer_stopped: bool) -> None:
        if self.closed or not consumer_stopped:
            raise AuthStateError("credential logout requires stopped native consumer")
        self.closed = True
        state = replace(
            self.snapshot.state,
            revision=self.snapshot.state.revision + 1,
            phase="logged_out",
            owner=None,
            native=b"",
        )
        self.store.compare_and_swap(state.profile, self.snapshot.version, state)


def claim_session(
    store: SessionStore,
    ref: NativeAuthReference,
    harness: str,
    *,
    consumer_id: str | None = None,
    consumer_run_id: str | None = None,
) -> SessionClaim:
    if consumer_id is None and getattr(store, "requires_consumer_id", False):
        raise AuthStateError("remote auth requires an explicit runtime consumer ID")
    owner = consumer_id or uuid.uuid4().hex
    if _OWNER_ID.fullmatch(owner) is None:
        raise AuthStateError("invalid runtime consumer ID")
    current = _bound(store, ref, harness)
    if current.state.phase != "ready":
        raise AuthBusyError(
            "native auth is claimed or logged out; explicit reseed required"
        )
    if consumer_run_id is not None:
        NativeAuthReference(profile=consumer_run_id, binding=ref.binding, generation=1)
    state = replace(
        current.state, phase="claimed", owner=owner, consumer_run_id=consumer_run_id
    )
    return SessionClaim(
        store, store.compare_and_swap(ref.profile, current.version, state)
    )
