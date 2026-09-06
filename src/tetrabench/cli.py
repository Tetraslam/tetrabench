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
)
from tetrabench.authoring import (
    add_task,
    create_task,
    initialize_project,
    validate_fixture,
)
from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.catalog import get_section, load_catalog, select_tasks
from tetrabench.config import load_project_config
from tetrabench.context import resolve_context
from tetrabench.engines import Engine, get_engine, selected_engine
from tetrabench.engines.modal import (
    legacy_artifact_service as _artifact_service,
)
from tetrabench.engines.modal import (
    legacy_cancellation_service as _cancellation_service,
)
from tetrabench.engines.modal import (
    legacy_recovery_service as _recovery_service,
)
from tetrabench.engines.modal import (
    legacy_result_service as _remote_result_service,
)
from tetrabench.engines.modal import (
    legacy_status_service as _status_service,
)
from tetrabench.engines.modal import (
    wait_for_run,
)
from tetrabench.lifecycle import (
    CancellationConflictError,
    CancellationUnavailableError,
    RecoveryConflictError,
    RecoveryRefusedError,
)
from tetrabench.local_execution import LocalOutputExistsError
from tetrabench.modal_app import (
    controller_deployment_spec,
    deploy_controller,
)
from tetrabench.models import ConfigOverrides, EnginePatch, ProjectConfig
from tetrabench.plan import canonical_model_bytes, plan_digest, resolve_plan
from tetrabench.receipts import ReceiptConflictError, ReceiptStore
from tetrabench.run_reference import RunReferenceStore
from tetrabench.s3 import (
    CoordinationTopology,
    S3CasConflictError,
    S3ConflictError,
    S3IntegrityError,
    UnsafeCoordinationTopologyError,
    create_s3_store,
)
from tetrabench.submission import (
    SubmissionRefusedError,
    prepare_run,
)

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
)
controller_app = typer.Typer(no_args_is_help=True)
artifacts_app = typer.Typer(no_args_is_help=True)
task_app = typer.Typer(no_args_is_help=True)
app.add_typer(controller_app, name="controller")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(task_app, name="task")
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


@app.command("init")
def initialize(
    directory: Path,
    section: Annotated[str, typer.Option(help="Starter section name.")] = "example",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Create a runnable local tetrabench project in a new directory."""
    try:
        created = initialize_project(directory, section)
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    report = {
        "directory": str(created),
        "schema_version": 1,
        "status": "created",
    }
    if json_output:
        _canonical_echo(report)
    else:
        out.print(f"[green]created[/green] {created}")


@task_app.command("new")
def task_new(
    section: str,
    task_id: str,
    project: Annotated[Path, typer.Option(help="Tetrabench project directory.")] = Path(
        "."
    ),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Create an unlisted runnable Harbor task skeleton."""
    try:
        created, fixture = create_task(project, section, task_id)
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    report = {
        "directory": str(created),
        "fixture": fixture,
        "schema_version": 1,
        "status": "created",
    }
    if json_output:
        _canonical_echo(report)
    else:
        out.print(f"[green]created[/green] {fixture}")


@task_app.command("validate")
def task_validate(
    fixture: str,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Validate one complete project-relative Harbor task fixture."""
    try:
        report = validate_fixture(Path.cwd(), fixture)
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(report.as_dict())
    else:
        out.print(
            f"[green]ok[/green] {report.fixture} "
            f"({report.file_count} files, {report.total_bytes} bytes)"
        )


@task_app.command("add")
def task_add(
    section: str,
    task_id: str,
    fixture: str,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Validate and append a local task to the configured catalog."""
    try:
        validation = add_task(Path.cwd(), section, task_id, fixture)
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    report = {
        "fixture": validation.fixture,
        "schema_version": 1,
        "section": section,
        "status": "added",
        "task_id": task_id,
    }
    if json_output:
        _canonical_echo(report)
    else:
        out.print(f"[green]added[/green] {task_id} to {section}")


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
    for name in catalog.sections:
        table.add_row(name, str(len(get_section(catalog, name).tasks)))
    out.print(table)


@app.command()
def plan(
    section: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    engine: Annotated[
        str | None, typer.Option(help="Execution engine; built-ins: docker, modal.")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Resolve a section into a canonical, secret-free plan."""
    try:
        resolved = resolve_plan(
            Path.cwd(), section, profile, overrides=_engine_override(engine)
        )
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
    section: str,
    output: Annotated[
        Path | None, typer.Option(help="New local output directory.")
    ] = None,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    engine: Annotated[
        str | None, typer.Option(help="Execution engine; built-ins: docker, modal.")
    ] = None,
    run_id: Annotated[str | None, typer.Option(help="Explicit safe run ID.")] = None,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait", help="Wait for a result; remote observation is independent."
        ),
    ] = False,
    detach: Annotated[
        bool, typer.Option("--detach", help="Leave execution running remotely.")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Run a sealed selection using the configured engine."""
    output_path = output.expanduser().absolute() if output is not None else None
    selected_run_id = run_id
    remote = False
    adapter: Engine | None = None
    observe = False

    def validate_mode(config: ProjectConfig) -> None:
        nonlocal adapter, observe, remote
        adapter = selected_engine(config)
        observe = adapter.capabilities.validate_launch(
            wait=wait, detach=detach, output=output
        )
        remote = adapter.capabilities.detached

    try:
        overrides = _engine_override(engine)
        prepared = prepare_run(
            Path.cwd(),
            section,
            profile,
            run_id=run_id,
            overrides=overrides,
            validate_config=validate_mode,
        )
        if adapter is None:
            raise ValueError("preparation did not select an engine")
        if prepared.engine_kind is not None and prepared.engine_kind != adapter.kind:
            raise ValueError("engine selection changed while preparing the run")
        selected_run_id = prepared.request.run_id
        if not remote and output_path is None:
            output_path = Path.cwd() / selected_run_id
        if json_output:
            with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink):
                launched = adapter.launch(prepared, output_path)
        else:
            launched = adapter.launch(prepared, output_path)
        if remote and observe:
            reference = RunReferenceStore().read(selected_run_id)
            if reference is None:
                raise ValueError("launched run has no recorded engine binding")
            launched = wait_for_run(adapter, reference)
    except KeyboardInterrupt:
        report = {
            "schema_version": 1,
            "run_id": selected_run_id,
            "status": "observer_detached" if remote else "interrupted",
        }
        if output_path is not None:
            evidence = output_path / "harbor-job"
            report["evidence_path"] = str(
                evidence if evidence.exists() else output_path
            )
        if json_output:
            _canonical_echo(report, stderr=True)
        else:
            err.print(
                "observer detached; remote run was not cancelled"
                if remote
                else "local run interrupted; private evidence retained"
            )
            err.print(f"Run: {selected_run_id}")
        raise typer.Exit(130) from None
    except LocalOutputExistsError as error:
        _fail_command(error, json_output=json_output)
    except (
        BotoCoreError,
        ClientError,
        ModalError,
        OSError,
        RuntimeError,
        ValueError,
        ValidationError,
    ) as error:
        if output_path is not None and output_path.exists():
            _, message = _safe_command_error(error)
            if json_output:
                _canonical_echo(
                    {
                        "error": message,
                        "evidence_path": str(output_path),
                        "schema_version": 1,
                        "run_id": selected_run_id,
                    },
                    stderr=True,
                )
            else:
                err.print(f"[red]error:[/red] {message}")
                err.print(f"[bold]Evidence:[/bold] {output_path}")
            raise typer.Exit(2) from None
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(launched).decode("utf-8"))
    else:
        out.print(f"[bold]Run:[/bold] {selected_run_id}")
        outcome = getattr(launched, "outcome", None)
        out.print(f"[bold]Outcome:[/bold] {outcome or 'submitted'}")
        summary = getattr(launched, "summary", None)
        if summary is not None and summary.policy == "binary":
            out.print(
                f"[bold]Pass rate:[/bold] {summary.aggregate} "
                f"({summary.pass_count}/{summary.sample_count})"
            )
        if output_path is not None:
            out.print(f"[bold]Harbor job:[/bold] {output_path / 'harbor-job'}")
    code = _result_exit(launched)
    if code:
        raise typer.Exit(code)


def _engine_override(engine: str | None) -> ConfigOverrides | None:
    return ConfigOverrides(engine=EnginePatch(kind=engine)) if engine else None


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
        for name in catalog.sections:
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
        report = deploy_controller(spec)
    except (ModalError, OSError, ValueError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(report)
    else:
        out.print(f"[green]deployed[/green] {spec.app_name}")
        out.print(f"[bold]Environment:[/bold] {spec.environment_name}")
        if "wheel_filename" in report:
            out.print(f"[bold]Wheel:[/bold] {report['wheel_filename']}")
        out.print(f"[bold]Wheel SHA-256:[/bold] {report['wheel_sha256']}")


@app.command()
def submit(
    section: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    run_id: Annotated[str | None, typer.Option(help="Explicit safe run ID.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Compatibility alias for run --engine modal --detach."""
    run(
        section,
        profile=profile,
        engine="modal",
        run_id=run_id,
        detach=True,
        json_output=json_output,
    )


def _recorded_operation(
    run_id: str,
    operation: str,
    *,
    json_output: bool,
    output: Path | None = None,
    profile: str | None = None,
    environment_name: str | None = None,
) -> bool:
    """Route a known run without consulting a mutable project or user profile."""
    try:
        try:
            reference = RunReferenceStore().read(run_id)
        except (OSError, ValueError):
            if profile is None:
                raise ValueError(
                    "run routing reference is unreadable; supply an explicit --profile "
                    "for validated remote lookup"
                ) from None
            return False
        if reference is None:
            return False
        if (
            environment_name is not None
            and environment_name != reference.environment_name
        ):
            raise ValueError("--environment cannot override a recorded run binding")
        engine = get_engine(reference.engine)
        if operation in {"recover", "cancel", "artifacts"} and not getattr(
            engine.capabilities, operation
        ):
            raise ValueError(f"{reference.engine} does not support {operation}")
        if operation == "artifacts":
            if output is None:
                raise ValueError("artifact output is required")
            report = engine.artifacts(reference, output)
        else:
            report = getattr(engine, operation)(reference)
    except (
        BotoCoreError,
        ClientError,
        ModalError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        typer.echo(canonical_model_bytes(report).decode("utf-8"))
    else:
        out.print(f"[bold]Run:[/bold] {reference.run_id}")
        for field in (
            "state",
            "outcome",
            "job_directory",
            "output_directory",
            "detail",
            "result_error",
        ):
            value = getattr(report, field, None)
            if value is not None:
                out.print(f"{field}: {value}")
        summary = getattr(report, "summary", None)
        if summary is not None:
            out.print(f"Aggregate: {summary.aggregate}")
    state = getattr(report, "state", None)
    if state == "conflict":
        raise typer.Exit(3)
    if operation == "result":
        code = _result_exit(report)
        if code:
            raise typer.Exit(code)
    if operation in {"recover", "cancel"} and not getattr(
        report, "cleanup_complete", False
    ):
        raise typer.Exit(3)
    return True


@app.command()
def recover(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    environment: Annotated[
        str | None,
        typer.Option(
            help="Original Modal namespace for legacy runs without routing references."
        ),
    ] = None,
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
        if _recorded_operation(
            run_id,
            "recover",
            json_output=json_output,
            profile=profile,
            environment_name=environment,
        ):
            return
        service = (
            _recovery_service(profile, run_id=run_id, environment_name=environment)
            if environment is not None
            else _recovery_service(profile)
        )
        result = service.recover(run_id)
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


@app.command()
def status(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Inspect a run using its recorded engine and execution identity."""
    try:
        if _recorded_operation(
            run_id, "status", json_output=json_output, profile=profile
        ):
            return
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


@app.command()
def cancel(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="User profile name.")] = None,
    environment: Annotated[
        str | None,
        typer.Option(
            help="Original Modal namespace for legacy runs without routing references."
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Cancel without an interactive confirmation."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Request cancellation and verify cleanup of the run's owned children."""
    if json_output and not yes:
        _fail_command(ValueError("cancel --json requires --yes"), json_output=True)
    if not yes and not typer.confirm(f"Cancel run {run_id}?", default=False):
        err.print("cancellation declined; no cloud mutation attempted")
        raise typer.Exit(1)
    try:
        if _recorded_operation(
            run_id,
            "cancel",
            json_output=json_output,
            profile=profile,
            environment_name=environment,
        ):
            return
        service = (
            _cancellation_service(profile, run_id=run_id, environment_name=environment)
            if environment is not None
            else _cancellation_service(profile)
        )
        result = service.cancel(run_id)
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


def _result_exit(report) -> int:
    state = getattr(report, "state", None)
    outcome = getattr(report, "outcome", None)
    if state == "conflict":
        return 3
    if state == "terminal":
        return 1 if outcome in {"failed", "cancelled"} else 0
    if state == "unknown":
        return 4
    if (
        outcome in {"failed", "cancelled"}
        or state in {"failed", "interrupted"}
        or getattr(report, "admission_state", None)
        in {
            "failed",
            "cancelled",
        }
    ):
        return 1
    return 0


@app.command()
def result(
    run_id: str,
    profile: Annotated[
        str | None, typer.Option(help="Legacy remote storage profile name.")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Read validated native or remote results using the recorded run identity."""
    try:
        if _recorded_operation(
            run_id, "result", json_output=json_output, profile=profile
        ):
            return
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
    profile: Annotated[
        str | None, typer.Option(help="Legacy remote storage profile name.")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit canonical JSON to stdout."),
    ] = False,
) -> None:
    """Pull one successful terminal inventory into a new private directory."""
    try:
        if _recorded_operation(
            run_id,
            "artifacts",
            json_output=json_output,
            output=output_dir,
            profile=profile,
        ):
            return
        report = _artifact_service(profile).pull(run_id, output_dir)
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
        references = RunReferenceStore().list()
    except (OSError, ValueError, ValidationError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(
            {
                "receipts": [item.model_dump(mode="json") for item in receipts],
                **(
                    {
                        "references": [
                            item.model_dump(mode="json") for item in references
                        ]
                    }
                    if references
                    else {}
                ),
                "schema_version": 1,
            }
        )
        return
    table = Table("Run", "Local evidence", "Request SHA-256")
    for receipt in receipts:
        evidence = receipt.attempts[-1].transitions[-1].type
        table.add_row(receipt.run_id, evidence, receipt.request_sha256)
    receipt_ids = {receipt.run_id for receipt in receipts}
    for reference in references:
        if reference.run_id not in receipt_ids:
            table.add_row(reference.run_id, reference.engine, reference.request_sha256)
    out.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
