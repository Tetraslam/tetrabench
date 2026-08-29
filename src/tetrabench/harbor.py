"""Harbor v0.22 Modal child observation through public extension surfaces."""

from __future__ import annotations

import contextlib
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol

import modal
from harbor.environments.modal import ModalEnvironment

from tetrabench.plan import canonical_model_bytes
from tetrabench.records import AttemptEvent

HARBOR_APP_NAME = "__harbor__"
ENVIRONMENT_IMPORT_PATH = "tetrabench.harbor:TetrabenchModalEnvironment"
RUN_LABEL = "tetrabench.run_id"
ATTEMPT_LABEL = "tetrabench.attempt_id"
PLAN_LABEL = "tetrabench.plan_sha256"


class ChildEventSink(Protocol):
    def __call__(self, event: AttemptEvent, /) -> None: ...


_event_sinks: dict[str, ChildEventSink] = {}
_event_sinks_lock = threading.Lock()


@contextlib.contextmanager
def child_event_sink(sink: ChildEventSink) -> Iterator[str]:
    """Register one invocation-scoped, controller-process-only event sink."""
    key = secrets.token_urlsafe(32)
    with _event_sinks_lock:
        _event_sinks[key] = sink
    try:
        yield key
    finally:
        with _event_sinks_lock:
            _event_sinks.pop(key, None)


def _registered_event_sink(key: str) -> ChildEventSink:
    with _event_sinks_lock:
        sink = _event_sinks.get(key)
    if sink is None:
        raise RuntimeError("Harbor child event sink is unavailable or expired")
    return sink


def _sandbox_v2_requested(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


class TetrabenchModalEnvironment(ModalEnvironment):
    """Observe Harbor-owned Modal children without changing Harbor lifecycle."""

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: Any,
        task_env_config: Any,
        run_id: str,
        attempt_id: str,
        plan_sha256: str,
        event_sink_key: str,
        observation_path: str,
        app_name: str = HARBOR_APP_NAME,
        labels: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        if _sandbox_v2_requested(kwargs.get("modal_sandbox_v2", False)):
            raise ValueError("tetrabench rejects Modal sandbox v2 for Harbor 0.22.0")
        owned = {
            RUN_LABEL: run_id,
            ATTEMPT_LABEL: attempt_id,
            PLAN_LABEL: plan_sha256,
        }
        supplied = dict(labels or {})
        for key, value in owned.items():
            if key in supplied and supplied[key] != value:
                raise ValueError(f"label {key!r} conflicts with tetrabench identity")
        supplied.update(owned)
        self._observation_path = Path(observation_path)
        self._lookup_app_name = app_name
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._plan_sha256 = plan_sha256
        self._event_sink_key = event_sink_key
        self._sequence = 0
        self._tetrabench_labels = supplied
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            app_name=app_name,
            labels=supplied,
            **kwargs,
        )

    def _record(
        self,
        phase: str,
        *,
        sandbox_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "app_name": self._lookup_app_name,
            "labels": self._tetrabench_labels,
            "phase": phase,
            "plan_sha256": self._plan_sha256,
            "sandbox_id": sandbox_id,
            "session_id": self.session_id,
        }
        if detail is not None:
            payload["detail"] = detail
        event = AttemptEvent(
            schema_version=1,
            run_id=self._run_id,
            attempt_id=self._attempt_id,
            sequence=self._sequence,
            type="modal-child",
            payload=payload,
        )
        self._sequence += 1
        data = canonical_model_bytes(event)
        self._observation_path.parent.mkdir(parents=True, exist_ok=True)
        with self._observation_path.open("ab") as stream:
            stream.write(data + b"\n")
            stream.flush()
        _registered_event_sink(self._event_sink_key)(event)

    async def _lookup(self) -> modal.Sandbox:
        return await modal.Sandbox.from_name.aio(
            self._lookup_app_name,
            self.session_id,
        )

    async def start(self, force_build: bool) -> None:
        self._record("creation-started")
        try:
            await super().start(force_build)
        except BaseException as error:
            try:
                sandbox = await self._lookup()
            except Exception:
                self._record("start-failed", detail=type(error).__name__)
            else:
                self._record(
                    "start-failed-after-create",
                    sandbox_id=sandbox.object_id,
                    detail=type(error).__name__,
                )
            raise
        try:
            sandbox = await self._lookup()
        except BaseException as error:
            self._record("start-failed", detail=type(error).__name__)
            raise
        self._record("running", sandbox_id=sandbox.object_id)

    async def stop(self, delete: bool) -> None:
        try:
            sandbox = await self._lookup()
        except Exception:
            sandbox_id = None
        else:
            sandbox_id = sandbox.object_id
        self._record("stop-started", sandbox_id=sandbox_id)
        try:
            await super().stop(delete)
        except BaseException as error:
            self._record(
                "stop-failed",
                sandbox_id=sandbox_id,
                detail=type(error).__name__,
            )
            raise
        self._record("harbor-stop-returned", sandbox_id=sandbox_id)


class ChildIdentitySource(Protocol):
    def list_child_ids(self, run_id: str) -> tuple[str, ...]: ...


class S3ChildIdentitySource:
    """Read controller-published child identities from immutable attempt events."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def list_child_ids(self, run_id: str) -> tuple[str, ...]:
        child_ids: set[str] = set()
        for event in self._store.read_attempt_events(run_id):
            if event.type != "modal-child" or not isinstance(event.payload, Mapping):
                continue
            sandbox_id = event.payload.get("sandbox_id")
            if isinstance(sandbox_id, str) and sandbox_id:
                child_ids.add(sandbox_id)
        return tuple(sorted(child_ids))


class ModalChildObserver:
    """Terminate persisted and tag-discovered Harbor children with public APIs."""

    def __init__(
        self,
        identities: ChildIdentitySource,
        *,
        environment_name: str,
        app_name: str = HARBOR_APP_NAME,
        app_lookup: Callable[..., Any] = modal.App.lookup,
        sandbox_type: Any = modal.Sandbox,
    ) -> None:
        self._identities = identities
        self._environment_name = environment_name
        self._app_name = app_name
        self._app_lookup = app_lookup
        self._sandbox_type = sandbox_type

    def _listed(self, run_id: str) -> dict[str, Any]:
        try:
            app = self._app_lookup(
                self._app_name,
                environment_name=self._environment_name,
                create_if_missing=False,
            )
        except modal.exception.NotFoundError:
            return {}
        return {
            sandbox.object_id: sandbox
            for sandbox in self._sandbox_type.list(
                app_id=app.object_id,
                tags={RUN_LABEL: run_id},
            )
        }

    def sweep(self, run_id: str):
        # Imported lazily to avoid a lifecycle -> harbor import cycle.
        from tetrabench.lifecycle import ChildSweepResult

        listed = self._listed(run_id)
        handles = dict(listed)
        for sandbox_id in self._identities.list_child_ids(run_id):
            handles.setdefault(sandbox_id, self._sandbox_type.from_id(sandbox_id))
        terminated: list[str] = []
        for sandbox_id, sandbox in handles.items():
            try:
                sandbox.terminate(wait=True)
            except modal.exception.NotFoundError:
                continue
            terminated.append(sandbox_id)
        remaining = tuple(sorted(self._listed(run_id)))
        evidence = (
            f"terminated {len(terminated)} child sandbox(es); "
            f"{len(remaining)} remain visible"
        )
        return ChildSweepResult(
            run_id=run_id,
            remaining_child_ids=remaining,
            evidence=evidence,
        )
