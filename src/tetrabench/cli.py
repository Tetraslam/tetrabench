"""The tetrabench planning CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from tetrabench import __version__
from tetrabench.catalog import SectionName, get_section, load_catalog, select_tasks
from tetrabench.config import load_project_config
from tetrabench.context import resolve_context
from tetrabench.plan import canonical_model_bytes, plan_digest, resolve_plan

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
)
out = Console()
err = Console(stderr=True)


def _fail(error: Exception) -> None:
    err.print(f"[red]error:[/red] {error}")
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
) -> None:
    """Validate local configuration, catalog, and context paths."""
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
    except (ValueError, ValidationError) as error:
        _fail(error)
    out.print("[green]ok[/green] project configuration")
    out.print("[green]ok[/green] catalog and local context paths")
    out.print("[dim]not attempted[/dim] cloud controller checks")
    out.print("[dim]not attempted[/dim] storage provider checks")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
