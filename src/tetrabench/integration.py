"""Non-production construction and local execution for integration fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from tetrabench.canonical_json import sha256_hex
from tetrabench.context import SealedContext, seal_context
from tetrabench.controller import ControllerInvocation
from tetrabench.controller_runtime import (
    ControllerRuntime,
    ControllerRuntimeResult,
    HarborRunResult,
    attempt_paths,
)
from tetrabench.harbor import (
    ATTEMPT_LABEL,
    ENVIRONMENT_IMPORT_PATH,
    PLAN_LABEL,
    RUN_LABEL,
)
from tetrabench.harbor_runner import HarborRunner
from tetrabench.lifecycle import ChildSweepResult
from tetrabench.models import (
    ContextConfig,
    ContextFileSpec,
    ProjectConfig,
    ResolvedContextFile,
    ResolvedHarborConfig,
    ResolvedPlan,
    ResolvedTaskSelection,
    ResolvedTrial,
)
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.records import (
    AttemptEvent,
    ContentObject,
    RequestRecord,
    TerminalRecord,
    TerminalRunState,
    UnknownRunState,
    new_admission,
)
from tetrabench.s3 import AdmissionRead
from tetrabench.storage import content_object_key
from tetrabench.submission import PreparedSubmission, resolve_controller_launch

FIXTURE_DESTINATION = "fixture-task"


@dataclass(frozen=True, slots=True)
class LocalFixtureResult:
    run: HarborRunResult
    invocation_root: Path
    runtime: str


@dataclass(frozen=True, slots=True)
class LocalCompositionResult:
    controller: ControllerRuntimeResult
    terminal: TerminalRecord
    invocation_root: Path
    published_content: dict[str, bytes]
    runtime: str


class _MemoryVolume:
    def commit(self) -> None:
        pass

    def reload(self) -> None:
        pass


class _EmptyObserver:
    def sweep(self, run_id: str) -> ChildSweepResult:
        return ChildSweepResult(
            run_id=run_id,
            remaining_child_ids=(),
            evidence="local Docker fixture has no Modal children",
        )


class _CapturingHarborRunner:
    def __init__(self) -> None:
        self._runner = HarborRunner()
        self.error: BaseException | None = None

    def run(self, *args, **kwargs):
        try:
            return self._runner.run(*args, **kwargs)
        except BaseException as error:
            self.error = error
            raise


class _MemoryStore:
    def __init__(self, prepared: PreparedSubmission) -> None:
        self.request = prepared.request
        self.request_sha256 = sha256_hex(canonical_model_bytes(self.request))
        self.request_key = (
            f"runs/{self.request.run_id}/requests/{self.request_sha256}.json"
        )
        self.admission = AdmissionRead(
            new_admission(self.request, timestamp="2026-08-28T00:00:00Z"),
            "etag-0",
        )
        self.content = {
            item.descriptor.sha256: item.content
            for item in prepared.sealed_context.files
        }
        self.events: list[AttemptEvent] = []
        self.terminal: TerminalRecord | None = None

    def require_coordination_safe(self):
        return None

    def read_request(self, run_id: str, request_sha256: str, request_key: str, /):
        if (run_id, request_sha256, request_key) != (
            self.request.run_id,
            self.request_sha256,
            self.request_key,
        ):
            raise ValueError("in-memory request identity mismatch")
        return self.request

    def read_admission(self, run_id: str):
        return self.admission if run_id == self.request.run_id else None

    def read_run_state(self, run_id: str):
        if self.terminal is None:
            return UnknownRunState(run_id=run_id)
        digest = sha256_hex(canonical_model_bytes(self.terminal))
        return TerminalRunState(
            run_id=run_id,
            terminal_sha256=digest,
            terminal=self.terminal,
        )

    def read_content(self, descriptor: ContentObject) -> bytes:
        data = self.content[descriptor.sha256]
        if len(data) != descriptor.size or sha256_hex(data) != descriptor.sha256:
            raise ValueError("in-memory content identity mismatch")
        return data

    def publish_content_stream(
        self,
        stream: BinaryIO,
        *,
        media_type: str = "application/octet-stream",
    ) -> ContentObject:
        data = stream.read()
        digest = sha256_hex(data)
        self.content.setdefault(digest, data)
        return ContentObject(
            sha256=digest,
            key=content_object_key(digest),
            size=len(data),
            media_type=media_type,
        )

    def publish_event(self, event: AttemptEvent) -> str:
        self.events.append(event)
        return sha256_hex(canonical_model_bytes(event))

    def update_admission(self, expected: AdmissionRead, replacement):
        if expected.etag != self.admission.etag:
            raise RuntimeError("in-memory admission CAS conflict")
        self.admission = AdmissionRead(replacement, f"etag-{replacement.revision}")
        return self.admission

    def publish_terminal(self, terminal: TerminalRecord) -> str:
        if self.terminal is not None and self.terminal != terminal:
            raise RuntimeError("conflicting in-memory terminal")
        self.terminal = terminal
        return sha256_hex(canonical_model_bytes(terminal))


def _fixture_context(task_directory: Path) -> ContextConfig:
    task_directory = task_directory.resolve()
    if not task_directory.is_dir():
        raise ValueError(f"fixture task does not exist: {task_directory}")
    files = []
    for path in sorted(task_directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("fixture task cannot contain symlinks")
        if path.is_file():
            relative = path.relative_to(task_directory).as_posix()
            files.append(
                ContextFileSpec(
                    source=str(path),
                    destination=f"{FIXTURE_DESTINATION}/{relative}",
                )
            )
    return ContextConfig(files=files)


def prepare_fixture_submission(
    task_directory: Path,
    config: ProjectConfig,
    *,
    run_id: str,
    profile: str | None = None,
) -> PreparedSubmission:
    """Seal the fixture without placing it in a benchmark catalog."""
    task_directory = task_directory.resolve()
    if config.storage is None:
        key_prefix = ""
    else:
        key_prefix = config.storage.prefix
    sealed = seal_context(
        task_directory.parent,
        _fixture_context(task_directory),
        key_prefix=key_prefix,
    )
    plan = ResolvedPlan(
        schema_version=1,
        section="integration",
        controller=config.controller.model_dump(mode="python"),
        execution=config.execution.model_dump(mode="python"),
        storage=(
            config.storage.model_dump(mode="python")
            if config.storage is not None
            else None
        ),
        selection=ResolvedTaskSelection(),
        harbor=ResolvedHarborConfig.model_validate(
            config.harbor.model_dump(mode="python")
        ),
        context=tuple(
            ResolvedContextFile(
                destination=item.destination,
                mode=item.mode,
                size=item.content.size,
                sha256=item.content.sha256,
            )
            for item in sealed.manifest.files
        ),
        trials=(
            ResolvedTrial(
                task_id="fixture-task",
                harbor_task=FIXTURE_DESTINATION,
            ),
        ),
        runnable=True,
        not_runnable_reasons=(),
    )
    request = RequestRecord(
        schema_version=1,
        run_id=run_id,
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(sealed.manifest)),
        context_manifest=sealed.manifest,
    )
    return PreparedSubmission(
        plan=plan,
        sealed_context=sealed,
        request=request,
        controller_launch=resolve_controller_launch(config, profile),
    )


def _materialize(sealed: SealedContext, destination: Path) -> None:
    by_digest = {item.descriptor.sha256: item.content for item in sealed.files}
    for item in sealed.manifest.files:
        path = destination / item.destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(by_digest[item.content.sha256])
        path.chmod(item.mode)


def run_local_fixture(
    task_directory: Path,
    output_root: Path,
    *,
    run_id: str = "fixture-local",
) -> LocalFixtureResult:
    """Run one real Harbor Docker job attached and return native paths."""
    if output_root.exists():
        raise FileExistsError(f"output root already exists: {output_root}")
    config = ProjectConfig.model_validate(
        {
            "schema_version": 1,
            "controller": {"kind": "local"},
            "execution": {"kind": "docker"},
            "harbor": {
                "agent_name": "oracle",
                "attempts": 1,
                "concurrency": 1,
            },
        }
    )
    prepared = prepare_fixture_submission(task_directory, config, run_id=run_id)
    paths = attempt_paths(output_root, run_id, "attempt-local")
    paths.root.mkdir(parents=True)
    paths.context.mkdir()
    paths.jobs.mkdir()
    _materialize(prepared.sealed_context, paths.context)
    labels = {
        RUN_LABEL: run_id,
        ATTEMPT_LABEL: paths.root.name,
        PLAN_LABEL: prepared.request.plan_sha256,
    }
    result = HarborRunner().run(
        prepared.request,
        paths,
        environment_import_path=ENVIRONMENT_IMPORT_PATH,
        labels=labels,
    )
    return LocalFixtureResult(
        run=result,
        invocation_root=paths.root,
        runtime="attached local Docker via Harbor 0.22.0",
    )


def run_local_composition(
    task_directory: Path,
    output_root: Path,
    *,
    run_id: str = "fixture-composition",
) -> LocalCompositionResult:
    """Run real Harbor Docker through ControllerRuntime and in-memory S3/Volume."""
    if output_root.exists():
        raise FileExistsError(f"output root already exists: {output_root}")
    config = ProjectConfig.model_validate(
        {
            "schema_version": 1,
            "controller": {"kind": "local"},
            "execution": {"kind": "docker"},
            "storage": {
                "provider": "aws",
                "bucket": "in-memory-fixture",
                "region": "us-west-2",
            },
            "harbor": {"agent_name": "oracle", "attempts": 1, "concurrency": 1},
        }
    )
    prepared = prepare_fixture_submission(task_directory, config, run_id=run_id)
    store = _MemoryStore(prepared)
    if prepared.plan.storage is None:
        raise RuntimeError("local composition requires resolved storage")
    invocation = ControllerInvocation(
        schema_version=1,
        run_id=run_id,
        request_sha256=store.request_sha256,
        plan_sha256=prepared.request.plan_sha256,
        request_key=store.request_key,
        storage=prepared.plan.storage,
    )
    runner = _CapturingHarborRunner()
    runtime = ControllerRuntime(
        store,
        _MemoryVolume(),
        runner,
        _EmptyObserver(),
        controller_root=output_root,
        attempt_id=lambda: "attempt-local",
    )
    controller = runtime.run(invocation, function_call_id="local-fixture-call")
    state = store.read_run_state(run_id)
    if controller.state != "terminal" or not isinstance(state, TerminalRunState):
        if runner.error is not None:
            raise RuntimeError("local Harbor composition failed") from runner.error
        raise RuntimeError(f"local composition failed: {controller.detail}")
    return LocalCompositionResult(
        controller=controller,
        terminal=state.terminal,
        invocation_root=output_root / f"runs/{run_id}/attempts/attempt-local",
        published_content=dict(store.content),
        runtime="ControllerRuntime with attached Harbor 0.22.0 Docker",
    )
