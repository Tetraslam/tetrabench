"""The tetrabench planning CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Protocol

import typer
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from tetrabench import __version__
from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.catalog import SectionName, get_section, load_catalog, select_tasks
from tetrabench.config import load_project_config
from tetrabench.context import resolve_context
from tetrabench.plan import canonical_model_bytes, plan_digest, resolve_plan
from tetrabench.s3 import create_s3_store

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
)
out = Console()
err = Console(stderr=True)


class _ReadAccessStore(Protocol):
    def check_read_access(self) -> None: ...


def _fail(error: Exception) -> None:
    err.print(f"[red]error:[/red] {error}")
    raise typer.Exit(2)


def _canonical_echo(value: object, *, stderr: bool = False) -> None:
    typer.echo(dumps_canonical_json(value).decode("utf-8"), err=stderr)


def _provider_display(provider: str) -> str:
    return "AWS" if provider == "aws" else "Tigris"


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
                store.check_read_access()
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
            "bucket": storage.bucket,
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
    else:
        out.print("[dim]not attempted[/dim] storage provider checks (offline)")
    out.print("[yellow]unproven[/yellow] storage writes (not attempted)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
