"""The tetrabench planning CLI."""

from __future__ import annotations

import os
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated, Protocol

import typer
from botocore.exceptions import BotoCoreError, ClientError
from modal.exception import Error as ModalError
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from tetrabench import __version__
from tetrabench.artifacts import (
    ArtifactDestinationExistsError,
    ArtifactPullRefusedError,
    ArtifactPullService,
)
from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.catalog import SectionName, get_section, load_catalog, select_tasks
from tetrabench.config import load_project_config
from tetrabench.context import resolve_context
from tetrabench.controller import ModalControllerClient
from tetrabench.harbor import ModalChildObserver, S3ChildIdentitySource
from tetrabench.lifecycle import (
    CancellationConflictError,
    CancellationService,
    CancellationUnavailableError,
    RecoveryConflictError,
    RecoveryRefusedError,
    RecoveryService,
    StatusService,
)
from tetrabench.local_execution import LocalOutputExistsError, run_local
from tetrabench.modal_app import (
    controller_deployment_spec,
    deploy_controller,
)
from tetrabench.plan import canonical_model_bytes, plan_digest, resolve_plan
from tetrabench.receipts import ReceiptConflictError, ReceiptStore
from tetrabench.remote import RemoteResult, RemoteResultService
from tetrabench.s3 import (
    CoordinationTopology,
    S3CasConflictError,
    S3ConflictError,
    S3IntegrityError,
    UnsafeCoordinationTopologyError,
    create_s3_store,
)
from tetrabench.submission import (
    ControllerLaunchConfiguration,
    SubmissionRefusedError,
    SubmissionService,
    prepare_submission,
    resolve_controller_launch,
)

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
)
controller_app = typer.Typer(no_args_is_help=True)
artifacts_app = typer.Typer(no_args_is_help=True)
app.add_typer(controller_app, name="controller")
app.add_typer(artifacts_app, name="artifacts")
out = Console()
err = Console(stderr=True)


class _ReadAccessStore(Protocol):
    def check_read_access(self) -> CoordinationTopology: ...


def _fail(error: Exception) -> None:
    err.print(f"[red]error:[/red] {error}")
    raise typer.Exit(2)


def _safe_command_error(error: Exception) -> tuple[str | None, str]:
    if isinstance(error, (BotoCoreError, ClientError, ModalError)):
        return "provider_error", "provider request failed"
    return None, str(error)


def _fail_command(error: Exception, *, json_output: bool) -> None:
    error_type, message = _safe_command_error(error)
    if json_output:
        report = {"error": message, "schema_version": 1}
        if error_type is not None:
            report["error_type"] = error_type
        _canonical_echo(report, stderr=True)
    else:
        suffix = f" ({error_type})" if error_type is not None else ""
        err.print(f"[red]error:[/red] {message}{suffix}")
    raise typer.Exit(2) from None


def _canonical_echo(value: object, *, stderr: bool = False) -> None:
    typer.echo(dumps_canonical_json(value).decode("utf-8"), err=stderr)


def _provider_display(provider: str) -> str:
    return "AWS" if provider == "aws" else "Tigris"


def _deployment_spec(profile: str | None):
    config = load_project_config(Path.cwd(), profile=profile)
    return controller_deployment_spec(config, profile)


def _modal_client(
    launch: ControllerLaunchConfiguration,
) -> ModalControllerClient:
    return ModalControllerClient(
        launch.app_name,
        launch.function_name,
        environment_name=launch.environment_name,
    )


def _fail_doctor(error: Exception, *, json_output: bool) -> None:
    error_type, message = _safe_command_error(error)
    if json_output:
        report = {
            "error": message,
            "mutation_attempted": False,
            "schema_version": 1,
            "storage_writes": "unproven",
        }
        if error_type is not None:
            report["error_type"] = error_type
        _canonical_echo(report, stderr=True)
    else:
        suffix = f" ({error_type})" if error_type is not None else ""
        err.print(f"[red]error:[/red] {message}{suffix}")
        err.print("[yellow]unproven:[/yellow] storage writes; no mutation attempted")
    raise typer.Exit(2) from None


@app.callback()
def callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed tetrabench version.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def sections() -> None:
    """List local catalog sections and task counts."""
    root = Path.cwd()
    try:
        config = load_project_config(root)
        catalog = load_catalog(root, config.catalog_path)
    except (ValueError, ValidationError) as error:
        _fail(error)
    table = Table("Section", "Tasks")
    for name in ("systems-design", "github-workflow"):
        table.add_row(name, str(len(get_section(catalog, name).tasks)))
    out.print(table)


@app.command()
def plan(
    section: SectionName,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Resolve a section into a canonical, secret-free plan."""
    try:
        resolved = resolve_plan(Path.cwd(), section, profile)
    except (ValueError, ValidationError) as error:
        _fail(error)
    if json_output:
        typer.echo(canonical_model_bytes(resolved).decode("utf-8"))
        return
    out.print(f"[bold]Section:[/bold] {resolved.section}")
    out.print(f"[bold]Trials:[/bold] {len(resolved.trials)}")
    out.print(f"[bold]Plan SHA-256:[/bold] {plan_digest(resolved)}")
    if not resolved.runnable:
        reasons = "; ".join(resolved.not_runnable_reasons)
        out.print(f"[yellow]Not runnable:[/yellow] {reasons}")


@app.command()
def run(
    section: SectionName,
    profile: Annotated[str, typer.Option(help="Local Docker profile name.")],
    output: Annotated[Path, typer.Option(help="New output directory.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Run selected catalog tasks attached through local Docker."""
    output_path = output.expanduser().absolute()
    try:
        if json_output:
            with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink):
                result = run_local(Path.cwd(), section, profile, output_path)
        else:
            result = run_local(Path.cwd(), section, profile, output_path)
    except KeyboardInterrupt:
        evidence_path = output_path / "harbor-job"
        if not evidence_path.exists():
            evidence_path = output_path
        if json_output:
            _canonical_echo(
                {
                    "evidence_path": str(evidence_path),
                    "schema_version": 1,
                    "status": "interrupted",
                },
                stderr=True,
            )
        else:
            err.print("[yellow]interrupted[/yellow] local Harbor execution")
            if output_path.exists():
                err.print(f"[bold]Evidence:[/bold] {evidence_path}")
        raise typer.Exit(130) from None
    except LocalOutputExistsError as error:
        _fail_command(error, json_output=json_output)
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        if output_path.exists():
            evidence_path = output_path / "harbor-job"
            if not evidence_path.exists():
                evidence_path = output_path
            if json_output:
                _canonical_echo(
                    {
                        "error": str(error),
                        "evidence_path": str(evidence_path),
                        "schema_version": 1,
                    },
                    stderr=True,
                )
            else:
                err.print(f"[red]error:[/red] {error}")
                err.print(f"[bold]Evidence:[/bold] {evidence_path}")
            raise typer.Exit(2) from None
        _fail_command(error, json_output=json_output)

    report = {
        "job_directory": str(result.job_directory),
        "outcome": result.outcome,
        "reward": result.reward,
        "schema_version": 1,
        "summary": result.summary.model_dump(mode="json"),
    }
    if json_output:
        _canonical_echo(report)
    else:
        out.print(f"[bold]Outcome:[/bold] {result.outcome}")
        if result.summary.policy == "binary":
            out.print(
                f"[bold]Pass rate:[/bold] {result.summary.aggregate} "
                f"({result.summary.pass_count}/{result.summary.sample_count})"
            )
        else:
            out.print(f"[bold]Reward:[/bold] {result.reward or 'unavailable'}")
        task_table = Table("Task", "Samples", "Passed", "Aggregate")
        for task in result.summary.tasks:
            task_table.add_row(
                task.task_id,
                str(task.sample_count),
                str(task.pass_count) if task.pass_count is not None else "-",
                task.aggregate or "unavailable",
            )
        out.print(task_table)
        out.print(f"[bold]Harbor job:[/bold] {result.job_directory}")
    if result.outcome != "succeeded":
        raise typer.Exit(1)


@app.command()
def doctor(
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    online: Annotated[
        bool,
        typer.Option(help="Check read-only access to the selected storage profile."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Validate local inputs and optionally check read-only storage access."""
    root = Path.cwd()
    topology: CoordinationTopology | None = None
    try:
        config = load_project_config(root, profile=profile)
        catalog = load_catalog(root, config.catalog_path)
        resolve_context(root, config.context)
        configured_catalog_path = Path(config.catalog_path)
        catalog_path = (
            configured_catalog_path
            if configured_catalog_path.is_absolute()
            else root / configured_catalog_path
        )
        for name in ("systems-design", "github-workflow"):
            section = get_section(catalog, name)
            select_tasks(section, config.selection)
            readme = Path(section.readme)
            readme_path = (
                readme if readme.is_absolute() else catalog_path.parent / readme
            )
            if not readme_path.is_file():
                raise ValueError(f"catalog README does not exist: {readme_path}")
        storage = config.storage
        if online:
            if storage is None:
                raise ValueError(
                    "online storage checks require a storage configuration"
                )
            try:
                store: _ReadAccessStore = create_s3_store(storage)
                topology = store.check_read_access()
            except (BotoCoreError, ClientError) as error:
                _fail_doctor(error, json_output=json_output)
    except (ValueError, ValidationError) as error:
        _fail_doctor(error, json_output=json_output)

    storage_report: dict[str, object] | None = None
    if storage is not None:
        storage_report = {
            "admission_safe": topology.admission_safe if topology is not None else None,
            "bucket": storage.bucket,
            "bucket_location": (
                topology.bucket_location if topology is not None else None
            ),
            "location_type": topology.location_type if topology is not None else None,
            "prefix": storage.prefix,
            "provider": storage.provider,
            "provider_display": _provider_display(storage.provider),
        }
    if json_output:
        storage_status = "ok" if online else "not_attempted"
        _canonical_echo(
            {
                "checks": [
                    {"name": "project_configuration", "status": "ok"},
                    {"name": "catalog_and_local_context", "status": "ok"},
                    {"name": "cloud_controller", "status": "not_attempted"},
                    {"name": "storage_bucket", "status": storage_status},
                    {"name": "storage_prefix", "status": storage_status},
                    {
                        "name": "admission_coordination",
                        "status": (
                            "ok"
                            if topology is not None and topology.admission_safe
                            else ("unsafe" if topology is not None else "not_attempted")
                        ),
                    },
                    {"name": "storage_writes", "status": "unproven"},
                ],
                "mode": "online" if online else "offline",
                "profile": profile,
                "schema_version": 1,
                "storage": storage_report,
            }
        )
        return
    out.print("[green]ok[/green] project configuration")
    out.print("[green]ok[/green] catalog and local context paths")
    out.print("[dim]not attempted[/dim] cloud controller checks")
    if online:
        if storage is None:
            raise RuntimeError("online doctor completed without storage configuration")
        display = _provider_display(storage.provider)
        prefix = f"s3://{storage.bucket}/{storage.prefix}".rstrip("/")
        out.print(f"[green]ok[/green] {display} bucket read access: {storage.bucket}")
        out.print(f"[green]ok[/green] {display} prefix list access: {prefix}")
        if topology is None:
            raise RuntimeError("online doctor completed without bucket topology")
        out.print(
            f"[green]ok[/green] {display} bucket location: "
            f"{topology.bucket_location} ({topology.location_type})"
        )
        if topology.admission_safe:
            out.print("[green]safe[/green] mutable admission coordination")
        else:
            out.print(
                "[yellow]unsafe[/yellow] mutable admission coordination: "
                f"{topology.detail}"
            )
    else:
        out.print("[dim]not attempted[/dim] storage provider checks (offline)")
    out.print("[yellow]unproven[/yellow] storage writes (not attempted)")


@controller_app.command("info")
def controller_info(
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Show the exact profile-specific controller deployment contract."""
    try:
        spec = _deployment_spec(profile)
    except (ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(spec.as_dict())
        return
    out.print(f"[bold]App:[/bold] {spec.app_name}")
    out.print(f"[bold]Function:[/bold] {spec.function_name}")
    out.print(f"[bold]Environment:[/bold] {spec.environment_name}")
    out.print(f"[bold]Volume:[/bold] {spec.volume_name}")
    out.print(f"[bold]Secret:[/bold] {spec.secret_name}")
    out.print(f"[bold]Controller root:[/bold] {spec.controller_root}")


@controller_app.command("deploy")
def controller_deploy(
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Deploy without an interactive confirmation."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Deploy the selected profile's named controller resources and Function."""
    try:
        spec = _deployment_spec(profile)
    except (ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    if not yes:
        if json_output:
            _fail_command(
                ValueError("controller deploy --json requires --yes"),
                json_output=True,
            )
        controller_info(profile=profile, json_output=False)
        if not typer.confirm("Deploy these Modal resources?", default=False):
            err.print("deployment cancelled; no cloud mutation attempted")
            raise typer.Exit(1)
    try:
        deploy_controller(spec)
    except (ModalError, OSError, ValueError) as error:
        _fail_command(error, json_output=json_output)
    report = spec.as_dict() | {"deployed": True}
    if json_output:
        _canonical_echo(report)
    else:
        out.print(f"[green]deployed[/green] {spec.app_name}")
        out.print(f"[bold]Environment:[/bold] {spec.environment_name}")


@app.command()
def submit(
    section: SectionName,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    run_id: Annotated[str | None, typer.Option(help="Explicit safe run ID.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Publish a runnable request and spawn its deployed Modal controller."""
    try:
        prepared = prepare_submission(
            Path.cwd(),
            section,
            profile,
            run_id=run_id,
        )
        storage = prepared.plan.storage
        controller_config = prepared.plan.controller
        launch = prepared.controller_launch
        if storage is None or controller_config.kind != "modal" or launch is None:
            raise SubmissionRefusedError(
                "cloud submission requires resolved storage and a Modal controller"
            )
        service = SubmissionService(
            create_s3_store(storage),
            _modal_client(launch),
            ReceiptStore(),
        )
        receipt = service.submit(prepared)
    except (
        BotoCoreError,
        ClientError,
        ModalError,
        OSError,
        ReceiptConflictError,
        S3CasConflictError,
        SubmissionRefusedError,
        UnsafeCoordinationTopologyError,
        ValueError,
        ValidationError,
    ) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(receipt).decode("utf-8"))
        return
    call_id = receipt.attempts[-1].controller_calls[-1].call_id
    out.print(f"[green]submitted[/green] {receipt.run_id}")
    out.print(f"[bold]Request SHA-256:[/bold] {receipt.request_sha256}")
    out.print(f"[bold]Modal call:[/bold] {call_id}")


def _recovery_service(profile: str | None) -> RecoveryService:
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("recover requires storage configuration")
    if config.controller.kind != "modal":
        raise ValueError("recover currently supports the Modal controller only")
    launch = resolve_controller_launch(config, profile)
    if launch is None:
        raise ValueError("recover requires Modal execution")
    store = create_s3_store(config.storage)
    controller = _modal_client(launch)
    receipts = ReceiptStore()
    return RecoveryService(
        store,
        controller,
        ModalChildObserver(
            S3ChildIdentitySource(store),
            environment_name=launch.environment_name,
        ),
        SubmissionService(store, controller, receipts),
    )


@app.command()
def recover(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm detached-controller recovery."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Recover a terminal detached controller after stale-child cleanup."""
    if json_output and not yes:
        _fail_command(
            ValueError("recover --json requires --yes"),
            json_output=True,
        )
    if not yes:
        out.print(
            "Recovery may CAS durable admission, terminate stale Harbor children, "
            "and spawn a Modal controller call. Concurrent recovery may spawn another "
            "call, but admission CAS selects one owner."
        )
        if not typer.confirm(f"Recover run {run_id}?", default=False):
            err.print("recovery cancelled; no cloud mutation attempted")
            raise typer.Exit(1)
    try:
        result = _recovery_service(profile).recover(run_id)
    except (
        BotoCoreError,
        ClientError,
        ModalError,
        OSError,
        ReceiptConflictError,
        RecoveryConflictError,
        RecoveryRefusedError,
        S3CasConflictError,
        SubmissionRefusedError,
        UnsafeCoordinationTopologyError,
        ValueError,
        ValidationError,
    ) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(result).decode("utf-8"))
    else:
        out.print(f"[bold]Recovery:[/bold] {result.state}")
        out.print(f"[bold]Run:[/bold] {result.run_id}")
        out.print(result.detail)
        if result.successor_function_call_id is not None:
            out.print(f"[bold]Modal call:[/bold] {result.successor_function_call_id}")
    if not result.cleanup_complete:
        raise typer.Exit(3)


def _status_service(profile: str | None) -> StatusService:
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("status requires storage configuration")
    if config.controller.kind != "modal":
        raise ValueError("status currently supports the Modal controller only")
    launch = resolve_controller_launch(config, profile)
    if launch is None:
        raise ValueError("status requires Modal execution")
    return StatusService(
        create_s3_store(config.storage),
        ReceiptStore(),
        _modal_client(launch),
    )


@app.command()
def status(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Combine durable S3 state with local and Modal execution evidence."""
    try:
        report = _status_service(profile).status(run_id)
    except (
        BotoCoreError,
        ClientError,
        ModalError,
        OSError,
        ValueError,
        ValidationError,
    ) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(report).decode("utf-8"))
    else:
        out.print(f"[bold]Run:[/bold] {report.run_id}")
        out.print(f"[bold]State:[/bold] {report.state}")
        if report.outcome is not None:
            out.print(f"[bold]Outcome:[/bold] {report.outcome}")
        out.print(report.detail)
    if report.state == "conflict":
        raise typer.Exit(3)


def _cancellation_service(profile: str | None) -> CancellationService:
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("cancel requires storage configuration")
    if config.controller.kind != "modal":
        raise ValueError("cancel currently supports the Modal controller only")
    store = create_s3_store(config.storage)
    launch = resolve_controller_launch(config, profile)
    if launch is None:
        raise ValueError("cancel requires Modal execution")
    return CancellationService(
        store,
        _modal_client(launch),
        ModalChildObserver(
            S3ChildIdentitySource(store),
            environment_name=launch.environment_name,
        ),
    )


@app.command()
def cancel(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Cancel without an interactive confirmation."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """CAS-cancel runs and clean profile-scoped Harbor Modal children."""
    if json_output and not yes:
        _fail_command(ValueError("cancel --json requires --yes"), json_output=True)
    if not yes and not typer.confirm(f"Cancel run {run_id}?", default=False):
        err.print("cancellation declined; no cloud mutation attempted")
        raise typer.Exit(1)
    try:
        result = _cancellation_service(profile).cancel(run_id)
    except (
        BotoCoreError,
        ClientError,
        ModalError,
        OSError,
        ReceiptConflictError,
        S3CasConflictError,
        CancellationConflictError,
        CancellationUnavailableError,
        SubmissionRefusedError,
        UnsafeCoordinationTopologyError,
        ValueError,
    ) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(result).decode("utf-8"))
    else:
        out.print(f"[bold]Cancellation:[/bold] {result.state}")
        out.print(f"[bold]Run:[/bold] {result.run_id}")
    if not result.cleanup_complete:
        raise typer.Exit(3)


def _remote_result_service(profile: str | None) -> RemoteResultService:
    config = load_project_config(Path.cwd(), profile=profile)
    if config.storage is None:
        raise ValueError("remote reads require storage configuration")
    return RemoteResultService(create_s3_store(config.storage))


def _result_exit(report: RemoteResult) -> int:
    if report.state == "conflict":
        return 3
    if report.state == "unknown":
        return 4
    if report.outcome in {"failed", "cancelled"} or report.admission_state in {
        "failed",
        "cancelled",
    }:
        return 1
    return 0


@app.command()
def result(
    run_id: str,
    profile: Annotated[str, typer.Option(help="Remote storage profile name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Read one authoritative remote run result without local receipts."""
    try:
        report = _remote_result_service(profile).result(run_id)
    except (
        BotoCoreError,
        ClientError,
        OSError,
        S3ConflictError,
        S3IntegrityError,
        ValueError,
        ValidationError,
    ) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(report).decode("utf-8"))
    else:
        out.print(f"[bold]Run:[/bold] {report.run_id}")
        out.print(f"[bold]State:[/bold] {report.state}")
        if report.admission_state is not None:
            out.print(f"[bold]Admission:[/bold] {report.admission_state}")
        if report.outcome is not None:
            out.print(f"[bold]Outcome:[/bold] {report.outcome}")
            if report.summary is not None and report.summary.policy == "binary":
                out.print(
                    f"[bold]Pass rate:[/bold] {report.summary.aggregate} "
                    f"({report.summary.pass_count}/{report.summary.sample_count})"
                )
            else:
                out.print(f"[bold]Reward:[/bold] {report.reward or 'unavailable'}")
            if report.summary_status == "legacy_unavailable":
                out.print("[bold]Summary:[/bold] unavailable (legacy)")
            if report.summary is not None:
                task_table = Table("Task", "Samples", "Passed", "Aggregate")
                for task in report.summary.tasks:
                    task_table.add_row(
                        task.task_id,
                        str(task.sample_count),
                        str(task.pass_count) if task.pass_count is not None else "-",
                        task.aggregate or "unavailable",
                    )
                out.print(task_table)
        if report.artifacts:
            table = Table("Artifact", "Bytes", "SHA-256", "Media type")
            for artifact in report.artifacts:
                table.add_row(
                    artifact.logical_path,
                    str(artifact.size),
                    artifact.sha256,
                    artifact.media_type,
                )
            out.print(table)
        for reason in report.reasons:
            err.print(f"[red]conflict:[/red] {reason}")
    exit_code = _result_exit(report)
    if exit_code:
        raise typer.Exit(exit_code)


@artifacts_app.command("pull")
def artifacts_pull(
    run_id: str,
    output_dir: Path,
    profile: Annotated[str, typer.Option(help="Remote storage profile name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Pull one successful terminal inventory into a new private directory."""
    try:
        config = load_project_config(Path.cwd(), profile=profile)
        if config.storage is None:
            raise ValueError("artifact pull requires storage configuration")
        report = ArtifactPullService(create_s3_store(config.storage)).pull(
            run_id, output_dir
        )
    except (
        ArtifactDestinationExistsError,
        ArtifactPullRefusedError,
        BotoCoreError,
        ClientError,
        OSError,
        S3IntegrityError,
        ValueError,
        ValidationError,
    ) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(report).decode("utf-8"))
    else:
        out.print(f"[green]pulled[/green] {len(report.artifacts)} artifacts")
        out.print(f"[bold]Run:[/bold] {report.run_id}")
        out.print(f"[bold]Output:[/bold] {report.output_directory}")


@app.command()
def runs(
    remote: Annotated[
        bool,
        typer.Option("--remote", help="List authoritative remote run records."),
    ] = False,
    profile: Annotated[
        str | None, typer.Option(help="Remote storage profile name.")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """List local receipts, or authoritative remote records with --remote."""
    if remote:
        if profile is None:
            _fail_command(
                ValueError("runs --remote requires --profile"),
                json_output=json_output,
            )
        try:
            report = _remote_result_service(profile).runs()
        except (
            BotoCoreError,
            ClientError,
            OSError,
            S3IntegrityError,
            ValueError,
            ValidationError,
        ) as error:
            _fail_command(error, json_output=json_output)
        if json_output:
            typer.echo(canonical_model_bytes(report).decode("utf-8"))
        else:
            table = Table("Run", "State", "Admission", "Outcome", "Aggregate")
            for item in report.runs:
                if item.summary is not None and item.summary.policy == "binary":
                    aggregate = (
                        f"Pass rate {item.summary.aggregate} "
                        f"({item.summary.pass_count}/{item.summary.sample_count})"
                    )
                else:
                    aggregate = (
                        f"Reward {item.reward}" if item.reward is not None else "-"
                    )
                table.add_row(
                    item.run_id,
                    item.state,
                    item.admission_state or "-",
                    item.outcome or "-",
                    aggregate,
                )
            out.print(table)
            for item in report.malformed_keys:
                err.print(f"[red]malformed key:[/red] {item.key}: {item.reason}")
        if report.malformed_keys or any(
            item.state == "conflict" for item in report.runs
        ):
            raise typer.Exit(3)
        return
    try:
        receipts = ReceiptStore().list()
    except (OSError, ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(
            {
                "receipts": [item.model_dump(mode="json") for item in receipts],
                "schema_version": 1,
            }
        )
        return
    table = Table("Run", "Local evidence", "Request SHA-256")
    for receipt in receipts:
        evidence = receipt.attempts[-1].transitions[-1].type
        table.add_row(receipt.run_id, evidence, receipt.request_sha256)
    out.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
