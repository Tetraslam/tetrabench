"""The tetrabench planning CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Protocol

import typer
from botocore.exceptions import BotoCoreError, ClientError
from modal.exception import Error as ModalError
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from tetrabench import __version__
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
from tetrabench.modal_app import (
    controller_deployment_spec,
    deploy_controller,
)
from tetrabench.plan import canonical_model_bytes, plan_digest, resolve_plan
from tetrabench.receipts import ReceiptConflictError, ReceiptStore
from tetrabench.s3 import (
    CoordinationTopology,
    S3CasConflictError,
    UnsafeCoordinationTopologyError,
    create_s3_store,
)
from tetrabench.submission import (
    SubmissionRefusedError,
    SubmissionService,
    prepare_submission,
)

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
)
controller_app = typer.Typer(no_args_is_help=True)
app.add_typer(controller_app, name="controller")
out = Console()
err = Console(stderr=True)


class _ReadAccessStore(Protocol):
    def check_read_access(self) -> CoordinationTopology: ...


def _fail(error: Exception) -> None:
    err.print(f"[red]error:[/red] {error}")
    raise typer.Exit(2)


def _fail_command(error: Exception, *, json_output: bool) -> None:
    if json_output:
        _canonical_echo(
            {"error": str(error), "schema_version": 1},
            stderr=True,
        )
    else:
        err.print(f"[red]error:[/red] {error}")
    raise typer.Exit(2)


def _canonical_echo(value: object, *, stderr: bool = False) -> None:
    typer.echo(dumps_canonical_json(value).decode("utf-8"), err=stderr)


def _provider_display(provider: str) -> str:
    return "AWS" if provider == "aws" else "Tigris"


def _deployment_spec(profile: str | None):
    config = load_project_config(Path.cwd(), profile=profile)
    return controller_deployment_spec(config, profile)


def _modal_client(config, profile: str | None) -> ModalControllerClient:
    spec = controller_deployment_spec(config, profile)
    return ModalControllerClient(
        spec.app_name,
        spec.function_name,
        environment_name=spec.environment_name,
    )


def _storage_error(error: ClientError, *, provider: str, bucket: str) -> str:
    code = str(error.response.get("Error", {}).get("Code", "Unknown"))
    display = _provider_display(provider)
    if code in {"404", "NoSuchBucket", "NotFound"}:
        return f"{display} storage bucket not found: {bucket}"
    if code in {
        "401",
        "403",
        "AccessDenied",
        "ExpiredToken",
        "InvalidAccessKeyId",
        "InvalidToken",
        "SignatureDoesNotMatch",
        "TokenRefreshRequired",
    }:
        return f"{display} storage authentication or authorization failed ({code})"
    return f"{display} storage read check failed ({code})"


def _fail_doctor(error: Exception, *, json_output: bool) -> None:
    message = str(error)
    if json_output:
        _canonical_echo(
            {
                "error": message,
                "mutation_attempted": False,
                "schema_version": 1,
                "storage_writes": "unproven",
            },
            stderr=True,
        )
    else:
        err.print(f"[red]error:[/red] {message}")
        err.print("[yellow]unproven:[/yellow] storage writes; no mutation attempted")
    raise typer.Exit(2)


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
            except ClientError as error:
                raise ValueError(
                    _storage_error(
                        error,
                        provider=storage.provider,
                        bucket=storage.bucket,
                    )
                ) from error
            except BotoCoreError as error:
                display = _provider_display(storage.provider)
                raise ValueError(
                    f"{display} storage credentials or connection failed "
                    f"({type(error).__name__})"
                ) from error
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
        assert storage is not None
        display = _provider_display(storage.provider)
        prefix = f"s3://{storage.bucket}/{storage.prefix}".rstrip("/")
        out.print(f"[green]ok[/green] {display} bucket read access: {storage.bucket}")
        out.print(f"[green]ok[/green] {display} prefix list access: {prefix}")
        assert topology is not None
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
        assert storage is not None and controller_config.kind == "modal"
        service = SubmissionService(
            create_s3_store(storage),
            _modal_client(load_project_config(Path.cwd(), profile=profile), profile),
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
    store = create_s3_store(config.storage)
    controller = _modal_client(config, profile)
    receipts = ReceiptStore()
    spec = controller_deployment_spec(config, profile)
    return RecoveryService(
        store,
        controller,
        ModalChildObserver(
            S3ChildIdentitySource(store),
            environment_name=spec.environment_name,
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
    return StatusService(
        create_s3_store(config.storage),
        ReceiptStore(),
        _modal_client(config, profile),
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
    spec = controller_deployment_spec(config, profile)
    return CancellationService(
        store,
        _modal_client(config, profile),
        ModalChildObserver(
            S3ChildIdentitySource(store),
            environment_name=spec.environment_name,
        ),
    )


@app.command()
def cancel(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """CAS-cancel runs and clean profile-scoped Harbor Modal children."""
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


@app.command()
def runs(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """List local recovery receipts without claiming durable run authority."""
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
