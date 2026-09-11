"""Auth CLI handlers and native runtime sessions for the integration owner.

Only auth_login performs interactive login. Inspecting AuthSpec is offline;
auth_status reads the declared authority and native status without refreshing.
NativeRuntime.run owns process termination. Sandbox integrations must use
handoff()/consumer_stopped() and prove the actual remote consumer is stopped
before a credential generation can be made available to the next job.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tetrabench.auth_config import (
    NATIVE_AUTH_PINS,
    AuthSpec,
    EnvAuthReference,
    NativeAuthReference,
    api_key_provider,
    credential_env_name,
    validate_auth_spec,
)
from tetrabench.auth_sessions import (
    AuthError,
    AuthStateError,
    SessionClaim,
    SessionStore,
    check_seed_target,
    claim_session,
    private_directory,
    private_json,
    read_private,
    seed_session,
    write_private,
)
from tetrabench.nativeauth import (
    NativeAuthMetadata,
    NativeResult,
    assert_auth_outside_artifacts,
    inspect_native_store,
    isolated_auth_environment,
    native_auth_command,
    native_auth_path,
    parse_native_status,
    refuse_ambient_conflicts,
    run_native,
    verify_native_version,
)


@dataclass(frozen=True)
class AuthStatus:
    schema_version: Literal[1] = 1
    harness: str = ""
    mode: str = ""
    state: Literal["ready", "absent", "claimed", "logged_out", "setup_required"] = (
        "absent"
    )
    generation: int | None = None
    consumer_id: str | None = None
    native: NativeAuthMetadata | None = None
    # Native status establishes configured auth, never server acceptance/revocation.
    account_verified: bool = False


@dataclass(repr=False)
class NativeRuntime:
    harness: str
    spec: AuthSpec
    root: Path
    executable: str
    environment: dict[str, str] = field(repr=False)
    claim: SessionClaim | None = field(default=None, repr=False)
    model: str | None = None
    _stopped: bool = True
    _ambiguous: bool = False
    _external: bool = False
    _consumer_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def credential_path(self) -> Path:
        return native_auth_path(self.harness, self.root)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60,
        stdin: bytes | None = None,
        interactive: bool = False,
    ) -> NativeResult:
        if not self._consumer_lock.acquire(blocking=False):
            raise AuthError("only one native consumer is allowed per auth session")
        if not self._stopped or self._external:
            self._consumer_lock.release()
            raise AuthError("only one native consumer is allowed per auth session")
        self._stopped = False
        try:
            result = run_native(
                argv,
                environment=self.environment,
                cwd=self.root,
                stdin=stdin,
                interactive=interactive,
                timeout=timeout,
            )
            if result.returncode < 0 or result.returncode in {129, 130, 137, 143}:
                self._ambiguous = True
            return result
        except BaseException:
            # Process death may interrupt refresh after server rotation but before
            # native persistence. Even a surviving old auth.json is not reusable.
            self._ambiguous = True
            raise
        finally:
            self._stopped = True
            self._consumer_lock.release()

    def handoff(self) -> None:
        """Before an external sandbox reads/stages credential_path/environment."""
        if not self._consumer_lock.acquire(blocking=False):
            raise AuthError("native credential session already has a consumer")
        try:
            if not self._stopped or self._external:
                raise AuthError("native credential session already has a consumer")
            self._external = True
            self._stopped = False
        finally:
            self._consumer_lock.release()

    def consumer_stopped(
        self,
        *,
        prove_stopped: Callable[[], bool],
        refreshed_native: bytes | None,
        refresh_outcome_known: bool,
    ) -> None:
        """Runtime owner must collect auth privately and prove child termination.

        This is an integration hook, not an assertion that Modal has been wired.
        A controller-local finally block alone is insufficient remote proof.
        """
        if not self._external or self._stopped or prove_stopped() is not True:
            raise AuthError("external native consumer is not proven stopped")
        if self.claim is not None:
            if refreshed_native is None:
                raise AuthError("native refresh writeback is missing")
            inspect_native_store(self.harness, refreshed_native)
            write_private(self.credential_path, refreshed_native)
        self._stopped = True
        self._ambiguous = not refresh_outcome_known

    def status(self) -> NativeAuthMetadata:
        result = self.run(
            native_auth_command(
                self.harness, "status", executable=self.executable, mode=self.spec.mode
            )
        )
        metadata = parse_native_status(self.harness, result, model=self.model)
        if metadata.mode != self.spec.mode:
            raise AuthError(
                "native auth status does not match the explicit billing mode"
            )
        return metadata

    def refresh(self) -> NativeAuthMetadata:
        """Pi exposes native getAuth; Codex/OpenCode refresh on their next request."""
        result = self.run(
            native_auth_command(
                self.harness, "refresh", executable=self.executable, mode=self.spec.mode
            )
        )
        if result.returncode:
            self._ambiguous = True
            raise AuthError(
                "native credential refresh failed; explicit login may be required"
            )
        return inspect_native_store(self.harness, read_private(self.credential_path))


def _prepare_runtime(
    harness: str,
    spec: AuthSpec,
    root: Path,
    executable: str,
    environment: Mapping[str, str],
    pi_module: Path | None,
    model: str | None = None,
) -> NativeRuntime:
    source = (
        spec.reference.name if isinstance(spec.reference, EnvAuthReference) else None
    )
    refuse_ambient_conflicts(
        harness, spec.mode, environment, source_name=source, model=model
    )
    env = isolated_auth_environment(harness, root, base=environment)
    if isinstance(spec.reference, EnvAuthReference):
        value = environment.get(spec.reference.name)
        if not value or len(value.encode()) > 16 * 1024 or "\x00" in value:
            raise AuthError(
                "declared credential environment reference is missing or invalid"
            )
        env[credential_env_name(harness, spec.mode, model=model)] = value
    runtime = NativeRuntime(harness, spec, root, executable, env, model=model)
    if harness == "pi":
        if pi_module is None:
            entrypoint = shutil.which("pi", path=env["PATH"])
            if entrypoint:
                pi_module = Path(entrypoint).resolve().with_name("index.js")
        if pi_module is None or not pi_module.is_absolute():
            raise AuthError(
                "Pi auth requires the installed coding-agent dist/index.js path"
            )
        # Package metadata is not credential state. This also binds the bridge to
        # Earendil's exported APIs rather than an older pi-mono implementation.
        try:
            package = private_json(
                (pi_module.parent.parent / "package.json").read_bytes()
            )
            if (
                package.get("name") != "@earendil-works/pi-coding-agent"
                or package.get("version") != NATIVE_AUTH_PINS["pi"]
                or pi_module.name != "index.js"
                or pi_module.parent.name != "dist"
            ):
                raise ValueError
        except (OSError, ValueError):
            raise AuthError(
                "Pi auth bridge package does not match the verified pin"
            ) from None
        env["TETRABENCH_PI_MODULE"] = str(pi_module)
        if spec.mode == "api_key":
            env["TETRABENCH_AUTH_PROVIDER"] = api_key_provider(model)[0]
    else:
        version = runtime.run([executable, "--version"])
        if version.returncode:
            raise AuthError("native auth executable version check failed")
        verify_native_version(harness, version.output)
    return runtime


@contextmanager
def _runtime_directory(
    parent: Path, artifact_roots: Sequence[Path], store: SessionStore | None
) -> Iterator[Path]:
    private_directory(parent, create=True)
    assert_auth_outside_artifacts(parent, artifact_roots)
    store_root = getattr(store, "root", None)
    if isinstance(store_root, Path):
        assert_auth_outside_artifacts(store_root, artifact_roots)
    with tempfile.TemporaryDirectory(prefix="native-auth-", dir=parent) as directory:
        yield Path(directory)


@contextmanager
def auth_session(
    harness: str,
    spec: AuthSpec,
    *,
    executable: str,
    runtime_parent: Path,
    environment: Mapping[str, str],
    artifact_roots: Sequence[Path],
    store: SessionStore | None = None,
    pi_module: Path | None = None,
    consumer_id: str | None = None,
    model: str | None = None,
) -> Iterator[NativeRuntime]:
    """Restore, execute, then persist only native auth, including ordinary errors.

    Claims remain blocked on cancellation, unproven child stop, invalid state or
    failed writeback. The finally path does not restore an earlier auth copy.
    API key and setup-token sessions do not acquire the shared OAuth lock.
    """
    validate_auth_spec(harness, spec, model=model)
    with _runtime_directory(runtime_parent, artifact_roots, store) as root:
        runtime = _prepare_runtime(
            harness, spec, root, executable, environment, pi_module, model
        )
        if isinstance(spec.reference, NativeAuthReference):
            if store is None:
                raise AuthError(
                    "native auth requires an explicitly approved private authority"
                )
            runtime.claim = claim_session(
                store, spec.reference, harness, consumer_id=consumer_id
            )
            write_private(runtime.credential_path, runtime.claim.snapshot.state.native)
        elif harness == "codex":
            # Native Codex persists its own API-key format. No hand-built auth blob.
            key = runtime.environment.pop("OPENAI_API_KEY")
            result = runtime.run(
                native_auth_command(
                    harness, "login", executable=executable, mode="api_key"
                ),
                stdin=(key + "\n").encode(),
            )
            if result.returncode:
                raise AuthError("native Codex API-key setup failed")
        try:
            runtime.status()
            yield runtime
        finally:
            if runtime.claim and not runtime.claim.closed:
                if runtime._stopped and not runtime._ambiguous:
                    native = read_private(runtime.credential_path)
                    # Only native state crosses into private authority. Never copy
                    # native session transcripts, runtime directories, or logs.
                    runtime.claim.finish(native, consumer_stopped=True)
                else:
                    if runtime._stopped:
                        runtime.claim.preserve_blocked(
                            read_private(runtime.credential_path)
                        )
                    raise AuthStateError(
                        "native auth ownership is ambiguous; "
                        "profile blocked pending fresh login"
                    ) from None


def auth_login(
    harness: str,
    spec: AuthSpec,
    *,
    executable: str,
    runtime_parent: Path,
    environment: Mapping[str, str],
    artifact_roots: Sequence[Path],
    store: SessionStore | None = None,
    pi_module: Path | None = None,
    device_auth: bool = True,
    previous_generation: int | None = None,
    stopped_owner: Callable[[str], bool] | None = None,
    model: str | None = None,
) -> AuthStatus:
    """Explicit user-facing login. Parent owns confirmation/browser approval."""
    validate_auth_spec(harness, spec, model=model)
    source_name = (
        spec.reference.name if isinstance(spec.reference, EnvAuthReference) else None
    )
    refuse_ambient_conflicts(
        harness, spec.mode, environment, source_name=source_name, model=model
    )
    if spec.mode == "api_key":
        with auth_session(
            harness,
            spec,
            executable=executable,
            runtime_parent=runtime_parent,
            environment=environment,
            artifact_roots=artifact_roots,
            pi_module=pi_module,
            model=model,
        ) as runtime:
            return AuthStatus(
                harness=harness, mode=spec.mode, state="ready", native=runtime.status()
            )
    with _runtime_directory(runtime_parent, artifact_roots, store) as root:
        # setup-token prints to the user's terminal and does not save a token.
        # It intentionally doesn't resolve the yet-to-be-created env reference.
        bootstrap_env = dict(environment)
        if isinstance(spec.reference, EnvAuthReference):
            bootstrap_env.pop(spec.reference.name, None)
            isolated = isolated_auth_environment(harness, root, base=bootstrap_env)
            runtime = NativeRuntime(harness, spec, root, executable, isolated)
            version = runtime.run([executable, "--version"])
            verify_native_version(harness, version.output)
        else:
            if store is None:
                raise AuthError(
                    "native login requires an explicitly approved private authority"
                )
            check_seed_target(
                store,
                spec.reference,
                harness,
                previous_generation=previous_generation,
                stopped_owner=stopped_owner,
            )
            runtime = _prepare_runtime(
                harness, spec, root, executable, bootstrap_env, pi_module, model
            )
        result = runtime.run(
            native_auth_command(
                harness,
                "login",
                executable=executable,
                mode=spec.mode,
                device_auth=device_auth,
            ),
            interactive=True,
            timeout=900,
        )
        if result.returncode:
            raise AuthError("native login failed; no credentials published")
        if isinstance(spec.reference, EnvAuthReference):
            return AuthStatus(harness=harness, mode=spec.mode, state="setup_required")
        if store is None:
            raise AuthError("native auth authority is unavailable")
        native = read_private(runtime.credential_path)
        metadata = inspect_native_store(harness, native)
        runtime.status()
        seed_session(
            store,
            spec.reference,
            harness,
            native,
            previous_generation=previous_generation,
            stopped_owner=stopped_owner,
        )
        return AuthStatus(
            harness=harness,
            mode=spec.mode,
            state="ready",
            generation=spec.reference.generation,
            native=metadata,
        )


def auth_status(
    harness: str,
    spec: AuthSpec,
    *,
    store: SessionStore | None = None,
    model: str | None = None,
    **runtime_options,
) -> AuthStatus:
    """Explicit status may read the declared private source; never starts login."""
    validate_auth_spec(harness, spec, model=model)
    if isinstance(spec.reference, NativeAuthReference):
        from tetrabench.auth_sessions import _bound

        if store is None:
            raise AuthError("native auth requires a private authority")
        current = store.read(spec.reference.profile)
        if current is None:
            return AuthStatus(harness=harness, mode=spec.mode, state="absent")
        current = _bound(store, spec.reference, harness)
        return AuthStatus(
            harness=harness,
            mode=spec.mode,
            state=current.state.phase,
            generation=current.state.generation,
            consumer_id=current.state.owner,
            native=(
                inspect_native_store(harness, current.state.native)
                if current.state.phase == "ready"
                else None
            ),
        )
    with auth_session(harness, spec, model=model, **runtime_options) as runtime:
        return AuthStatus(
            harness=harness, mode=spec.mode, state="ready", native=runtime.status()
        )


@contextmanager
def _cli_logout_operation(
    parent: Path, artifact_roots: Sequence[Path]
) -> Iterator[str]:
    from tetrabench.auth_operations import cli_logout_lifetime

    assert_auth_outside_artifacts(parent, artifact_roots)
    with cli_logout_lifetime(parent) as owner:
        yield owner


def auth_logout(
    harness: str,
    spec: AuthSpec,
    *,
    store: SessionStore | None = None,
    **runtime_options,
) -> AuthStatus:
    """Remove only the declared eval login. This does not revoke provider tokens."""
    validate_auth_spec(harness, spec)
    if not isinstance(spec.reference, NativeAuthReference):
        # Never delete a user's secret-manager item or global native keychain.
        return AuthStatus(harness=harness, mode=spec.mode, state="setup_required")
    current = auth_status(harness, spec, store=store)
    if current.state == "logged_out":
        return current
    parent = runtime_options.get("runtime_parent")
    if not isinstance(parent, Path):
        raise AuthError("native logout requires a private runtime directory")
    with _cli_logout_operation(
        parent, runtime_options.get("artifact_roots", ())
    ) as owner:
        runtime_options.pop("consumer_id", None)
        with auth_session(
            harness, spec, store=store, consumer_id=owner, **runtime_options
        ) as runtime:
            result = runtime.run(
                native_auth_command(harness, "logout", executable=runtime.executable)
            )
            if result.returncode or runtime._ambiguous or not runtime._stopped:
                runtime._ambiguous = True
                raise AuthError("native logout failed; profile remains blocked")
            if runtime.claim is None:
                raise AuthError("native logout has no credential authority claim")
            runtime.claim.logout(consumer_stopped=True)
    return AuthStatus(
        harness=harness,
        mode=spec.mode,
        state="logged_out",
        generation=spec.reference.generation,
    )


def auth_reseed(
    harness: str,
    spec: AuthSpec,
    *,
    stopped_owner: Callable[[str], bool] | None = None,
    **login_options,
) -> AuthStatus:
    """Explicit fresh native login into the next declared generation."""
    if (
        not isinstance(spec.reference, NativeAuthReference)
        or spec.reference.generation < 2
    ):
        raise AuthError(
            "reseed requires a native_session reference with next generation"
        )
    return auth_login(
        harness,
        spec,
        previous_generation=spec.reference.generation - 1,
        stopped_owner=stopped_owner,
        **login_options,
    )
