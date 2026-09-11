"""The tetrabench planning CLI."""

from __future__ import annotations

import os
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated, Literal, Protocol

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
from tetrabench.costs import human_cost_lines
from tetrabench.diagnostics import DiagnosticError, sanitize_error
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
auth_app = typer.Typer(
    no_args_is_help=True,
    help="Manage explicitly selected eval credentials, not your interactive login.",
)
models_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect installed native reasoning metadata and adopt a bound configuration.",
)
app.add_typer(controller_app, name="controller")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(task_app, name="task")
app.add_typer(auth_app, name="auth")
app.add_typer(models_app, name="models")
out = Console()
err = Console(stderr=True)


class _ReadAccessStore(Protocol):
    def check_read_access(self) -> CoordinationTopology: ...


def _auth_command(
    action: Literal["login", "status", "logout", "reseed"],
    harness_path: Path | None,
    authority: Path | None,
    local_filesystem: bool,
    executable: str | None,
    pi_module: Path | None,
    json_output: bool,
    device_auth: bool = True,
    profile: str | None = None,
    auth_config: Path | None = None,
) -> None:
    import shutil
    from dataclasses import asdict

    from platformdirs import user_runtime_path

    from tetrabench import auth as authentication
    from tetrabench.auth_config import NativeAuthReference
    from tetrabench.auth_sessions import AuthError, LocalSessionStore
    from tetrabench.config import load_harness_override

    try:
        if profile is not None:
            if harness_path is not None or authority is not None or local_filesystem:
                raise ValueError(
                    "choose --profile/--auth-config or --harness/--authority"
                )
            if not device_auth:
                raise ValueError(
                    "profile login uses device auth; use --harness for browser-auth"
                )
            from tetrabench.auth_profiles import (
                auth_profile_command,
                load_auth_config_file,
            )

            private = load_auth_config_file(auth_config)
            selected = private.profiles.get(profile)
            if selected is None:
                raise ValueError("private auth profile is not configured")
            binary = executable or shutil.which(
                {"claude-code": "claude", "pi": "node"}.get(
                    selected.harness, selected.harness
                )
            )
            if binary is None and action != "status":
                raise ValueError("native executable missing; supply --executable")
            status = auth_profile_command(
                action,
                name=profile,
                executable=binary or "",
                config_path=auth_config,
                pi_module=pi_module,
            )
            document = asdict(status)
            if json_output:
                _canonical_echo(document)
            else:
                out.print(
                    f"{status.harness}: {status.mode}, {status.state}", markup=False
                )
            return
        if harness_path is None or auth_config is not None:
            raise ValueError(
                "select --harness FILE or --profile NAME [--auth-config FILE]"
            )
        config = load_harness_override(harness_path)
        if config.auth is None:
            raise ValueError(
                "harness.auth must explicitly select the login mode and reference"
            )
        store = None
        if isinstance(config.auth.reference, NativeAuthReference):
            if authority is None or not local_filesystem:
                raise ValueError(
                    "local auth requires --authority DIR --local-filesystem; "
                    "never use a shared mount"
                )
            store = LocalSessionStore(
                authority.expanduser().absolute(),
                binding=config.auth.reference.binding,
                local_filesystem=True,
            )
        elif authority is not None:
            raise ValueError(
                "environment-reference auth does not use an authority directory"
            )
        binary = executable or shutil.which(
            {"claude-code": "claude", "pi": "node"}.get(config.name, config.name)
        )
        if binary is None and not (action == "status" and store is not None):
            raise ValueError(
                "native executable missing; supply --executable for the exact pin"
            )
        options = {
            "store": store,
            "model": config.model,
            "executable": binary or "",
            "runtime_parent": user_runtime_path("tetrabench") / "native-auth",
            "environment": dict(os.environ),
            "artifact_roots": (),  # Auth commands collect no job artifacts.
            "pi_module": pi_module.expanduser().absolute() if pi_module else None,
        }
        handler = getattr(authentication, "auth_" + action)
        if action in {"login", "reseed"}:
            options["device_auth"] = device_auth
        status = handler(config.name, config.auth, **options)
        document = asdict(status)
    except (ValueError, AuthError, OSError, BotoCoreError, ClientError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(document)
    else:
        out.print(f"{status.harness}: {status.mode}, {status.state}", markup=False)
        if status.generation is not None:
            out.print(
                f"generation {status.generation}; server acceptance is unverified",
                markup=False,
            )
        if status.state == "setup_required":
            out.print(
                "Store the native setup token in your secret manager and supply "
                "the declared environment reference. Tetrabench did not save it.",
                markup=False,
            )


@auth_app.command("login")
def auth_login_command(
    harness: Annotated[
        Path | None,
        typer.Option(
            "--harness", help="Run TOML containing an explicit harness.auth reference."
        ),
    ] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    auth_config: Annotated[Path | None, typer.Option("--auth-config")] = None,
    authority: Annotated[
        Path | None,
        typer.Option(
            "--authority", help="Private local native-session authority directory."
        ),
    ] = None,
    local_filesystem: Annotated[
        bool,
        typer.Option(
            "--local-filesystem",
            help="Confirm authority is on a single-machine local filesystem.",
        ),
    ] = False,
    executable: Annotated[str | None, typer.Option("--executable")] = None,
    pi_module: Annotated[
        Path | None,
        typer.Option("--pi-module", help="Pinned Pi dist/index.js for native OAuth."),
    ] = None,
    device_auth: Annotated[bool, typer.Option("--device-auth/--browser-auth")] = True,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Start the selected native login. Browser approval happens in your terminal."""
    _auth_command(
        "login",
        harness,
        authority,
        local_filesystem,
        executable,
        pi_module,
        json_output,
        device_auth,
        profile,
        auth_config,
    )


@auth_app.command("status")
def auth_status_command(
    harness: Annotated[Path | None, typer.Option("--harness")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    auth_config: Annotated[Path | None, typer.Option("--auth-config")] = None,
    authority: Annotated[Path | None, typer.Option("--authority")] = None,
    local_filesystem: Annotated[bool, typer.Option("--local-filesystem")] = False,
    executable: Annotated[str | None, typer.Option("--executable")] = None,
    pi_module: Annotated[Path | None, typer.Option("--pi-module")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read the selected auth state without refreshing an OAuth session."""
    _auth_command(
        "status",
        harness,
        authority,
        local_filesystem,
        executable,
        pi_module,
        json_output,
        profile=profile,
        auth_config=auth_config,
    )


@auth_app.command("logout")
def auth_logout_command(
    harness: Annotated[Path | None, typer.Option("--harness")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    auth_config: Annotated[Path | None, typer.Option("--auth-config")] = None,
    authority: Annotated[Path | None, typer.Option("--authority")] = None,
    local_filesystem: Annotated[bool, typer.Option("--local-filesystem")] = False,
    executable: Annotated[str | None, typer.Option("--executable")] = None,
    pi_module: Annotated[Path | None, typer.Option("--pi-module")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove only this eval login; provider revocation is not implied."""
    if not yes and (
        json_output or not typer.confirm("Remove the selected eval login?")
    ):
        _fail_command(
            ValueError("logout requires confirmation; use --yes"),
            json_output=json_output,
        )
    _auth_command(
        "logout",
        harness,
        authority,
        local_filesystem,
        executable,
        pi_module,
        json_output,
        profile=profile,
        auth_config=auth_config,
    )


@auth_app.command("reseed")
def auth_reseed_command(
    harness: Annotated[
        Path | None,
        typer.Option("--harness", help="Run TOML naming the next auth generation."),
    ] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    auth_config: Annotated[Path | None, typer.Option("--auth-config")] = None,
    authority: Annotated[Path | None, typer.Option("--authority")] = None,
    local_filesystem: Annotated[bool, typer.Option("--local-filesystem")] = False,
    executable: Annotated[str | None, typer.Option("--executable")] = None,
    pi_module: Annotated[Path | None, typer.Option("--pi-module")] = None,
    device_auth: Annotated[bool, typer.Option("--device-auth/--browser-auth")] = True,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Start a fresh native login for the next generation; never copy old tokens."""
    _auth_command(
        "reseed",
        harness,
        authority,
        local_filesystem,
        executable,
        pi_module,
        json_output,
        device_auth,
        profile,
        auth_config,
    )


def _fail(error: Exception) -> None:
    _, message = _safe_command_error(error)
    err.print(f"[red]error:[/red] {message}")
    raise typer.Exit(2)


def _inspection_config(path: Path):
    import tomllib

    from tetrabench.harness_config import HarnessConfig, capability_config
    from tetrabench.harnesses import read_config_text, seal_harness

    text = read_config_text(path)
    values = tomllib.loads(text)
    if set(values) != {"harness"}:
        raise ValueError("model inspection requires a standalone [harness] file")
    fields = dict(values["harness"])
    fields.pop("capability_snapshot", None)
    config = capability_config(
        seal_harness(HarnessConfig.model_validate(fields), path.parent)
    )
    return text, config


@models_app.command("inspect")
def models_inspect_command(
    harness: Annotated[Path, typer.Option("--harness")],
    native_modules: Annotated[Path | None, typer.Option("--native-modules")] = None,
    node: Annotated[str | None, typer.Option("--node")] = None,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
    allow_config_execution: Annotated[
        bool, typer.Option("--allow-config-execution")
    ] = False,
    no_native_cache: Annotated[bool, typer.Option("--no-native-cache")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect installed native metadata without inference or ambient auth reads."""
    from tetrabench.native_discovery import inspect_installed_handler

    try:
        path = harness.expanduser().absolute()
        _, config = _inspection_config(path)
        result = inspect_installed_handler(
            config,
            base=path.parent,
            modules=native_modules,
            node=node,
            refresh=refresh,
            allow_config_execution=allow_config_execution,
            reuse_native_cache=not no_native_cache,
        )
    except (ValueError, OSError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(result)
    else:
        capability = result["capability"]
        out.print(
            f"{config.name} {config.version}: {capability['status']}", markup=False
        )
        for control in capability["controls"]:
            choices = ", ".join(choice["name"] for choice in control["choices"])
            out.print(
                f"  {control['name']}: {control['status']} {choices}", markup=False
            )
        for limitation in capability["limitations"]:
            out.print(f"  {limitation}", markup=False)


@models_app.command("adopt")
def models_adopt_command(
    harness: Annotated[Path, typer.Option("--harness")],
    control: Annotated[str, typer.Option("--control")],
    select: Annotated[str | None, typer.Option("--select")] = None,
    budget: Annotated[int | None, typer.Option("--budget")] = None,
    accept_normalization: Annotated[
        bool, typer.Option("--accept-normalization")
    ] = False,
    write: Annotated[bool, typer.Option("--write")] = False,
    native_modules: Annotated[Path | None, typer.Option("--native-modules")] = None,
    node: Annotated[str | None, typer.Option("--node")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect afresh and preview a sealed adoption; --write applies the shown diff."""
    import difflib

    import tomlkit

    from tetrabench.config import replace_harness_document
    from tetrabench.harness_config import bind_capability_adoption
    from tetrabench.native_discovery import collect_installed

    try:
        path = harness.expanduser().absolute()
        before, source = _inspection_config(path)
        snapshot = collect_installed(
            source, base=path.parent, modules=native_modules, node=node
        )
        selection = {"control": control, "accept_normalization": accept_normalization}
        if select is not None:
            selection["select"] = select
        if budget is not None:
            selection["budget"] = budget
        adopted = bind_capability_adoption(source, snapshot, **selection)
        document = tomlkit.parse(before)
        table = document["harness"]
        table.pop("args", None)
        table["options"] = {
            key: value for key, value in adopted.options.items() if value is not None
        }
        if adopted.native_config:
            table["native_config"] = adopted.native_config.model_dump(exclude_none=True)
        if adopted.resources:
            table["resources"] = [
                resource.model_dump() for resource in adopted.resources
            ]
        evidence = adopted.capability_snapshot
        if evidence is None:
            raise ValueError("adoption produced no capability binding")
        table["capability_snapshot"] = evidence.model_dump(mode="python")
        after = tomlkit.dumps(document)
        # Prove TOML's null-free representation preserves the exact bound config.
        from tetrabench.harness_config import HarnessConfig

        HarnessConfig.model_validate(tomlkit.parse(after)["harness"].unwrap())
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            )
        )
        if write:
            replace_harness_document(path, before.encode(), after)
        result = {
            "schema_version": 1,
            "written": write,
            "diff": diff,
            "snapshot_sha256": snapshot.digest,
            "effective_config_digest": evidence.snapshot().identity.config_digest,
            "inference_validated": False,
        }
    except (ValueError, OSError, KeyError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo(result)
    else:
        out.print(diff, markup=False)
        out.print(
            "Written." if write else "Preview only; use --write to apply.", markup=False
        )


def _safe_command_error(error: Exception) -> tuple[str | None, str]:
    if isinstance(error, DiagnosticError):
        return str(error.as_dict()["error_type"]), str(error)
    if isinstance(error, ValidationError):
        return "configuration_error", "; ".join(
            item["msg"]
            for item in error.errors(
                include_input=False, include_context=False, include_url=False
            )
        )
    if isinstance(error, (BotoCoreError, ClientError, ModalError)):
        return "provider_error", str(sanitize_error(error))
    return None, str(error)


def _fail_command(error: Exception, *, json_output: bool) -> None:
    if isinstance(error, (BotoCoreError, ClientError, ModalError, DiagnosticError)):
        diagnostic = sanitize_error(error)
        if json_output:
            _canonical_echo(diagnostic.as_dict(), stderr=True)
        else:
            err.print(
                f"error: {diagnostic} ({diagnostic.as_dict()['error_type']})",
                markup=False,
            )
        raise typer.Exit(2) from None
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
        if isinstance(error, (BotoCoreError, ClientError, ModalError, DiagnosticError)):
            report.update(sanitize_error(error, operation="doctor").as_dict())
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


@app.command("agents")
def agents(
    name: Annotated[
        str | None, typer.Argument(help="Optional controlled harness name.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    resolve_version: Annotated[
        str | None,
        typer.Option(
            "--resolve-version",
            help="Resolve latest from official package metadata for authoring only.",
        ),
    ] = None,
) -> None:
    """Show supported harnesses, options and the run-level configuration shape."""
    from tetrabench.harnesses import (
        capabilities,
        get_harness,
        resolve_authoring_version,
    )

    try:
        if resolve_version is not None:
            if name is None:
                raise ValueError("version resolution requires a harness name")
            version = resolve_authoring_version(name, resolve_version)
            if json_output:
                _canonical_echo(
                    {
                        "schema_version": 1,
                        "harness": name,
                        "version": version,
                        "evidence": "registry metadata, not execution proof",
                    }
                )
            else:
                out.print(version, markup=False)
            return
        if name is not None:
            get_harness(name)
        entries = [
            entry for entry in capabilities() if name is None or entry["name"] == name
        ]
    except ValueError as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo({"schema_version": 1, "harnesses": entries})
    else:
        for entry in entries:
            out.print(f"{entry['name']} ({entry['package']})", markup=False)
            options = get_harness(str(entry["name"])).options
            out.print(f"  options: {', '.join(options)}", markup=False)
            out.print(
                f"  native config: {entry['native_config']}; {entry['version']}",
                markup=False,
            )
            if name is not None:
                details = entry["option_details"]
                if not isinstance(details, dict):
                    raise TypeError("harness option details must be a mapping")
                for option, detail in details.items():
                    choices = detail.get("choices")
                    choice_text = f" ({', '.join(choices)})" if choices else ""
                    out.print(
                        f"  {option}: {detail['type']}{choice_text}", markup=False
                    )
                out.print(
                    f"  credential variables: {entry['credential_variables']}",
                    markup=False,
                )
        out.print(
            "Use [harness]: name, version, model, options, args, env, "
            "native_config, ancillary_models, resources, discovery, session, auth.",
            markup=False,
        )
        out.print(
            "env accepts only ${VARIABLE} references. "
            "native_config accepts format and path or text.",
            markup=False,
        )
        out.print(
            'ancillary_models = "primary" (default) or "native"; '
            "no universal all-call or billing cap guarantee.",
            markup=False,
        )


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


@app.command("category-create")
def category_create(
    name: str,
    readme: Annotated[
        str,
        typer.Option(help="Existing README path relative to the catalog directory."),
    ],
    project: Annotated[Path, typer.Option()] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add an empty task category without changing any task fixture."""
    from tetrabench.categories import create_category

    try:
        create_category(project, name, readme)
    except (OSError, RuntimeError, ValueError) as error:
        _fail_command(error, json_output=json_output)
    if json_output:
        _canonical_echo({"schema_version": 1, "status": "created", "section": name})
    else:
        out.print(f"Created category {name}", markup=False)


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
    harness: Annotated[
        Path | None,
        typer.Option(help="TOML file containing a run-level [harness] table."),
    ] = None,
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
        if harness is not None:
            from tetrabench.config import load_harness_override

            harness_override = load_harness_override(harness)
            overrides = ConfigOverrides(
                engine=overrides.engine if overrides else None, harness=harness_override
            )
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
        if outcome is not None:
            for line in human_cost_lines(getattr(launched, "costs", None)):
                out.print(line, markup=False)
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
    from tetrabench.preflight import check_runtime

    root = Path.cwd()
    topology: CoordinationTopology | None = None
    try:
        check_runtime("doctor")
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
    from tetrabench.preflight import check_runtime

    try:
        check_runtime("controller_deploy")
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
        if operation == "verify" and not callable(getattr(engine, "verify", None)):
            raise ValueError("explicit artifact verification requires a remote run")
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
    elif operation == "verify":
        from tetrabench.integrity import ArtifactVerificationReport

        if not isinstance(report, ArtifactVerificationReport):
            raise TypeError("artifact verifier returned an invalid report")
        _print_artifact_verification(report)
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
        if operation == "result":
            for line in human_cost_lines(getattr(report, "costs", None)):
                out.print(line, markup=False)
    state = getattr(report, "state", None)
    if state == "conflict":
        raise typer.Exit(3)
    if operation == "verify" and state != "verified":
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
        for line in human_cost_lines(getattr(report, "costs", None)):
            out.print(line, markup=False)
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


def _print_artifact_verification(report) -> None:
    """Identical, untruncated audit evidence for recorded and legacy routing."""
    out.print(f"Run: {report.run_id}", markup=False)
    out.print(
        f"Audit: {report.state}; {report.objects_verified}/{report.objects_total} "
        f"objects verified; {report.bytes_verified}/{report.bytes_total} bytes",
        markup=False,
    )
    out.print(
        f"Missing: {len(report.missing)}; corrupt: {len(report.corrupt)}", markup=False
    )
    for kind, descriptors in (("missing", report.missing), ("corrupt", report.corrupt)):
        for descriptor in descriptors:
            out.print(
                f"  {kind}: key={descriptor.key} size={descriptor.size} "
                f"sha256={descriptor.sha256}",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
    for reason in report.reasons:
        out.print(f"Reason: {reason}", markup=False, highlight=False)


@artifacts_app.command("verify")
def artifacts_verify(
    run_id: str,
    profile: Annotated[str | None, typer.Option(help="Legacy remote profile.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Stream and hash all bound remote inputs/artifacts, without downloading files."""
    from tetrabench.engines.modal import legacy_verification_service

    if _recorded_operation(run_id, "verify", json_output=json_output, profile=profile):
        return
    try:
        report = legacy_verification_service(profile).verify(run_id)
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
        typer.echo(canonical_model_bytes(report).decode())
    else:
        _print_artifact_verification(report)
    if report.state != "verified":
        raise typer.Exit(3)


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
