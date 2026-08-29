"""Decorator-independent detached controller orchestration."""

from __future__ import annotations

import mimetypes
import os
import posixpath
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from harbor.models.trajectories import Trajectory

from tetrabench.artifact_policy import ArtifactLimits
from tetrabench.canonical_json import dumps_canonical_json, sha256_hex
from tetrabench.controller import (
    ControllerAdmissionService,
    ControllerIdentityError,
    ControllerInvocation,
    TerminalAcknowledgementPending,
    TerminalPublicationUncertain,
)
from tetrabench.harbor import ENVIRONMENT_IMPORT_PATH, child_event_sink
from tetrabench.lifecycle import ChildCleanupObserver
from tetrabench.plan import canonical_model_bytes
from tetrabench.records import (
    ArtifactBinding,
    ArtifactInventoryEntry,
    AttemptEvent,
    ConflictRunState,
    ContentObject,
    RequestRecord,
    TerminalEvidence,
    TerminalRecord,
    TerminalRunState,
    validate_attempt_id,
)

CONTROLLER_ROOT = Path("/tetrabench/controller")
HARBOR_VERSION = "0.22.0"
MODAL_VERSION = "1.5.4"
_PROVIDER_ENVIRONMENT_PREFIXES = ("AWS_", "TIGRIS_")
_LEGACY_PROVIDER_ENVIRONMENT_NAMES = frozenset(
    {"BOTO_CONFIG", "BOTOCORE_TCP_KEEPALIVE"}
)
_credential_environment_lock = threading.RLock()


def _is_provider_environment_name(name: str) -> bool:
    normalized = name.upper()
    return normalized.startswith(_PROVIDER_ENVIRONMENT_PREFIXES) or (
        normalized in _LEGACY_PROVIDER_ENVIRONMENT_NAMES
    )


@contextmanager
def credential_free_harbor_environment() -> Iterator[None]:
    """Hide reviewed provider environment selectors for one Harbor boundary."""
    with _credential_environment_lock:
        saved = {
            name: value
            for name, value in os.environ.items()
            if _is_provider_environment_name(name)
        }
        for name in saved:
            os.environ.pop(name, None)
        try:
            yield
        finally:
            for name in tuple(os.environ):
                if _is_provider_environment_name(name):
                    os.environ.pop(name, None)
            os.environ.update(saved)


class ControllerVolume(Protocol):
    def commit(self) -> None: ...

    def reload(self) -> None: ...


class ControllerRuntimeStore(Protocol):
    def require_coordination_safe(self): ...

    def read_request(
        self, run_id: str, request_sha256: str, request_key: str, /
    ) -> RequestRecord: ...

    def read_admission(self, run_id: str): ...

    def read_run_state(self, run_id: str): ...

    def read_content(self, descriptor: ContentObject) -> bytes: ...

    def publish_content_stream(
        self, stream: BinaryIO, *, media_type: str = "application/octet-stream"
    ) -> ContentObject: ...

    def publish_event(self, event: AttemptEvent) -> str: ...

    def update_admission(self, expected, replacement): ...

    def publish_terminal(self, terminal: TerminalRecord) -> str: ...


@dataclass(frozen=True, slots=True)
class AttemptPaths:
    root: Path
    context: Path
    jobs: Path
    request: Path
    child_events: Path
    controller_plan: Path
    controller_result: Path
    failure: Path


@dataclass(frozen=True, slots=True)
class HarborRunResult:
    """Native job-directory result returned by a future real Harbor runner."""

    outcome: Literal["succeeded", "failed", "cancelled"]
    job_directory: Path
    config_path: Path | None = None
    lock_path: Path | None = None
    result_path: Path | None = None
    reward: str | None = None
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    atif_paths: tuple[Path, ...] = ()


class HarborRunnerProtocol(Protocol):
    def run(
        self,
        request: RequestRecord,
        paths: AttemptPaths,
        *,
        environment_import_path: str,
        labels: dict[str, str],
        event_sink_key: str,
    ) -> HarborRunResult: ...


ArtifactCollectionLimits = ArtifactLimits


@dataclass(frozen=True, slots=True)
class ControllerRuntimeResult:
    state: Literal["skipped", "terminal", "failed"]
    run_id: str
    attempt_id: str | None = None
    terminal_sha256: str | None = None
    detail: str = ""


class ControllerCancelled(RuntimeError):
    pass


class ChildCleanupError(RuntimeError):
    pass


def parse_controller_invocation(payload: bytes, digest: str) -> ControllerInvocation:
    """Verify the complete canonical invocation before constructing any client."""
    from tetrabench.plan import parse_canonical_model

    if sha256_hex(payload) != digest:
        raise ControllerIdentityError("controller invocation digest mismatch")
    return parse_canonical_model(payload, ControllerInvocation)


def attempt_paths(root: Path, run_id: str, attempt_id: str) -> AttemptPaths:
    attempt = root / "runs" / run_id / "attempts" / attempt_id
    return AttemptPaths(
        root=attempt,
        context=attempt / "context",
        jobs=attempt / "jobs",
        request=attempt / "request.json",
        child_events=attempt / "child-events.jsonl",
        controller_plan=attempt / "controller-plan.json",
        controller_result=attempt / "controller-result.json",
        failure=attempt / "failure.json",
    )


def _write_new(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
    path.chmod(mode)


def _package_metadata(run_id: str, attempt_id: str, plan_sha256: str) -> bytes:
    return dumps_canonical_json(
        {
            "attempt_id": attempt_id,
            "environment_import_path": ENVIRONMENT_IMPORT_PATH,
            "harbor_version": HARBOR_VERSION,
            "modal_version": MODAL_VERSION,
            "plan_sha256": plan_sha256,
            "run_id": run_id,
            "schema_version": 1,
            "tetrabench_version": version("tetrabench"),
        }
    )


def _media_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _bounded_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if not name.isascii() or not name.replace("_", "").isalnum():
        return "Exception"
    return name[:64]


def _failure_code(error: BaseException) -> str:
    if isinstance(error, ChildCleanupError):
        return (
            "child-not-quiescent"
            if str(error) == "not-quiescent"
            else "child-cleanup-error"
        )
    if isinstance(error, ControllerIdentityError):
        return "identity-rejected"
    if isinstance(error, OSError):
        return "filesystem-error"
    if isinstance(error, ValueError):
        return "invalid-output"
    if isinstance(error, RuntimeError):
        return "runtime-error"
    return "unexpected-error"


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_ctime_ns,
        value.st_mtime_ns,
    )


def _relative_parts(root: Path, path: Path) -> tuple[str, ...]:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("Harbor output escaped the attempt root") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Harbor output has an unsafe attempt-relative path")
    return relative.parts


def _open_directory_no_follow(path: Path) -> int:
    absolute = path.absolute()
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _open_relative_no_follow(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for index, component in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index + 1 < len(parts):
                flags |= os.O_DIRECTORY
            next_fd = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _walk_regular_files(
    directory_fd: int,
    relative: tuple[str, ...],
) -> Iterator[tuple[tuple[str, ...], tuple[int, int, int, int, int]]]:
    for name in sorted(os.listdir(directory_fd)):
        try:
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise ValueError(
                "artifact path is a symlink or cannot be opened safely"
            ) from error
        try:
            metadata = os.fstat(child_fd)
            child_relative = (*relative, name)
            if stat.S_ISDIR(metadata.st_mode):
                yield from _walk_regular_files(child_fd, child_relative)
            elif stat.S_ISREG(metadata.st_mode):
                yield child_relative, _file_identity(metadata)
            else:
                raise ValueError("artifact path is not a regular file or directory")
        finally:
            os.close(child_fd)


class ControllerRuntime:
    """Run one admitted attempt while preserving S3 and Volume boundaries."""

    def __init__(
        self,
        store: ControllerRuntimeStore,
        volume: ControllerVolume,
        runner: HarborRunnerProtocol,
        observer: ChildCleanupObserver,
        *,
        controller_root: Path = CONTROLLER_ROOT,
        attempt_id: Callable[[], str] | None = None,
        cleanup_sweeps: int = 5,
        cleanup_delay_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        artifact_limits: ArtifactCollectionLimits | None = None,
    ) -> None:
        if cleanup_sweeps < 2:
            raise ValueError("replay cleanup requires at least two sweeps")
        if cleanup_delay_seconds < 0:
            raise ValueError("replay cleanup delay must be non-negative")
        self._store = store
        self._volume = volume
        self._runner = runner
        self._observer = observer
        self._root = controller_root
        self._attempt_id = attempt_id or (lambda: f"attempt-{uuid.uuid4().hex}")
        self._cleanup_sweeps = cleanup_sweeps
        self._cleanup_delay_seconds = cleanup_delay_seconds
        self._sleep = sleep
        self._artifact_limits = artifact_limits or ArtifactCollectionLimits()
        self._admission = ControllerAdmissionService(store)

    def run(
        self,
        invocation: ControllerInvocation,
        *,
        function_call_id: str,
    ) -> ControllerRuntimeResult:
        self._store.require_coordination_safe()
        with credential_free_harbor_environment():
            return self._run_credential_free(
                invocation,
                function_call_id=function_call_id,
            )

    def _run_credential_free(
        self,
        invocation: ControllerInvocation,
        *,
        function_call_id: str,
    ) -> ControllerRuntimeResult:
        attempt_id: str | None = None
        paths: AttemptPaths | None = None
        admitted = False
        phase = "startup-reconciliation"
        try:
            terminal = self._store.read_run_state(invocation.run_id)
            if isinstance(terminal, ConflictRunState):
                raise ControllerIdentityError("; ".join(terminal.reasons))
            if isinstance(terminal, TerminalRunState):
                digest, acknowledged = self._admission.reconcile_terminal(
                    invocation,
                    terminal.terminal,
                )
                return ControllerRuntimeResult(
                    state="terminal",
                    run_id=invocation.run_id,
                    attempt_id=terminal.terminal.winning_attempt_id,
                    terminal_sha256=digest,
                    detail=(
                        "reconciled existing terminal proof"
                        if acknowledged
                        else (
                            "terminal proof is durable; "
                            "admission acknowledgement pending"
                        )
                    ),
                )

            phase = "admission-claim"
            decision = self._admission.claim(invocation, function_call_id)
            if not decision.admitted:
                return ControllerRuntimeResult(
                    state="skipped",
                    run_id=invocation.run_id,
                    detail=decision.detail,
                )
            admitted = True

            phase = "post-claim-reconciliation"
            terminal = self._store.read_run_state(invocation.run_id)
            if isinstance(terminal, ConflictRunState):
                raise ControllerIdentityError("; ".join(terminal.reasons))
            if isinstance(terminal, TerminalRunState):
                digest, acknowledged = self._admission.reconcile_terminal(
                    invocation,
                    terminal.terminal,
                )
                return ControllerRuntimeResult(
                    state="terminal",
                    run_id=invocation.run_id,
                    attempt_id=terminal.terminal.winning_attempt_id,
                    terminal_sha256=digest,
                    detail=(
                        "reconciled existing terminal proof"
                        if acknowledged
                        else (
                            "terminal proof is durable; "
                            "admission acknowledgement pending"
                        )
                    ),
                )
            request = self._admission.validate_invocation(invocation)
            self._check_cancellation(invocation.run_id, function_call_id)
            phase = "interrupted-volume-commit"
            self._volume.commit()
            phase = "attempt-setup"
            attempts_root = self._root / "runs" / invocation.run_id / "attempts"
            prior_attempts = (
                tuple(sorted(path.name for path in attempts_root.iterdir()))
                if attempts_root.exists()
                else ()
            )
            cleanup_evidence: tuple[str, ...] = ()
            if prior_attempts:
                phase = "prior-child-cleanup"
                cleanup_evidence = self._quiesce_children(invocation.run_id)
                self._check_cancellation(invocation.run_id, function_call_id)

            phase = "attempt-setup"
            attempt_id = validate_attempt_id(self._attempt_id())
            paths = attempt_paths(self._root, invocation.run_id, attempt_id)
            paths.root.mkdir(parents=True, exist_ok=False)
            self._volume.commit()
            self._store.publish_event(
                AttemptEvent(
                    schema_version=1,
                    run_id=invocation.run_id,
                    attempt_id=attempt_id,
                    sequence=0,
                    type="attempt-started",
                    payload={"function_call_id": function_call_id},
                )
            )
            next_event_sequence = 1
            if prior_attempts:
                self._store.publish_event(
                    AttemptEvent(
                        schema_version=1,
                        run_id=invocation.run_id,
                        attempt_id=attempt_id,
                        sequence=next_event_sequence,
                        type="replay-reconciled",
                        payload={
                            "cleanup_evidence": list(cleanup_evidence),
                            "prior_attempts": list(prior_attempts),
                        },
                    )
                )
                next_event_sequence += 1

            phase = "materialization"
            request = self._materialize(request, attempt_id, paths)
            self._volume.commit()
            self._volume.reload()
            self._check_cancellation(invocation.run_id, function_call_id)

            labels = {
                "tetrabench.run_id": invocation.run_id,
                "tetrabench.attempt_id": attempt_id,
                "tetrabench.plan_sha256": invocation.plan_sha256,
            }

            child_sequence = next_event_sequence

            def publish_child_event(event: AttemptEvent) -> None:
                nonlocal child_sequence
                sequenced = event.model_copy(update={"sequence": child_sequence})
                child_sequence += 1
                self._store.publish_event(sequenced)

            phase = "harbor-execution"
            with child_event_sink(publish_child_event) as event_sink_key:
                result = self._runner.run(
                    request,
                    paths,
                    environment_import_path=ENVIRONMENT_IMPORT_PATH,
                    labels=labels,
                    event_sink_key=event_sink_key,
                )
            self._volume.commit()
            self._volume.reload()
            if self._is_cancelling(invocation.run_id, function_call_id):
                self._quiesce_children(invocation.run_id)
                result = HarborRunResult(
                    outcome="cancelled",
                    job_directory=result.job_directory,
                    config_path=result.config_path,
                    lock_path=result.lock_path,
                    result_path=result.result_path,
                    evidence=(
                        *result.evidence,
                        "cancellation observed after Harbor return",
                    ),
                    warnings=result.warnings,
                    atif_paths=result.atif_paths,
                )
            phase = "artifact-validation"
            self._validate_runner_result(paths, result)
            _write_new(
                paths.controller_result,
                dumps_canonical_json(
                    {
                        "attempt_id": attempt_id,
                        "harbor_version": HARBOR_VERSION,
                        "modal_version": MODAL_VERSION,
                        "outcome": result.outcome,
                        "run_id": invocation.run_id,
                        "schema_version": 1,
                        "tetrabench_version": version("tetrabench"),
                    }
                ),
            )
            self._volume.commit()
            self._volume.reload()
            phase = "artifact-publication"
            terminal_record = self._build_terminal(
                invocation,
                attempt_id,
                paths,
                result,
            )
            phase = "terminal-publication"
            digest = self._admission.publish_terminal_and_finish(
                invocation,
                terminal_record,
                function_call_id=function_call_id,
            )
            return ControllerRuntimeResult(
                state="terminal",
                run_id=invocation.run_id,
                attempt_id=attempt_id,
                terminal_sha256=digest,
                detail=f"published {result.outcome} terminal",
            )
        except TerminalAcknowledgementPending as condition:
            return ControllerRuntimeResult(
                state="terminal",
                run_id=invocation.run_id,
                attempt_id=attempt_id,
                terminal_sha256=condition.terminal_sha256,
                detail="terminal proof is durable; admission acknowledgement pending",
            )
        except TerminalPublicationUncertain:
            if admitted:
                self._admission.mark_failed(
                    invocation.run_id,
                    function_call_id=function_call_id,
                )
            return ControllerRuntimeResult(
                state="failed",
                run_id=invocation.run_id,
                attempt_id=attempt_id,
                detail="terminal-publication-uncertain",
            )
        except ControllerCancelled:
            return ControllerRuntimeResult(
                state="skipped",
                run_id=invocation.run_id,
                attempt_id=attempt_id,
                detail="cancellation observed before run work",
            )
        except BaseException as error:
            error_code = _failure_code(error)
            print(
                "tetrabench controller failed "
                f"phase={phase} error={type(error).__name__} code={error_code}"
                + (f" detail={error}" if isinstance(error, ChildCleanupError) else ""),
                flush=True,
            )
            if paths is not None and attempt_id is not None:
                self._publish_failure_evidence(
                    invocation,
                    attempt_id,
                    paths,
                    error,
                    phase=phase,
                )
            if admitted:
                self._admission.mark_failed(
                    invocation.run_id,
                    function_call_id=function_call_id,
                )
            return ControllerRuntimeResult(
                state="failed",
                run_id=invocation.run_id,
                attempt_id=attempt_id,
                detail=type(error).__name__,
            )

    def _check_cancellation(self, run_id: str, function_call_id: str) -> None:
        if self._is_cancelling(run_id, function_call_id):
            self._quiesce_children(run_id)
            raise ControllerCancelled

    def _is_cancelling(self, run_id: str, function_call_id: str) -> bool:
        observed = self._store.read_admission(run_id)
        if observed is None:
            raise ControllerIdentityError("admission disappeared during controller run")
        record = observed.record
        if record.owner_function_call_id != function_call_id:
            raise ControllerIdentityError("controller lost admission ownership")
        if record.state in {"cancelling", "cancelled"}:
            return True
        if record.state != "running":
            raise ControllerIdentityError(
                f"controller cannot continue from admission state {record.state!r}"
            )
        return False

    def _quiesce_children(self, run_id: str) -> tuple[str, ...]:
        empty = 0
        evidence: list[str] = []
        for sweep_index in range(self._cleanup_sweeps):
            try:
                result = self._observer.sweep(run_id)
            except BaseException as error:
                raise ChildCleanupError(f"observer-{type(error).__name__}") from error
            evidence.append(result.evidence)
            empty = empty + 1 if not result.remaining_child_ids else 0
            if empty >= 2:
                return tuple(evidence)
            if sweep_index + 1 < self._cleanup_sweeps:
                self._sleep(self._cleanup_delay_seconds)
        raise ChildCleanupError("not-quiescent")

    def _materialize(
        self,
        request: RequestRecord,
        attempt_id: str,
        paths: AttemptPaths,
    ) -> RequestRecord:
        _write_new(paths.request, canonical_model_bytes(request))
        for item in request.context_manifest.files:
            data = self._store.read_content(item.content)
            valid = (
                len(data) == item.content.size
                and sha256_hex(data) == item.content.sha256
            )
            if not valid:
                raise ControllerIdentityError(
                    "materialized context failed verification"
                )
            _write_new(paths.context / item.destination, data, mode=item.mode)
        _write_new(
            paths.controller_plan,
            _package_metadata(request.run_id, attempt_id, request.plan_sha256),
        )
        paths.jobs.mkdir()
        return request

    @staticmethod
    def _validate_runner_result(paths: AttemptPaths, result: HarborRunResult) -> None:
        job_parts = _relative_parts(paths.root, result.job_directory)
        jobs_parts = _relative_parts(paths.root, paths.jobs)
        if job_parts[: len(jobs_parts)] != jobs_parts:
            raise ValueError(
                "Harbor runner job directory escaped the attempt jobs root"
            )
        for role in (result.config_path, result.lock_path, result.result_path):
            if role is not None:
                role_parts = _relative_parts(paths.root, role)
                if role_parts[: len(job_parts)] != job_parts or role_parts == job_parts:
                    raise ValueError(
                        "Harbor artifact binding escaped the job directory"
                    )
        for path in result.atif_paths:
            atif_parts = _relative_parts(paths.root, path)
            if atif_parts[: len(job_parts)] != job_parts or atif_parts == job_parts:
                raise ValueError("Harbor ATIF path escaped the job directory")

    def _atif_evidence(
        self,
        paths: AttemptPaths,
        inventory: tuple[ArtifactInventoryEntry, ...],
        atif_paths: tuple[Path, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        by_path = {item.logical_path: item for item in inventory}
        expected = tuple(
            f"attempts/{paths.root.name}/{path.relative_to(paths.root).as_posix()}"
            for path in atif_paths
        )
        warnings = tuple(
            f"ATIF missing: {path.removeprefix(f'attempts/{paths.root.name}/')} "
            "was not emitted"
            for path in expected
            if path not in by_path
        )
        discovered: set[str] = set()
        pending = [path for path in expected if path in by_path]
        while pending:
            logical_path = pending.pop()
            if logical_path in discovered:
                continue
            item = by_path[logical_path]
            trajectory = Trajectory.model_validate_json(
                self._store.read_content(item.content)
            )
            discovered.add(logical_path)
            reference = trajectory.continued_trajectory_ref
            if reference is None:
                continue
            if reference.startswith("/"):
                raise ValueError("ATIF continuation reference must be relative")
            joined = posixpath.normpath(
                posixpath.join(posixpath.dirname(logical_path), reference)
            )
            job_prefix = (
                f"attempts/{paths.root.name}/"
                f"{paths.jobs.relative_to(paths.root).as_posix()}/"
            )
            if not joined.startswith(job_prefix) or joined not in by_path:
                raise ValueError("ATIF continuation reference is absent or escaped")
            pending.append(joined)
        evidence = (
            f"Secure artifact inventory contains {len(discovered)} ATIF trajectory "
            "file(s)",
        )
        return evidence, warnings

    def _publish_open_file(
        self,
        descriptor: int,
        path: Path,
    ) -> ContentObject:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("artifact descriptor is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            stream.seek(0)
            content = self._store.publish_content_stream(
                stream,
                media_type=_media_type(path),
            )
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after):
            raise ValueError("artifact file changed while publishing")
        if content.size != before.st_size:
            raise ValueError("published artifact size does not match its descriptor")
        return content

    def _publish_artifacts(
        self,
        paths: AttemptPaths,
        *,
        individual_files: tuple[Path, ...],
        directory: Path | None,
    ) -> tuple[ArtifactInventoryEntry, ...]:
        inventory: list[ArtifactInventoryEntry] = []
        attempt_fd = _open_directory_no_follow(paths.root)
        try:
            pending: list[tuple[tuple[str, ...], tuple[int, int, int, int, int]]] = []
            for path in individual_files:
                parts = _relative_parts(paths.root, path)
                descriptor = _open_relative_no_follow(attempt_fd, parts)
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError("artifact binding is not a regular file")
                    pending.append((parts, _file_identity(metadata)))
                finally:
                    os.close(descriptor)

            if directory is not None:
                directory_parts = _relative_parts(paths.root, directory)
                directory_fd = _open_relative_no_follow(attempt_fd, directory_parts)
                try:
                    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                        raise ValueError("Harbor runner did not return a directory")
                    pending.extend(_walk_regular_files(directory_fd, directory_parts))
                finally:
                    os.close(directory_fd)

            if len({parts for parts, _identity in pending}) != len(pending):
                raise ValueError("artifact collection contains duplicate logical paths")
            if len(pending) > self._artifact_limits.max_files:
                raise ValueError("artifact collection exceeds max_files")
            total_bytes = 0
            for _parts, identity in pending:
                size = identity[2]
                if size > self._artifact_limits.max_file_bytes:
                    raise ValueError("artifact collection file exceeds max_file_bytes")
                total_bytes += size
                if total_bytes > self._artifact_limits.max_total_bytes:
                    raise ValueError("artifact collection exceeds max_total_bytes")

            for parts, expected_identity in pending:
                descriptor = _open_relative_no_follow(attempt_fd, parts)
                try:
                    if _file_identity(os.fstat(descriptor)) != expected_identity:
                        raise ValueError(
                            "artifact file changed after collection preflight"
                        )
                    logical_path = f"attempts/{paths.root.name}/{'/'.join(parts)}"
                    content = self._publish_open_file(
                        descriptor,
                        paths.root.joinpath(*parts),
                    )
                    inventory.append(
                        ArtifactInventoryEntry(
                            logical_path=logical_path,
                            content=content,
                        )
                    )
                finally:
                    os.close(descriptor)
        finally:
            os.close(attempt_fd)
        return tuple(sorted(inventory, key=lambda item: item.logical_path))

    def _build_terminal(
        self,
        invocation: ControllerInvocation,
        attempt_id: str,
        paths: AttemptPaths,
        result: HarborRunResult,
    ) -> TerminalRecord:
        inventory = self._publish_artifacts(
            paths,
            individual_files=(paths.controller_plan, paths.controller_result),
            directory=result.job_directory,
        )
        atif_evidence, atif_warnings = self._atif_evidence(
            paths,
            inventory,
            result.atif_paths,
        )
        by_path = {item.logical_path: item for item in inventory}

        def binding(path: Path | None) -> ArtifactBinding | None:
            if path is None:
                return None
            logical = f"attempts/{attempt_id}/{path.relative_to(paths.root).as_posix()}"
            item = by_path[logical]
            return ArtifactBinding(logical_path=logical, sha256=item.content.sha256)

        return TerminalRecord(
            schema_version=1,
            run_id=invocation.run_id,
            request_sha256=invocation.request_sha256,
            winning_attempt_id=attempt_id,
            outcome=result.outcome,
            harbor_version=HARBOR_VERSION,
            artifacts=inventory,
            harbor_config=binding(result.config_path),
            harbor_lock=binding(result.lock_path),
            harbor_result=binding(result.result_path),
            evidence=tuple(
                TerminalEvidence(type="harbor-runner", message=message)
                for message in (*result.evidence, *atif_evidence)
            ),
            warnings=(*result.warnings, *atif_warnings),
        )

    def _publish_failure_evidence(
        self,
        invocation: ControllerInvocation,
        attempt_id: str,
        paths: AttemptPaths,
        error: BaseException,
        *,
        phase: str,
    ) -> None:
        try:
            _write_new(
                paths.failure,
                dumps_canonical_json(
                    {
                        "attempt_id": attempt_id,
                        "error_code": _failure_code(error),
                        "error_type": _bounded_error_type(error),
                        "phase": phase,
                        "run_id": invocation.run_id,
                        "schema_version": 1,
                    }
                ),
            )
            self._volume.commit()
            self._volume.reload()
            individual_files = tuple(
                path
                for path in (
                    paths.controller_plan,
                    paths.controller_result,
                    paths.failure,
                )
                if path.exists()
            )
            inventory = self._publish_artifacts(
                paths,
                individual_files=individual_files,
                directory=paths.jobs if paths.jobs.exists() else None,
            )
            self._store.publish_event(
                AttemptEvent(
                    schema_version=1,
                    run_id=invocation.run_id,
                    attempt_id=attempt_id,
                    sequence=(1 << 53) - 1,
                    type="controller-failed",
                    payload={
                        "artifacts": [
                            item.model_dump(mode="json") for item in inventory
                        ],
                        "error_code": _failure_code(error),
                        "error_type": _bounded_error_type(error),
                        "phase": phase,
                    },
                )
            )
        except Exception:
            # The original failure remains primary; admission still records failed.
            return
