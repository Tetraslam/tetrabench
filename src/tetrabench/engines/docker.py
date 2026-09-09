"""Local Harbor lifecycle. Native files own results; pidfds pin cancellation."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Literal

from harbor.models.job.result import JobResult

from tetrabench.canonical_json import loads_canonical_json, sha256_hex
from tetrabench.costs import CostSummary, read_local_costs
from tetrabench.docker_lifecycle import (
    cleanup_containers,
    observe_cleanup,
    owner_active,
)
from tetrabench.engines import Capabilities
from tetrabench.harbor import (
    ATTEMPT_LABEL,
    ENVIRONMENT_IMPORT_PATH,
    PLAN_LABEL,
    RUN_LABEL,
)
from tetrabench.harbor_api import Harbor022Api
from tetrabench.harbor_runner import _outcome, compile_harbor_job
from tetrabench.local_control import (
    publish_cancellation,
    read_cancellation,
    read_owner_control,
)
from tetrabench.local_execution import local_paths, run_prepared_local
from tetrabench.models import FrozenRecord, StrictModel
from tetrabench.plan import parse_canonical_model
from tetrabench.receipts import ReceiptStore
from tetrabench.records import RequestRecord
from tetrabench.rewards import SectionRewardSummary, summarize_rewards
from tetrabench.run_reference import (
    RunReference,
    open_process_handle,
    process_identity,
)
from tetrabench.submission import PreparedSubmission


class LocalReport(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: str
    state: str
    outcome: str | None = None
    reward: str | None = None
    summary: SectionRewardSummary | None = None
    job_directory: str
    detail: str = "Local native Harbor evidence."
    cleanup_complete: bool = False
    result_error: str | None = None
    costs: CostSummary | None = None


def _cancel_intent(reference: RunReference):
    return read_cancellation(reference) or read_cancellation(reference, observed=True)


def _output(reference: RunReference) -> Path:
    if reference.output_directory is None:
        raise ValueError("local run reference has no output directory")
    output = Path(reference.output_directory)
    if output != output.resolve(strict=True) or not output.is_dir():
        raise ValueError("local output is missing or replaced")
    metadata = output.stat()
    if (
        reference.output_identity is not None
        and (metadata.st_dev, metadata.st_ino) != reference.output_identity
    ):
        raise ValueError("local output directory identity changed")
    return output


def _request(reference: RunReference) -> RequestRecord:
    data = (_output(reference) / "request.json").read_bytes()
    if sha256_hex(data) != reference.request_sha256:
        raise ValueError("local request digest disagrees with run reference")
    request = parse_canonical_model(data, RequestRecord)
    if request.run_id != reference.run_id or request.plan.execution.kind != "docker":
        raise ValueError("local request identity changed")
    return request


def _state(reference: RunReference) -> str:
    _request(reference)
    try:
        value = loads_canonical_json(
            (_output(reference) / "execution.json").read_bytes()
        )
    except FileNotFoundError:
        return "unknown"
    if (
        not isinstance(value, dict)
        or value.get("run_id") != reference.run_id
        or value.get("request_sha256") != reference.request_sha256
    ):
        raise ValueError("local execution observation identity changed")
    state = value.get("state")
    if state not in {"running", "terminal", "failed", "interrupted"}:
        raise ValueError("invalid local execution observation")
    return str(state)


def _queue_cancellation(reference: RunReference, output: Path) -> None:
    control = read_owner_control(output)
    if control is None or control.reference != reference:
        raise ValueError(
            "active local run has no matching cooperative owner; no signal sent"
        )
    identity = reference.process
    if identity is None:
        raise ValueError("local cancellation requires a process identity")
    try:
        fd = open_process_handle(identity.pid)
    except ProcessLookupError:
        if owner_active(output) is False:
            return
        raise
    try:
        try:
            observed = process_identity(identity.pid)
        except (FileNotFoundError, ProcessLookupError):
            if owner_active(output) is False:
                return
            raise
        if observed != identity or identity.uid != os.geteuid():
            raise ValueError("local process identity changed; no request published")
        if owner_active(output) is True and read_cancellation(reference) is None:
            publish_cancellation(reference)
    finally:
        os.close(fd)


class DockerEngine:
    kind = "docker"
    capabilities = Capabilities(artifacts=False)

    def compile(self, settings: dict[str, object]) -> tuple[dict, dict]:
        StrictModel.model_validate(settings)
        return {"kind": "local"}, {"kind": "docker"}

    def launch(self, prepared: PreparedSubmission, output: Path | None) -> LocalReport:
        output = output or Path.cwd() / prepared.request.run_id
        result = run_prepared_local(prepared, output)
        return LocalReport(
            run_id=result.run_id,
            state="terminal",
            outcome=result.outcome,
            reward=result.reward,
            summary=result.summary,
            costs=result.costs,
            job_directory=str(result.job_directory),
            cleanup_complete=observe_cleanup(output, identity=result.output_identity),
        )

    def status(self, reference: RunReference) -> LocalReport:
        native = self._native_result(reference)
        if native is not None:
            return native
        try:
            state = _state(reference)
        except (OSError, ValueError):
            state = "unknown"
        active = owner_active(_output(reference))
        if active is True:
            state = "cancelling" if _cancel_intent(reference) is not None else "running"
        elif state in {"running", "terminal"}:
            state = "unknown"
        if active is None and state == "unknown":
            try:
                if (
                    reference.process is not None
                    and process_identity(reference.process.pid) == reference.process
                    and _state(reference) == "running"
                ):
                    state = "running"
            except (OSError, ValueError, IndexError):
                pass
        return LocalReport(
            run_id=reference.run_id,
            state=state,
            job_directory=str(_output(reference) / "harbor-job"),
            detail="No validated native terminal result; retained output is private.",
            cleanup_complete=observe_cleanup(
                _output(reference), identity=reference.output_identity
            ),
        )

    def result(self, reference: RunReference) -> LocalReport:
        return self.status(reference)

    def _native_result(self, reference: RunReference) -> LocalReport | None:
        request = _request(reference)
        paths = local_paths(_output(reference))
        job = paths.jobs / "harbor-job"
        try:
            native = JobResult.model_validate_json((job / "result.json").read_bytes())
        except FileNotFoundError:
            return None
        except ValueError:
            if owner_active(paths.root) is True:
                return None
            raise
        if native.finished_at is None:
            return None
        api = Harbor022Api()
        config = compile_harbor_job(
            request,
            paths,
            environment_import_path=ENVIRONMENT_IMPORT_PATH,
            labels={
                RUN_LABEL: request.run_id,
                ATTEMPT_LABEL: paths.root.name,
                PLAN_LABEL: request.plan_sha256,
            },
            api=api,
        )
        try:
            artifacts = api.validate_native_artifacts(job, None, config)
        except (OSError, ValueError):
            if owner_active(paths.root) is True:
                return None
            raise
        summary = summarize_rewards(request.plan, paths.context, artifacts)
        return LocalReport(
            run_id=request.run_id,
            state="terminal",
            outcome=_outcome(artifacts.result),
            reward=summary.aggregate,
            summary=summary,
            costs=read_local_costs(job / "tetrabench-costs.json", request),
            job_directory=str(job),
            cleanup_complete=observe_cleanup(
                paths.root, identity=reference.output_identity
            ),
        )

    def cancel(self, reference: RunReference) -> LocalReport:
        output = _output(reference)
        _request(reference)
        if reference.output_identity is None:
            raise ValueError("local cleanup requires a recorded output identity")
        with ReceiptStore(output).lock("cancel"):
            _output(reference)
            if owner_active(output) is True:
                # Verify the live owner, but never signal it. The owner's loop
                # reads the request and shares one cancellation path with Ctrl-C.
                _queue_cancellation(reference, output)
        for _ in range(50):
            if owner_active(output) is not True:
                break
            time.sleep(0.1)
        with ReceiptStore(output).lock("cancel"):
            _output(reference)
            complete = cleanup_containers(
                output, remove=True, identity=reference.output_identity
            )
            # Grader/native artifacts are not cleanup authorization. Keep their
            # validation failure visible after reclaiming exact-owned children.
            try:
                report = self.status(reference)
            except (OSError, ValueError) as error:
                report = LocalReport(
                    run_id=reference.run_id,
                    state="unknown",
                    job_directory=str(output / "harbor-job"),
                    result_error=(
                        f"{type(error).__name__}: "
                        "native result is invalid or unreadable"
                    ),
                )
            if report.state != "terminal":
                if report.result_error is None:
                    report = report.model_copy(
                        update={"result_error": "native result is unavailable"}
                    )
                try:
                    cancellation = _cancel_intent(reference)
                except (OSError, ValueError):
                    cancellation = None
                    report = report.model_copy(
                        update={
                            "result_error": (
                                f"{report.result_error}; cancellation observation "
                                "is unreadable"
                            )
                        }
                    )
                if cancellation is not None:
                    report = report.model_copy(
                        update={"state": "cancelled" if complete else "cancelling"}
                    )
        return report.model_copy(
            update={
                "cleanup_complete": complete,
                "detail": "Owned Docker containers are gone."
                if complete
                else "Cancellation or cleanup is unproven; no signal was sent.",
            }
        )

    def recover(self, reference: RunReference) -> LocalReport:
        raise ValueError(
            "Docker recovery is unsupported; inspect output and start a new run"
        )

    def artifacts(self, reference: RunReference, output: Path) -> LocalReport:
        raise ValueError(
            "Docker artifacts are retained in the recorded local output directory"
        )
