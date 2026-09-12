"""Private one-off renewal proof instrumentation; no standalone live command.

Use RenewalProof inside cli_operation_lifetime/auth_session. In the NEW actual
controller, construct SuccessorProof from the verified released revision and
wrap its ordinary runner with observe(). Both objects contain private bytes:
never serialize them, closures, or exception locals. Only report() is publishable.
No retry, model invocation, backend discovery, or credentials are built in here.
"""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tetrabench.auth import NativeRuntime
from tetrabench.auth_config import NativeAuthReference
from tetrabench.auth_sessions import (
    AuthError,
    SessionStore,
    private_json,
    read_private,
    write_private,
)
from tetrabench.native_refresh import require_renewed
from tetrabench.runtime_auth import RuntimeAuthHook


def _ready(store: SessionStore, ref: NativeAuthReference, harness: str):
    current = store.read(ref.profile)
    if current is None or (
        current.state.phase != "ready"
        or current.state.owner is not None
        or current.state.harness != harness
        or current.state.binding != ref.binding
        or current.state.generation != ref.generation
    ):
        raise AuthError("renewal proof requires the exact ready authority")
    return current


class RenewalProof:
    def __init__(self, runtime: NativeRuntime):
        if runtime.claim is None or runtime.claim.closed:
            raise AuthError("renewal proof requires an active native claim")
        self._runtime = runtime
        self._before = read_private(runtime.credential_path)
        self._after: bytes | None = None
        self._released = False
        self._attempted = False
        self.released_revision: int | None = None

    def expire_unsigned_cache(self) -> None:
        runtime = self._runtime
        if runtime.harness not in {"opencode", "pi"}:
            raise AuthError("only Pi/OpenCode have an approved unsigned expiry trigger")
        if not runtime._consumer_lock.acquire(blocking=False):
            raise AuthError("renewal proof already has a consumer")
        try:
            if not runtime._stopped or runtime._external or runtime._ambiguous:
                raise AuthError("renewal proof requires stopped unambiguous custody")
            if runtime.claim is None or runtime.claim.closed:
                raise AuthError("renewal proof claim is closed")
            runtime._ambiguous = True
            if read_private(runtime.credential_path) != self._before:
                raise AuthError("native state changed before expiry injection")
            value = private_json(self._before)
            provider = "openai" if runtime.harness == "opencode" else "openai-codex"
            value[provider]["expires"] = 1
            injected = json.dumps(value).encode()
            write_private(runtime.credential_path, injected)
            runtime.claim.checkpoint(injected)
            # No consumer ran during injection. Only acknowledged native bytes
            # are now staged; failures leave custody poisoned without rollback.
            runtime._ambiguous = False
        finally:
            runtime._consumer_lock.release()

    def renew(self, *, timeout: float = 60, prompt: str | None = None) -> None:
        runtime = self._runtime
        if self._attempted:
            raise AuthError("renewal proof is single-use; never retry a renewal")
        self._attempted = True
        # refresh owns the launched/unlaunched distinction. A credential-free
        # containment preflight failure must not invent an ambiguous renewal.
        runtime.refresh(timeout=timeout, prompt=prompt)
        try:
            after = read_private(runtime.credential_path)
            require_renewed(runtime.harness, self._before, after)
            self._after = after
        except BaseException:
            runtime._ambiguous = True
            raise

    def verify_release(self) -> int:
        """Read-only reconciliation, never replay a failed finish/checkpoint CAS."""
        runtime = self._runtime
        claim = runtime.claim
        ref = runtime.spec.reference
        if (
            claim is None
            or not claim.closed
            or not isinstance(ref, NativeAuthReference)
        ):
            raise AuthError("renewal proof must observe auth_session exit first")
        current = _ready(claim.store, ref, runtime.harness)
        if (
            self._after is None
            or current.state.native != self._after
            or current.state.revision != claim.snapshot.state.revision + 1
        ):
            raise AuthError(
                "renewal release identity is unproven; do not start a consumer"
            )
        self._released = True
        self.released_revision = current.state.revision
        return current.state.revision

    def report(self) -> dict[str, bool]:
        changed = False
        if self._after is not None:
            provider = {"codex": "tokens", "pi": "openai-codex", "opencode": "openai"}[
                self._runtime.harness
            ]
            field = "refresh_token" if self._runtime.harness == "codex" else "refresh"
            changed = (
                private_json(self._before)[provider][field]
                != private_json(self._after)[provider][field]
            )
        return {
            "renewed": self._after is not None,
            "refresh_token_changed": changed,
            "release_verified": self._released,
        }


class SuccessorProof:
    """Install only inside the fresh controller, around its real production runner.

    Reconcile the exact release revision first, then compare the actual claim and
    a private download of the first staged native file before any native request.
    The download uses existing transport/private files and is immediately removed;
    the comparison baseline stays in memory. No token/hash goes in a run request.
    """

    def __init__(
        self,
        store: SessionStore,
        ref: NativeAuthReference,
        harness: str,
        *,
        released_revision: int,
        consumer_id: str,
    ):
        self._baseline = _ready(store, ref, harness).state
        if self._baseline.revision != released_revision:
            raise AuthError("successor release revision changed; stop and reconcile")
        self._consumer_id = consumer_id
        self._observed = False

    @contextmanager
    def observe(self):
        original = RuntimeAuthHook._restore_credentials

        async def restore(hook: RuntimeAuthHook) -> None:
            if self._observed:
                await original(hook)
                return
            claim = hook.scope.claim
            expected = self._baseline
            try:
                if (
                    claim is None
                    or claim.closed
                    or claim.snapshot.state.phase != "claimed"
                    or claim.snapshot.state.owner != self._consumer_id
                    or claim.snapshot.state.profile != expected.profile
                    or claim.snapshot.state.harness != expected.harness
                    or claim.snapshot.state.binding != expected.binding
                    or claim.snapshot.state.generation != expected.generation
                    or claim.snapshot.state.revision != expected.revision
                    or claim.snapshot.state.native != expected.native
                    or hook.scope.native != expected.native
                ):
                    raise AuthError("successor did not claim the verified renewal")
                await original(hook)
                with tempfile.TemporaryDirectory(dir=hook.scope.directory) as directory:
                    local = Path(directory) / "native-readback"
                    await hook.environment.download_file(hook.native_path, local)
                    if local.is_symlink():
                        raise AuthError(
                            "successor readback is not a private regular file"
                        )
                    local.chmod(0o600)
                    if read_private(local) != expected.native:
                        raise AuthError("successor staged native bytes differ")
                self._observed = True
            except BaseException:
                hook.scope.poisoned = True
                raise

        with patch.object(RuntimeAuthHook, "_restore_credentials", restore):
            yield self

    def report(self) -> dict[str, bool]:
        return {"claimed_and_staged_identity_verified": self._observed}
