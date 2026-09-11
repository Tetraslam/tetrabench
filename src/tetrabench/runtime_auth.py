"""Harbor 0.22 native-auth handoff and physical consumer-stop boundary.

Working contract: one run-scoped claim, at most one subscription consumer at a
time, native-format transfer outside all run artifacts. Native model exec return
is the writeback capture point (before Codex.run's finally deletes CODEX_HOME).
Docker daemon/container identity or the retained Modal Sandbox handle proves
physical stop. Native errors preserve state; cancellation/missing capture/stop
or ambiguous CAS never releases a refresh lineage. No callbacks asserting True
stand in for provider observation. API-key jobs keep normal trial concurrency.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from tetrabench.auth_config import (
    EnvAuthReference,
    NativeAuthReference,
    api_key_provider,
    authentication_environment_names,
    credential_env_name,
    validate_auth_spec,
)
from tetrabench.auth_profiles import (
    load_auth_config_file,
    load_auth_profile,
    profile_store,
)
from tetrabench.auth_retention import KnownAuthRetention
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    AuthStateError,
    SessionClaim,
    SessionStore,
    claim_session,
    private_directory,
    private_json,
    read_private,
    write_private,
)
from tetrabench.controller_runtime import AttemptPaths
from tetrabench.harness_config import ResolvedHarness
from tetrabench.native_execution import NATIVE_SHELL_PREFIX, native_shell_command
from tetrabench.nativeauth import (
    NativeAuthMetadata,
    NativeResult,
    assert_auth_outside_artifacts,
    inspect_native_store,
    native_auth_command,
    parse_native_status,
    refuse_ambient_conflicts,
)

_current: ContextVar[RuntimeAuthScope | None] = ContextVar(
    "native_auth_scope", default=None
)
_NATIVE_PREFIX = NATIVE_SHELL_PREFIX


@dataclass(frozen=True)
class NativeDispatch:
    """Single-use launcher witness identity; contains no credential material."""

    nonce: str
    producer_status_path: str


@dataclass
class _DispatchState:
    dispatch: NativeDispatch
    started: bool = False
    consumed: bool = False
    producer_exit_code: int | None = None
    pipeline_exit_code: int | None = None
    captured: bool = False
    finished: bool = False


class ConsumerProof(Protocol):
    async def stopped(self) -> bool: ...


class DockerConsumerProof:
    def __init__(self, daemon: str, container: str):
        self.daemon, self.container = daemon, container

    async def stopped(self) -> bool:
        from tetrabench.docker_lifecycle import _docker

        if (
            await asyncio.to_thread(_docker, "info", "--format", "{{.ID}}")
            != self.daemon
        ):
            return False
        output = await asyncio.to_thread(
            _docker,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={self.container}",
            "--format",
            "{{.ID}} {{.State}}",
        )
        return output == "" or output in {
            f"{self.container} exited",
            f"{self.container} dead",
        }


class ModalConsumerProof:
    def __init__(self, sandbox: Any):
        self.sandbox = sandbox

    async def stopped(self) -> bool:
        import modal

        try:
            result = await self.sandbox.poll.aio()
        except modal.exception.NotFoundError:
            return True
        return type(result) is int


async def capture_consumer(environment: Any, engine: str) -> ConsumerProof:
    """Pin physical identity before any credential delivery. No name-only proof."""
    from harbor.environments.docker.docker import DockerEnvironment

    from tetrabench.harbor import TetrabenchModalEnvironment

    if engine == "docker" and isinstance(environment, DockerEnvironment):
        from tetrabench.docker_lifecycle import _docker

        result = await environment._run_docker_compose_command(
            ["ps", "--quiet", "main"],
            timeout_sec=15,
        )
        container = (result.stdout or "").strip()
        if result.return_code or re.fullmatch(r"[0-9a-f]{64}", container) is None:
            raise AuthError(
                "cannot bind native auth to one running Docker main container"
            )
        daemon = await asyncio.to_thread(_docker, "info", "--format", "{{.ID}}")
        return DockerConsumerProof(daemon, container)
    if engine == "modal" and isinstance(environment, TetrabenchModalEnvironment):
        sandbox = await environment._lookup()
        native_sandbox = environment._sandbox
        if native_sandbox is None or native_sandbox.object_id != sandbox.object_id:
            raise AuthError("Modal native consumer and named sandbox identity disagree")
        if await sandbox.poll.aio() is not None:
            raise AuthError("native auth requires a running bound Modal sandbox")
        return ModalConsumerProof(sandbox)
    raise AuthError(
        "native auth physical lifecycle is unsupported for this environment"
    )


class RuntimeAuthScope:
    def __init__(
        self,
        harness: ResolvedHarness,
        paths: AttemptPaths,
        *,
        engine: Literal["docker", "modal"],
        consumer_id: str,
        run_id: str | None = None,
        directory: Path,
        environment: Mapping[str, str],
        store: SessionStore | None = None,
        proof_factory: Callable[..., Any] = capture_consumer,
    ):
        self.harness, self.paths, self.engine = harness, paths, engine
        self.directory = private_directory(directory)
        assert_auth_outside_artifacts(directory, [paths.root])
        store_root = getattr(store, "root", None)
        if isinstance(store_root, Path):
            assert_auth_outside_artifacts(store_root, [paths.root])
        self.claim: SessionClaim | None = None
        self.native: bytes | None = None
        self.key: str | None = None
        self.hooks: list[RuntimeAuthHook] = []
        self.poisoned = False
        self.retention = KnownAuthRetention()
        self.proof_factory = proof_factory
        spec = harness.auth
        if spec is None:
            raise AuthError("runtime auth scope requires an explicit AuthSpec")
        validate_auth_spec(
            harness.name,
            spec,
            version=harness.version,
            model=harness.model,
            env=harness.env,
        )
        from tetrabench.harnesses import validate_explicit_auth_configuration

        validate_explicit_auth_configuration(harness)
        source = (
            spec.reference.name
            if isinstance(spec.reference, EnvAuthReference)
            else None
        )
        # Infrastructure auth configuration is not a model credential selector.
        native_env = {
            k: v for k, v in environment.items() if not k.startswith("TETRABENCH_AUTH_")
        }
        refuse_ambient_conflicts(
            harness.name, spec.mode, native_env, source_name=source, model=harness.model
        )
        if isinstance(spec.reference, NativeAuthReference):
            if store is None:
                raise AuthError(
                    "native OAuth needs an explicitly approved private auth backend"
                )
            self.claim = claim_session(
                store,
                spec.reference,
                harness.name,
                consumer_id=consumer_id,
                consumer_run_id=run_id,
            )
            self.native = self.claim.snapshot.state.native
            self.retention.native(harness.name, self.native)
        else:
            self.key = environment.get(spec.reference.name)
            if (
                not self.key
                or len(self.key.encode()) > 16 * 1024
                or any(c in self.key for c in "\r\n\x00")
            ):
                raise AuthError(
                    "declared model credential reference is missing or invalid"
                )
            self.retention.add(self.key)

    def __repr__(self) -> str:
        return "<RuntimeAuthScope private>"

    def new_hook(self, harness: ResolvedHarness) -> RuntimeAuthHook:
        if harness != self.harness:
            raise AuthError("native agent does not match its run-scoped auth reference")
        hook = RuntimeAuthHook(self)
        self.hooks.append(hook)
        return hook

    def finalize(self, *, require_consumer: bool = False) -> None:
        failed = False
        try:
            self._finish_claim(require_consumer=require_consumer)
        except BaseException:
            failed = True
            raise
        finally:
            root = (
                self.paths.jobs
                if self.paths.jobs != self.paths.root
                else self.paths.root / "harbor-job"
            )
            uncertain = any(
                hook.bound and not hook.physically_stopped for hook in self.hooks
            )
            uncertain |= self.claim is not None and (
                failed
                or self.poisoned
                or any(hook.refresh_uncertain for hook in self.hooks)
            )
            self.retention.enforce(
                root,
                attempt_root=self.paths.root,
                private_parent=self.directory.parent,
                refresh_unknown=uncertain,
            )

    def _finish_claim(self, *, require_consumer: bool = False) -> None:
        if require_consumer and not any(hook.has_executed for hook in self.hooks):
            if self.claim is not None and not any(hook.bound for hook in self.hooks):
                if self.native is None:
                    raise AuthError("claimed native state is unavailable")
                self.claim.finish(self.native, consumer_stopped=True)
            raise AuthError(
                "native auth model-execution hook did not run; execution is unsupported"
            )
        if self.claim is None:
            return
        if self.claim.closed:
            raise AuthStateError(
                "native auth checkpoint outcome is ambiguous; profile blocked"
            )
        used = [hook for hook in self.hooks if hook.bound]
        safe = not self.poisoned and all(
            hook.physically_stopped and hook.processes_settled for hook in used
        )
        if not used:
            # No transfer occurred, so no client could have refreshed these bytes.
            safe = not self.poisoned
        if safe and self.native is not None:
            self.claim.finish(self.native, consumer_stopped=True)
            return
        if self.native is not None:
            self.claim.preserve_blocked(self.native)
        raise AuthStateError(
            "native auth writeback or physical stop is unproven; "
            "profile blocked, reseed required"
        )


class RuntimeAuthHook:
    """One agent's hook. Never serialize this object or its environment values."""

    def __init__(self, scope: RuntimeAuthScope):
        self.scope = scope
        # Fresh mkdir(0700), without -p, refuses an existing path or symlink.
        self.root = f"/tmp/tetrabench-auth-{uuid.uuid4().hex}"  # nosec B108
        self.bound = False
        self.binding = False
        self.execution_returned = False
        self.producer_exit_code: int | None = None
        self.refresh_uncertain = False
        self.captured = False
        self.physically_stopped = False
        self.executing = False
        self.environment: Any = None
        self.proof: ConsumerProof | None = None
        self._source = scope.directory / f"native-{uuid.uuid4().hex}.json"
        self._dispatches: list[_DispatchState] = []
        self._pending: _DispatchState | None = None
        self._credentials_staged = False
        self._observed_auth: NativeAuthMetadata | None = None
        self._observation_count = 0
        self._provenance_writer: Callable[[], None] | None = None
        self._metadata_count = 0
        self._metadata_settled = 0

    def __repr__(self) -> str:
        return "<RuntimeAuthHook private>"

    @property
    def producer_status_path(self) -> str:
        """Read the active witness without creating a new dispatch implicitly."""
        if self._pending is None:
            raise AuthError("reserve a native dispatch before requesting its witness")
        return self._pending.dispatch.producer_status_path

    def new_dispatch(self, *, producer_nonce: str | None = None) -> NativeDispatch:
        if self.executing or self.binding or self._pending is not None:
            raise AuthBusyError("native auth already has a reserved or active dispatch")
        if self.physically_stopped or self.scope.poisoned:
            raise AuthError("native auth consumer is stopped or its lineage is blocked")
        nonce = uuid.uuid4().hex if producer_nonce is None else producer_nonce
        if re.fullmatch(r"[0-9a-f]{32}", nonce) is None or any(
            item.dispatch.nonce == nonce for item in self._dispatches
        ):
            raise AuthError("native producer nonce is invalid or already used")
        dispatch = NativeDispatch(nonce, self.root + f"/producer-status-{nonce}.json")
        self._pending = _DispatchState(dispatch)
        self._dispatches.append(self._pending)
        return dispatch

    async def prepare_step(self, environment: Any, *, producer_nonce: str) -> str:
        """Native run() entry hook, before configuration/auth-file setup."""
        await self.bind(environment)
        return self.new_dispatch(producer_nonce=producer_nonce).producer_status_path

    @property
    def has_executed(self) -> bool:
        return self._metadata_count > 0 or any(
            item.started for item in self._dispatches
        )

    @property
    def processes_settled(self) -> bool:
        return (
            (bool(self._dispatches) or self._metadata_count > 0)
            and self._pending is None
            and not self.executing
            and self._metadata_count == self._metadata_settled
            and all(
                item.finished and item.captured and item.producer_exit_code == 0
                for item in self._dispatches
            )
        )

    @property
    def codex_source_path(self) -> Path | None:
        return self._source if self.scope.native is not None else None

    @property
    def native_path(self) -> str:
        suffix = {
            "codex": "codex/auth.json",
            "opencode": "data/opencode/auth.json",
            "pi": "pi/auth.json",
        }
        return self.root + "/" + suffix[self.scope.harness.name]

    @property
    def agent_environment(self) -> dict[str, str]:
        spec = self.scope.harness.auth
        if spec is None:
            raise AuthError("native auth specification is unavailable")
        result: dict[str, str] = {}
        if self.scope.key is not None:
            result[
                credential_env_name(
                    self.scope.harness.name, spec.mode, model=self.scope.harness.model
                )
            ] = self.scope.key
        return result

    def configure_agent(self, agent: Any) -> None:
        """Main adapter calls once, after construction; never edits the sealed spec."""
        agent._extra_env.update(self.agent_environment)
        writer = getattr(agent, "_write_provenance", None)
        logs_dir = getattr(agent, "logs_dir", None)
        if callable(writer) or isinstance(logs_dir, Path):

            def publish_observation() -> None:
                from tetrabench.canonical_json import dumps_canonical_json
                from tetrabench.run_reference import write_private_record

                if isinstance(logs_dir, Path):
                    logs_dir.mkdir(parents=True, exist_ok=True)
                    write_private_record(
                        logs_dir / "tetrabench-auth.json",
                        dumps_canonical_json(self.observed_auth_provenance()),
                    )
                if callable(writer):
                    writer(
                        "matched"
                        if getattr(agent, "_observed_version", None)
                        else "unverified"
                    )

            self._provenance_writer = publish_observation
        if self.scope.harness.name == "codex":
            agent._REMOTE_CODEX_HOME = PurePosixPath(self.root + "/codex")
            agent._REMOTE_CODEX_SECRETS_DIR = PurePosixPath(self.root + "/secrets")
            agent._base_config["cli_auth_credentials_store"] = "file"

    def observed_auth_provenance(self) -> dict[str, Any]:
        """Safe native-status evidence, never a claim of provider account access."""
        spec = self.scope.harness.auth
        observed = self._observed_auth
        return {
            "schema_version": 1,
            "requested_mode": spec.mode if spec is not None else None,
            "observed": asdict(observed) if observed is not None else None,
            "observations": self._observation_count,
            "account_verified": False,
            "model_activity": "not_assessed",
        }

    async def _exec(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ):
        try:
            result = await self.environment.exec(
                command=command, env=env, user=user, timeout_sec=30
            )
        except Exception:
            raise AuthError("private native auth sandbox operation failed") from None
        if result.return_code:
            raise AuthError("private native auth sandbox operation failed")
        return result

    async def _io(self, action: str, *args: str) -> None:
        await self._exec(
            shlex.join(["python3", self.root + "/auth-io.py", action, self.root, *args])
        )

    async def _upload(self, local: Path, remote: str) -> None:
        await self.environment.upload_file(local, remote)
        owner = getattr(self.environment, "default_user", None)
        command = "chmod 600 " + shlex.quote(remote)
        if owner is not None:
            command += (
                " && chown " + shlex.quote(str(owner)) + " " + shlex.quote(remote)
            )
        await self._exec(command, user="root")

    async def bind(self, environment: Any) -> None:
        from tetrabench.harnesses import validate_explicit_auth_configuration

        validate_explicit_auth_configuration(self.scope.harness)
        if self.binding:
            raise AuthBusyError("native auth binding is already in progress")
        if self.bound:
            if environment is not self.environment:
                raise AuthError("native auth cannot move between environments")
            if self.physically_stopped or self.scope.poisoned:
                raise AuthError(
                    "native auth consumer is stopped or its lineage is blocked"
                )
            if not self.executing and not self._credentials_staged:
                self.binding = True
                try:
                    await self._restore_credentials()
                except BaseException:
                    self.scope.poisoned = True
                    raise AuthError(
                        "native credential restaging failed; profile blocked"
                    ) from None
                finally:
                    self.binding = False
            return
        if self.scope.poisoned or (
            self.scope.claim
            and any(
                hook.bound and not hook.physically_stopped
                for hook in self.scope.hooks
                if hook is not self
            )
        ):
            raise AuthBusyError(
                "only one native subscription consumer may run at a time"
            )
        self.environment = environment
        # Reserve before the first provider await, not after an async lookup.
        self.bound = True
        self.binding = True
        try:
            self.proof = await self.scope.proof_factory(environment, self.scope.engine)
        except BaseException:
            self.scope.poisoned = True
            self.binding = False
            raise AuthError("native auth physical consumer binding failed") from None
        original_stop = environment.stop

        async def stop(delete: bool) -> None:
            try:
                await original_stop(delete=delete)
                if self.proof is None:
                    raise AuthError("native consumer stop proof is unavailable")
                self.physically_stopped = await self.proof.stopped()
                if self.physically_stopped and self._provenance_writer is not None:
                    self._provenance_writer()
            except BaseException:
                self.scope.poisoned = True
                raise

        environment.stop = stop
        try:
            await self._exec("python3 --version")
            await self._exec("umask 077; mkdir -m 700 " + shlex.quote(self.root))
            directories = [
                self.root + "/" + suffix
                for suffix in (
                    "codex",
                    "secrets",
                    "pi",
                    "claude",
                    "config",
                    "data",
                    "data/opencode",
                    "state",
                )
            ]
            await self._exec("umask 077; mkdir -p " + shlex.join(directories))
            await self._upload(
                Path(__file__).with_name("nativeauth_io.py"), self.root + "/auth-io.py"
            )
            await self._restore_credentials()
        except BaseException:
            self.scope.poisoned = True
            raise AuthError(
                "private native auth binding failed; profile blocked"
            ) from None
        finally:
            self.binding = False

    async def _restore_credentials(self) -> None:
        # Codex removes these native directories in its run() finally. Recreate
        # only our private paths; never read a deleted or interactive auth copy.
        await self._exec(
            "umask 077; mkdir -p "
            + shlex.join(
                [
                    self.root + "/codex",
                    self.root + "/secrets",
                    self.root + "/pi",
                    self.root + "/data/opencode",
                ]
            )
        )
        if self.scope.native is not None:
            write_private(self._source, self.scope.native)
            await self._upload(self._source, self.native_path)
        self._credentials_staged = True

    def execution_environment(
        self, incoming: Mapping[str, str] | None
    ) -> dict[str, str]:
        env = dict(incoming or {})
        name = self.scope.harness.name
        controlled = {
            "XDG_CONFIG_HOME": self.root + "/config",
            "CODEX_HOME": self.root + "/codex",
            "CLAUDE_CONFIG_DIR": self.root + "/claude",
            "PI_CODING_AGENT_DIR": self.root + "/pi",
        }
        allowed = self.agent_environment
        if name == "opencode" and any(
            item.destination.startswith("opencode/")
            for item in self.scope.harness.resources
        ):
            from tetrabench.resources import AGENT_RESOURCE_ROOT

            controlled["OPENCODE_CONFIG_DIR"] = AGENT_RESOURCE_ROOT + "/opencode"
        for key in authentication_environment_names():
            if (
                env.get(key)
                and key not in controlled
                and env.get(key) != allowed.get(key)
            ):
                raise AuthError(
                    "native model invocation contains competing auth "
                    "or billing selectors"
                )
            if key not in controlled:
                env.pop(key, None)
        env.update(allowed)
        if name != "opencode":
            env["XDG_CONFIG_HOME"] = controlled["XDG_CONFIG_HOME"]
        env[
            {
                "codex": "CODEX_HOME",
                "claude-code": "CLAUDE_CONFIG_DIR",
                "pi": "PI_CODING_AGENT_DIR",
                "opencode": "XDG_DATA_HOME",
            }[name]
        ] = (
            self.root + "/data"
            if name == "opencode"
            else controlled[
                {
                    "codex": "CODEX_HOME",
                    "claude-code": "CLAUDE_CONFIG_DIR",
                    "pi": "PI_CODING_AGENT_DIR",
                }[name]
            ]
        )
        if name == "opencode":
            env["XDG_STATE_HOME"] = self.root + "/state"
            env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
        return env

    async def _status(self, env: dict[str, str]) -> None:
        # The Pi guard belongs to the model producer, not npm or our metadata-only
        # auth helper. Keep the actual producer's environment unchanged.
        env = {
            key: value
            for key, value in env.items()
            if key
            not in {
                "NODE_OPTIONS",
                "TETRABENCH_PI_CAPABILITY_PROBE",
            }
        }
        name, spec = self.scope.harness.name, self.scope.harness.auth
        if spec is None:
            raise AuthError("native auth specification is unavailable")
        if name == "pi":
            root = await self._exec(_NATIVE_PREFIX + "npm root -g", env=env)
            module_root = (root.stdout or "").strip()
            if (
                not module_root.startswith("/")
                or "\n" in module_root
                or len(module_root) > 1024
            ):
                raise AuthError("cannot resolve pinned Pi native module")
            env = {
                **env,
                "PI_OFFLINE": "1",
                "TETRABENCH_PI_MODULE": module_root
                + "/@earendil-works/pi-coding-agent/dist/index.js",
            }
            if spec.mode == "api_key":
                env["TETRABENCH_AUTH_PROVIDER"] = api_key_provider(
                    self.scope.harness.model
                )[0]
        executable = {
            "codex": "codex",
            "claude-code": "claude",
            "opencode": "opencode",
            "pi": "node",
        }[name]
        command = native_auth_command(
            name, "status", executable=executable, mode=spec.mode
        )
        result = await self._exec(_NATIVE_PREFIX + shlex.join(command), env=env)
        metadata = parse_native_status(
            name,
            NativeResult(result.return_code, (result.stdout or "").encode()),
            model=self.scope.harness.model,
        )
        # Codex writes login status to stderr.
        if metadata.mode in {"unknown", "none"} and name == "codex":
            metadata = parse_native_status(
                name, NativeResult(result.return_code, (result.stderr or "").encode())
            )
        self._observed_auth = metadata
        self._observation_count += 1
        if self._provenance_writer is not None:
            self._provenance_writer()
        if metadata.mode != spec.mode:
            raise AuthError("native status refused the requested billing mode")

    async def _capture_native(self, *, remove: bool) -> None:
        if self.scope.native is None:
            return
        remote = self.root + "/snapshot-" + uuid.uuid4().hex
        await self._io("snapshot", self.native_path, remote)
        local = self.scope.directory / ("writeback-" + uuid.uuid4().hex)
        await self.environment.download_file(remote, local)
        if local.is_symlink():
            raise AuthError("native auth download produced a symlink")
        local.chmod(0o600)
        native = read_private(local)
        self.scope.retention.native(self.scope.harness.name, native)
        inspect_native_store(self.scope.harness.name, native)
        self.scope.native = native
        write_private(self._source, native)
        if self.scope.claim is None:
            raise AuthError("native writeback has no authority claim")
        self.scope.claim.checkpoint(native)
        await self._io("remove", remote)
        if remove:
            await self._io("remove", self.native_path)
            self._credentials_staged = False

    async def execute_metadata(
        self,
        environment: Any,
        command: str,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int = 40,
    ) -> Any:
        """Run only the pinned no-prompt startup helper inside this same lineage.

        A reserved model dispatch stays reserved. Metadata cannot run alongside
        any producer or writeback, and refresh failure poisons the same claim.
        """
        prefix = (
            "if [ -f ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
            'export PATH="$HOME/.local/bin:$PATH"; '
        )
        if not command.startswith(prefix):
            raise AuthError(
                "metadata execution requires the reviewed native startup helper"
            )
        args = shlex.split(command[len(prefix) :])
        if (
            len(args) != 3
            or args[0] != "python3"
            or re.fullmatch(
                r"/tmp/tetrabench-capability-[0-9a-f]{32}/"  # nosec B108
                r"runtime_metadata\.py",
                args[1],
            )
            is None
            or args[2] != str(PurePosixPath(args[1]).with_name("startup-probe.json"))
        ):
            raise AuthError(
                "metadata execution cannot run arbitrary commands or prompts"
            )
        if (
            self.executing
            or self.binding
            or self.scope.poisoned
            or self.physically_stopped
            or (self._pending is not None and self._pending.consumed)
        ):
            raise AuthBusyError(
                "native metadata consumer conflicts with active auth use"
            )
        self.executing = True
        self._metadata_count += 1
        completed = False
        result = None
        try:
            await self.bind(environment)
            if not self._credentials_staged:
                await self._restore_credentials()
            for filename in ("runtime_metadata.py", "native_control.py"):
                digest = hashlib.sha256(
                    Path(__file__).with_name(filename).read_bytes()
                ).hexdigest()
                await self._io(
                    "verify-helper",
                    str(PurePosixPath(args[1]).with_name(filename)),
                    digest,
                )
            effective = self.execution_environment(env)
            effective.pop("NODE_OPTIONS", None)
            effective.pop("TETRABENCH_PI_CAPABILITY_PROBE", None)
            from tetrabench.native_control import CUSTODY_ENV

            # This owner, not request JSON or inherited environment, decides
            # whether native metadata is consuming a refreshable credential.
            effective[CUSTODY_ENV] = (
                "required" if self.scope.claim is not None else "none"
            )
            if self.scope.harness.name == "codex":
                await self._io(
                    "codex-regular" if self.scope.native is not None else "codex-api"
                )
            elif self.scope.harness.name == "pi":
                await self._io("pi-config")
            await self._status(effective)
            result = await environment.exec(
                command=native_shell_command(command),
                env=effective,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
            if result.return_code != 0:
                raise AuthError(
                    "native metadata completion is uncertain; lineage blocked"
                )
            if (
                len((result.stdout or "").encode())
                + len((result.stderr or "").encode())
                > 128 * 1024
            ):
                raise AuthError("native metadata output exceeded its bound")
            report = private_json((result.stdout or "").encode())
            if report.get("inference_validated") is not False:
                raise AuthError("native query did not return metadata-only evidence")
            if self.scope.claim is not None:
                custody = report.get("auth_custody")
                if (
                    not isinstance(custody, dict)
                    or set(custody)
                    != {
                        "schema_version",
                        "credential_processes",
                        "graceful_credential_processes",
                    }
                    or type(custody.get("schema_version")) is not int
                    or custody.get("schema_version") != 1
                    or type(custody.get("credential_processes")) is not int
                    or type(custody.get("graceful_credential_processes")) is not int
                    or not 0 <= custody["credential_processes"] <= 64
                    or custody["credential_processes"]
                    != custody["graceful_credential_processes"]
                ):
                    raise AuthError("native credential consumer completion is unproven")
            completed = True
            return result
        except BaseException:
            self.scope.poisoned = True
            self.refresh_uncertain |= self.scope.claim is not None
            raise AuthError(
                "native metadata execution failed; no auth output retained"
            ) from None
        finally:
            try:
                await self._capture_native(remove=False)
                if completed:
                    if result is None:
                        raise AuthError("native metadata result is unavailable")
                    text = (result.stdout or "") + (result.stderr or "")
                    if any(value in text for value in self.scope.retention.values):
                        raise AuthError(
                            "native metadata output contains credential material"
                        )
                    self._metadata_settled += 1
            except BaseException:
                self.scope.poisoned = True
                self.refresh_uncertain |= self.scope.claim is not None
                raise AuthError(
                    "native metadata writeback failed; lineage blocked"
                ) from None
            finally:
                self.executing = False

    async def _producer_status(self, dispatch: NativeDispatch) -> int:
        remote = self.root + "/producer-snapshot-" + uuid.uuid4().hex
        await self._io("snapshot", dispatch.producer_status_path, remote)
        local = self.scope.directory / ("producer-status-" + uuid.uuid4().hex)
        await self.environment.download_file(remote, local)
        if local.is_symlink():
            raise AuthError("native producer status is not a private regular file")
        local.chmod(0o600)
        value = private_json(read_private(local, limit=1024))
        code = value.get("exit_code")
        if (
            set(value) != {"schema_version", "exit_code", "nonce"}
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or type(code) is not int
            or not 0 <= code <= 255
            or value.get("nonce") != dispatch.nonce
        ):
            raise AuthError("native producer status is invalid; profile blocked")
        await self._io("remove", dispatch.producer_status_path)
        await self._io("remove", remote)
        return code

    async def execute_model(
        self,
        environment: Any,
        command: str,
        *,
        env: dict[str, str] | None = None,
        user: str | int | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
        dispatch: NativeDispatch | None = None,
    ) -> Any:
        """Called ONLY for the exact native model exec by the version-matched adapter.

        Returns Harbor's ExecResult. Capture runs before the caller handles a
        nonzero result and before native Codex.run enters its cleanup finally.
        """
        if dispatch is None:
            dispatch = self._pending.dispatch if self._pending else self.new_dispatch()
        current = self._pending
        if (
            current is None
            or current.dispatch is not dispatch
            or current.consumed
            or self.executing
            or self.binding
            or self.scope.poisoned
            or self.physically_stopped
        ):
            raise AuthError("native dispatch is stale, consumed, or already active")
        current.consumed = True
        # Held through capture/checkpoint, not merely through process return.
        self.executing = True
        self.execution_returned = False
        self.producer_exit_code = None
        self.captured = False
        started = False
        try:
            # bind() never restages while execute is active; do it explicitly so
            # direct dispatch callers and native run() callers share the path.
            await self.bind(environment)
            if not self._credentials_staged:
                await self._restore_credentials()
            effective = self.execution_environment(env)
            name = self.scope.harness.name
            if name == "codex":
                await self._io(
                    "codex-regular" if self.scope.native is not None else "codex-api"
                )
            elif name == "pi":
                await self._io("pi-config")
            await self._status(effective)
            started = True
            current.started = True
            result = await environment.exec(
                command="set -o pipefail; " + command,
                env=effective,
                user=user,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
            current.pipeline_exit_code = result.return_code
            self.producer_exit_code = await self._producer_status(dispatch)
            current.producer_exit_code = self.producer_exit_code
            self.execution_returned = self.producer_exit_code < 128
            # A logging/filter pipeline's status does not describe the native
            # process. Only successful native completion proves refresh settled.
            self.refresh_uncertain |= (
                self.scope.claim is not None and self.producer_exit_code != 0
            )
            if not self.execution_returned or self.refresh_uncertain:
                self.scope.poisoned = True
            # Native completion is separate from logging. Preserve the pipeline
            # code in the dispatch record without masking the producer's result.
            result.return_code = self.producer_exit_code
            return result
        except asyncio.CancelledError:
            self.scope.poisoned = True
            raise
        except Exception:
            self.scope.poisoned = True
            # Native Docker errors can include its exec --env arguments.
            raise AuthError("native model transport failed; profile blocked") from None
        finally:
            if (
                started
                and self.producer_exit_code is None
                and self.scope.claim is not None
            ):
                self.refresh_uncertain = True
            try:
                await self._capture_native(remove=True)
                self.captured = True
                current.captured = True
                if self.scope.harness.name == "opencode":
                    await self._io("opencode-evidence")
            except BaseException:
                self.scope.poisoned = True
                raise AuthError(
                    "native auth writeback capture failed; profile blocked"
                ) from None
            finally:
                self._credentials_staged = False
                current.finished = True
                self._pending = None
                self.executing = False


def current_runtime_auth(harness: ResolvedHarness) -> RuntimeAuthHook | None:
    if harness.auth is None:
        return None
    scope = _current.get()
    if scope is None:
        raise AuthError("explicit auth is missing its runtime credential context")
    return scope.new_hook(harness)


def validate_runtime_auth_request(
    request: Any, *, environment: Mapping[str, str] | None = None
) -> None:
    harness = request.plan.harness
    if harness is None or harness.auth is None:
        return
    from tetrabench.harnesses import validate_explicit_auth_configuration

    if harness.auth.mode == "chatgpt_oauth":
        if request.plan.harbor.concurrency != 1:
            raise AuthError("native subscription auth requires harbor.concurrency=1")
    elif environment is not None and isinstance(
        harness.auth.reference, EnvAuthReference
    ):
        if not environment.get(harness.auth.reference.name):
            raise AuthError(
                "declared model credential reference is missing on this runtime"
            )
    validate_explicit_auth_configuration(harness)


def make_credential_context(
    *,
    engine: Literal["docker", "modal"],
    consumer_id: str,
    run_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    artifact_buckets: Sequence[str] = (),
    forbidden_runtime_roots: Sequence[Path] = (),
):
    """Build outside credential_free_harbor_environment; resolves only on entry."""
    captured_environment = dict(os.environ if environment is None else environment)

    @contextmanager
    def credential_context(
        harness: ResolvedHarness, paths: AttemptPaths
    ) -> Iterator[RuntimeAuthScope]:
        from tetrabench.controller_runtime import _credential_environment_lock

        spec = harness.auth
        if spec is None:
            raise AuthError("credential context requires explicit auth")
        store = None
        parent: str | None = None
        if isinstance(spec.reference, NativeAuthReference):
            config = load_auth_config_file(environment=captured_environment)
            profile = load_auth_profile(config, spec.reference, harness.name)
            store = profile_store(
                profile,
                engine=engine,
                environment=captured_environment,
                artifact_buckets=artifact_buckets,
            )
            parent = config.runtime_directory
        if engine == "modal":
            home = Path.home()
            # In the verified Modal Function image, /tmp is root-owned 0777,
            # without sticky. A 0700 child there does not have safe ancestry.
            # Keep private_directory strict; use an independently private parent.
            selected = (
                Path(parent)
                if parent is not None
                else home / ".tetrabench-native-auth" / "runtime"
            )
            if not selected.is_relative_to(home) and not selected.is_relative_to(
                "/tmp"
            ):  # nosec B108
                raise AuthStateError(
                    "Modal auth staging must use private ephemeral home "
                    "or trusted temporary storage",
                    reason="auth-runtime-location",
                )
            assert_auth_outside_artifacts(
                selected, [paths.root, *forbidden_runtime_roots]
            )
            parent = str(private_directory(selected, create=True))
        elif parent is not None:
            assert_auth_outside_artifacts(
                Path(parent), [paths.root, *forbidden_runtime_roots]
            )
            parent = str(private_directory(Path(parent), create=True))
        with tempfile.TemporaryDirectory(
            prefix="tetrabench-runtime-auth-", dir=parent
        ) as temporary:
            scope = RuntimeAuthScope(
                harness,
                paths,
                engine=engine,
                consumer_id=consumer_id,
                run_id=run_id,
                directory=Path(temporary),
                environment=captured_environment,
                store=store,
            )
            source = (
                spec.reference.name
                if isinstance(spec.reference, EnvAuthReference)
                else None
            )
            # Trial construction occurs inside this boundary. Task env expansion
            # cannot copy model or auth-backend credentials into Compose/artifacts.
            with _credential_environment_lock:
                removed = {
                    key: value
                    for key, value in os.environ.items()
                    if (
                        key in authentication_environment_names()
                        or key == source
                        or key.startswith("TETRABENCH_AUTH_")
                    )
                }
                for key in removed:
                    os.environ.pop(key, None)
                token = _current.set(scope)
                completed = False
                try:
                    yield scope
                    completed = True
                finally:
                    _current.reset(token)
                    for key in tuple(os.environ):
                        if (
                            key in authentication_environment_names()
                            or key == source
                            or key.startswith("TETRABENCH_AUTH_")
                        ):
                            os.environ.pop(key, None)
                    os.environ.update(removed)
                    scope.finalize(require_consumer=completed)

    return credential_context
