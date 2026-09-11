"""Attached Docker execution of a sealed task selection, with durable identity."""

from __future__ import annotations

import os
import tempfile
import threading
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tetrabench.canonical_json import dumps_canonical_json, sha256_hex
from tetrabench.catalog import SectionName
from tetrabench.context import materialize_sealed_context
from tetrabench.controller_runtime import (
    AttemptPaths,
    credential_free_harbor_environment,
)
from tetrabench.costs import CostSummary
from tetrabench.docker_lifecycle import execution_owner
from tetrabench.harbor import (
    ATTEMPT_LABEL,
    ENVIRONMENT_IMPORT_PATH,
    PLAN_LABEL,
    RUN_LABEL,
)
from tetrabench.harbor_runner import HarborRunner
from tetrabench.harnesses import validate_credentials
from tetrabench.local_control import initialize_owner
from tetrabench.plan import canonical_model_bytes
from tetrabench.rewards import SectionRewardSummary
from tetrabench.run_reference import (
    RunReference,
    RunReferenceStore,
    process_identity,
    write_private_record,
)
from tetrabench.submission import PreparedSubmission, prepare_run


@dataclass(frozen=True, slots=True)
class LocalExecutionResult:
    outcome: Literal["succeeded", "failed", "cancelled"]
    reward: str | None
    summary: SectionRewardSummary
    job_directory: Path
    run_id: str
    output_identity: tuple[int, int]
    costs: CostSummary | None = None


class LocalOutputExistsError(FileExistsError):
    """The requested local output path was already reserved."""


def local_paths(output: Path) -> AttemptPaths:
    return AttemptPaths(
        root=output,
        context=output / "context",
        jobs=output,
        request=output / "request.json",
        child_events=output / "child-events.jsonl",
        controller_plan=output / "controller-plan.json",
        controller_result=output / "controller-result.json",
        failure=output / "failure.json",
    )


def run_local(
    root: Path,
    section: SectionName,
    profile: str | None,
    output_directory: Path,
) -> LocalExecutionResult:
    return run_prepared_local(prepare_run(root, section, profile), output_directory)


def run_prepared_local(
    prepared: PreparedSubmission,
    output_directory: Path,
    *,
    references: RunReferenceStore | None = None,
) -> LocalExecutionResult:
    from tetrabench.preflight import check_runtime

    check_runtime("run")
    if threading.current_thread() is not threading.main_thread():
        raise ValueError(
            "local execution requires the main thread for cancellation ownership"
        )
    request = prepared.request
    validate_credentials(request.plan.harness)
    from tetrabench.runtime_auth import (
        make_credential_context,
        validate_runtime_auth_request,
    )

    validate_runtime_auth_request(request)
    if (
        request.plan.controller.kind != "local"
        or request.plan.execution.kind != "docker"
    ):
        raise ValueError(
            "run requires controller.kind='local' and execution.kind='docker'"
        )
    if not request.plan.runnable or not request.plan.trials:
        raise ValueError("plan is not runnable")
    if (
        request.plan != prepared.plan
        or request.context_manifest != prepared.sealed_context.manifest
    ):
        raise ValueError("prepared local request and sealed context disagree")
    references = references or RunReferenceStore()
    request_sha256 = sha256_hex(canonical_model_bytes(request))
    with references.admission(
        request.run_id, engine="docker", request_sha256=request_sha256, require_new=True
    ):
        pass
    output_directory = output_directory.expanduser().absolute()
    if not output_directory.parent.is_dir():
        raise ValueError(
            f"output parent directory does not exist: {output_directory.parent}"
        )
    if output_directory.parent != output_directory.parent.resolve(strict=True):
        raise ValueError(
            "output parent must not contain symlink ancestors or traversal"
        )
    if output_directory.exists() or output_directory.is_symlink():
        raise LocalOutputExistsError(
            f"output directory already exists: {output_directory}"
        )
    runner = (
        HarborRunner(
            credential_context=make_credential_context(
                engine="docker",
                consumer_id=f"local-{os.getpid()}-{request.run_id}",
                run_id=request.run_id,
            )
        )
        if request.plan.harness is not None and request.plan.harness.auth is not None
        else HarborRunner()
    )
    # Validate the immutable bytes before reserving output, then materialize the
    # same bytes in the retained run directory. Never hand Harbor source paths.
    with tempfile.TemporaryDirectory(prefix="tetrabench-run-") as temporary:
        validation_root = Path(temporary)
        materialize_sealed_context(prepared.sealed_context, validation_root)
        with credential_free_harbor_environment():
            runner.validate_tasks(request, validation_root)
    if output_directory.parent != output_directory.parent.resolve(strict=True):
        raise ValueError(
            "output parent changed to a symlink ancestor during validation"
        )
    with ExitStack() as lifetime:
        # Recheck after validation, and hold the shared receipt lock through
        # reservation and reference publication. Native execution needs only its
        # own lifetime lock, not a long-held namespace lock.
        with references.admission(
            request.run_id,
            engine="docker",
            request_sha256=request_sha256,
            require_new=True,
        ):
            try:
                output_directory.mkdir(mode=0o700)
            except FileExistsError as error:
                raise LocalOutputExistsError(
                    f"output directory already exists: {output_directory}"
                ) from error
            output_directory.chmod(0o700)
            metadata = output_directory.stat()
            output_identity = (metadata.st_dev, metadata.st_ino)
            paths = local_paths(output_directory)
            write_private_record(
                paths.request,
                canonical_model_bytes(request),
                expected_parent=output_identity,
            )
            reference = RunReference(
                run_id=request.run_id,
                engine="docker",
                request_sha256=request_sha256,
                output_directory=str(output_directory),
                output_identity=output_identity,
                process=process_identity(os.getpid()),
            )
            lock_identity = lifetime.enter_context(execution_owner(output_directory))
            initialize_owner(reference, lock_identity)
            references.create(reference)

        def record(state: str) -> None:
            write_private_record(
                output_directory / "execution.json",
                dumps_canonical_json(
                    {
                        "schema_version": 1,
                        "run_id": request.run_id,
                        "request_sha256": request_sha256,
                        "state": state,
                    }
                ),
                expected_parent=output_identity,
            )

        record("running")
        try:
            paths.context.mkdir(mode=0o700)
            materialize_sealed_context(prepared.sealed_context, paths.context)
            labels = {
                RUN_LABEL: request.run_id,
                ATTEMPT_LABEL: paths.root.name,
                PLAN_LABEL: request.plan_sha256,
            }
            with credential_free_harbor_environment():
                result = runner.run(
                    request,
                    paths,
                    environment_import_path=ENVIRONMENT_IMPORT_PATH,
                    labels=labels,
                )
            if result.summary is None:
                raise ValueError(
                    "Harbor runner did not return a canonical reward summary"
                )
            if result.costs is not None:
                job_metadata = result.job_directory.stat()
                write_private_record(
                    result.job_directory / "tetrabench-costs.json",
                    dumps_canonical_json(
                        {
                            "schema_version": 1,
                            "run_id": request.run_id,
                            "request_sha256": request_sha256,
                            "plan_sha256": request.plan_sha256,
                            "costs": result.costs.model_dump(mode="json"),
                        }
                    ),
                    expected_parent=(job_metadata.st_dev, job_metadata.st_ino),
                )
            record("terminal")
            return LocalExecutionResult(
                outcome=result.outcome,
                reward=result.reward,
                summary=result.summary,
                job_directory=result.job_directory,
                run_id=request.run_id,
                output_identity=output_identity,
                costs=result.costs,
            )
        except KeyboardInterrupt:
            with suppress(OSError):
                record("interrupted")
            raise
        except BaseException:
            with suppress(OSError):
                record("failed")
            raise
