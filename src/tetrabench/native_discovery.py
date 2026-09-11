"""Installed-native reasoning inspection, isolated by default and CLI-callable.

Linux PID/network namespaces enforce offline initialization and descendant cleanup.
An unavailable namespace fails closed. Explicit refresh never reads ambient auth;
authenticated collection requires a runtime leased by tetrabench.auth.auth_session.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tetrabench.auth_config import NativeAuthReference
from tetrabench.auth_sessions import AuthError
from tetrabench.canonical_json import sha256_hex
from tetrabench.capabilities import (
    CapabilityIdentity,
    CapabilitySnapshot,
    MetadataError,
    make_evidence,
    metadata_text,
    parse_metadata,
    safe_url,
)
from tetrabench.discovery import (
    NATIVE_VERSIONS,
    attach_catalog,
    catalog_controls,
    discover,
    observation_from_json,
)
from tetrabench.discovery_http import read_public_metadata, refresh_public_metadata
from tetrabench.harness_config import HarnessConfig, ResolvedHarness, SealedResource
from tetrabench.harnesses import native_configuration_layers, seal_harness
from tetrabench.nativeauth import isolated_auth_environment, run_native
from tetrabench.reasoning import harness_config_digest, suggested_snippets
from tetrabench.runtime_startup import (
    validate_runtime_capability as validate_runtime_capability,
)

if TYPE_CHECKING:
    from tetrabench.auth import NativeRuntime


@dataclass(frozen=True)
class NativeInstallation:
    command: tuple[str, ...]
    node: str
    pi_package: Path | None = None


def native_installation(
    harness: str,
    *,
    modules: Path | None = None,
    node: str | None = None,
    executable: Path | None = None,
) -> NativeInstallation:
    """Select installed binaries only. Never install packages or use npx."""
    node = node or shutil.which("node") or "node"
    node = shutil.which(node) or node
    if modules:
        modules = modules.absolute()
        paths = {
            "opencode": "opencode-linux-x64/bin/opencode",
            "codex": "@openai/codex/bin/codex.js",
            "claude-code": "@anthropic-ai/claude-code-linux-x64/claude",
            "pi": "@earendil-works/pi-coding-agent/package.json",
        }
        binary = modules / paths[harness]
        if harness == "pi":
            package = parse_metadata(binary.read_bytes())
            published = binary.parent / package["bin"]["pi"]
            binary = modules / ".bin/pi"
            if binary.resolve() != published.resolve():
                raise MetadataError(
                    "Pi executable differs from the published package bin"
                )
        if not binary.is_file():
            raise MetadataError(
                "selected native package missing; run the native installer"
            )
        command = (node, str(binary)) if harness == "codex" else (str(binary),)
        return NativeInstallation(
            command,
            node,
            modules / "@earendil-works/pi-coding-agent" if harness == "pi" else None,
        )
    name = {"claude-code": "claude"}.get(harness, harness)
    selected = str(executable) if executable else shutil.which(name)
    if not selected:
        raise MetadataError("native executable missing; install the selected exact pin")
    binary = Path(selected).resolve()
    package = None
    if harness == "pi":
        for parent in binary.parents:
            manifest = parent / "package.json"
            if manifest.is_file():
                data = parse_metadata(manifest.read_bytes())
                if data.get("name") == "@earendil-works/pi-coding-agent":
                    package = parent
                    break
        if package is None:
            raise MetadataError("Pi SDK location unavailable; select --native-modules")
    return NativeInstallation((str(binary),), node, package)


def _config_execution(native: Any, config: HarnessConfig | ResolvedHarness) -> bool:
    """Conservative native extension/helper gate, not a vendor allowlist."""
    executable_keys = {
        "plugin",
        "plugins",
        "enabledPlugins",
        "hooks",
        "mcp",
        "mcpServers",
        "mcp_servers",
        "apiKeyHelper",
        "awsAuthRefresh",
        "awsCredentialExport",
        "extensions",
        "experimental",
        "command",
        "exec",
        "initialPrompt",
    }
    if any(
        item.destination.endswith((".js", ".mjs", ".ts", ".sh", ".py"))
        for item in config.resources
    ):
        return True
    stack = [native]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if any(key in executable_keys and child for key, child in value.items()):
                return True
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str) and value.startswith("!"):
            return True
    return False


def _prepared(config: HarnessConfig, base: Path) -> HarnessConfig:
    if getattr(config, "capability_snapshot", None) is not None:
        values = config.model_dump(mode="python")
        values.pop("capability_snapshot", None)
        config = HarnessConfig.model_validate(values)
    resolved = seal_harness(config, base)
    values = resolved.model_dump(mode="python")
    if values.get("native_config"):
        values["native_config"].pop("sha256", None)
    return HarnessConfig.model_validate(values)


def _auth_reference(config: HarnessConfig) -> tuple[str, str | None]:
    auth = config.auth
    if auth is None:
        return "unresolved" if config.env else "none", None
    reference = auth.reference
    if isinstance(reference, NativeAuthReference):
        return auth.mode, reference.profile
    return auth.mode, "env:" + reference.name


def _identity(
    config: HarnessConfig,
    route: dict[str, Any] | None = None,
    *,
    observed: bool = False,
) -> CapabilityIdentity:
    provider, _, model = config.model.partition("/")
    route = route or {}
    endpoints: tuple[str, ...] = ()
    endpoint = route.get("endpoint")
    if isinstance(endpoint, str) and endpoint:
        try:
            endpoints = (safe_url(endpoint),)
        except MetadataError:
            pass
    mode, profile = _auth_reference(config)
    route_data = {
        "provider": route.get("provider", provider),
        "protocol": route.get("protocol", "unknown"),
        "endpoints": list(endpoints),
    }
    return CapabilityIdentity(
        harness=config.name,
        harness_version=config.version,
        native_adapter_version=NATIVE_VERSIONS.get(config.name, ("unknown", "unknown"))[
            1
        ],
        requested_model=config.model,
        resolved_model=route.get("model", model),
        provider_id=route_data["provider"],
        route_id=sha256_hex(metadata_text(route_data).encode()),
        protocol=route_data["protocol"],
        endpoints=endpoints,
        fallback_policy_json=metadata_text(
            route.get("fallback", {"status": "not-disclosed"})
        ),
        auth_mode=mode,
        profile_ref=profile,
        config_digest=harness_config_digest(config),
        route_status="bound"
        if observed
        and route_data["protocol"] != "unknown"
        and route.get("status", "bound") == "bound"
        and (config.name != "codex" or bool(endpoints))
        else "unknown",
    )


def _cache_path(harness: str) -> Path | None:
    home = Path.home()
    return {
        "opencode": Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
        / "opencode/models.json",
        "codex": home / ".codex/models_cache.json",
        "pi": home / ".pi/agent/models-store.json",
    }.get(harness)


def collect_installed(
    config: HarnessConfig,
    *,
    base: Path,
    installation: NativeInstallation | None = None,
    modules: Path | None = None,
    node: str | None = None,
    refresh: bool = False,
    allow_config_execution: bool = False,
    allow_authenticated_read: bool = False,
    runtime: NativeRuntime | None = None,
    native_cache: Path | None = None,
    reuse_native_cache: bool = True,
    verification_policy: Literal["native-startup", "strict-startup"] = "native-startup",
) -> CapabilitySnapshot:
    """Inspect an installed native harness. No JSON capture file or credentials needed.

    Default: isolated cwd/home, explicit sealed config plus copied native model cache,
    a fresh PID/net namespace, and metadata control requests only. Custom hooks and
    plugins require allow_config_execution. Online native initialization additionally
    requires a supplied auth-owner runtime and allow_authenticated_read; refresh alone
    only permits unauthenticated public catalog refresh, never an auth store read.
    """
    if verification_policy not in {"native-startup", "strict-startup"}:
        raise MetadataError("unknown native verification policy")
    prepared = _prepared(config, base)
    identity = _identity(prepared)
    if config.version != NATIVE_VERSIONS.get(config.name, (None, None))[0]:
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                "Install the verified native pin before metadata collection.",
            ),
        )
    if runtime is not None and (
        not allow_authenticated_read
        or runtime.spec != config.auth
        or runtime.harness != config.name
    ):
        raise MetadataError(
            "native discovery runtime requires explicit matching auth approval"
        )
    if allow_authenticated_read and runtime is None:
        raise MetadataError(
            "authenticated metadata read requires an acquired NativeRuntime"
        )
    if runtime is not None and (
        runtime.last_auth_status is None
        or runtime.last_auth_status.mode != runtime.spec.mode
    ):
        raise MetadataError(
            "native authentication mode must be observed before metadata collection"
        )
    layers = native_configuration_layers(prepared)
    native = layers.main_config
    executes = any(_config_execution(layer.config, prepared) for layer in layers.layers)
    if executes and not allow_config_execution:
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                "Config can execute plugins/hooks/helpers. Explicitly enable "
                "allow_config_execution before discovery; no runtime was started.",
            ),
        )
    if executes and runtime is not None:
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                "Executable config cannot guarantee metadata-only online traffic. "
                "Inspect offline or use passive config for authenticated discovery.",
            ),
        )
    if config.name == "pi" and (
        prepared.options.get("extensions") or prepared.options.get("model_api")
    ):
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                "Pi extension/model_api requires effective native models config; "
                "the collector will not silently omit the runtime route transform.",
            ),
        )
    # These controls create sessions or resume prior work during initialization.
    # They are never forwarded by this collector, even with plugin opt-in.
    if prepared.session is not None and (
        prepared.session.resume_trajectory or prepared.session.load_trajectory
    ):
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                "Session-bearing discovery requires separate metadata-only config.",
            ),
        )
    try:
        installation = installation or native_installation(
            config.name, modules=modules, node=node
        )
        unshare = shutil.which("unshare")
        if not unshare:
            raise MetadataError("Linux unshare required for safe native discovery")
        with tempfile.TemporaryDirectory(
            prefix="tetrabench-discovery-", dir=runtime.root if runtime else None
        ) as temporary:
            work = Path(temporary)
            if runtime:
                environment = runtime.environment
            else:
                environment = isolated_auth_environment(
                    config.name,
                    work,
                    base={
                        "PATH": str(Path(installation.node).parent) + ":" + os.defpath,
                    },
                )
            environment = {
                **environment,
                "CI": "true",
                "PI_TELEMETRY": "0",
                "DISABLE_AUTOUPDATER": "1",
            }
            # Permission makes auth available; it does not force any provider GET.
            offline = runtime is None
            prefix = [
                unshare,
                "--user",
                "--map-root-user",
                "--pid",
                "--fork",
                "--kill-child",
            ]
            if offline:
                prefix.append("--net")
            version_argv = [*prefix, *installation.command, "--version"]
            version = (
                runtime.run(version_argv, timeout=15)
                if runtime
                else run_native(
                    version_argv, environment=environment, cwd=work, timeout=15
                )
            )
            output = version.output.decode("utf-8", errors="replace").strip()
            if version.returncode or config.version not in output.split():
                raise MetadataError(
                    "installed native version mismatch or namespace unavailable"
                )
            cache_path = native_cache or (
                _cache_path(config.name)
                if reuse_native_cache and runtime is None
                else None
            )
            staged_cache = None
            cache_hash = None
            refresh_evidence = None
            if cache_path is not None and cache_path.is_file():
                with cache_path.open("rb") as handle:
                    cached = handle.read(16 * 1024 * 1024 + 1)
                if len(cached) > 16 * 1024 * 1024:
                    raise MetadataError("native catalog cache exceeds 16 MiB")
                json.loads(cached)
                staged_cache = work / "catalog-cache.json"
                staged_cache.write_bytes(cached)
                cache_hash = sha256_hex(cached)
            provider, _, model = config.model.partition("/")
            if refresh and runtime is None and config.name in {"opencode", "pi"}:
                url = (
                    "https://models.opencode.ai/api.json"
                    if config.name == "opencode"
                    else ("https://pi.dev/api/models/providers/" + provider)
                )
                body, headers = read_public_metadata(
                    url, refresh=True, full_catalog=True
                )
                public = json.loads(body)
                refresh_evidence = make_evidence(
                    url,
                    body,
                    datetime.now(UTC).isoformat(),
                    "explicit public native catalog refresh; original cache unchanged",
                )
                staged_cache = work / "catalog-cache.json"
                if config.name == "pi":
                    from email.utils import parsedate_to_datetime

                    modified = headers.get("last-modified")
                    entries = (
                        public
                        if isinstance(public, list)
                        else public.get("models", list(public.values()))
                    )
                    public = {
                        provider: {
                            "models": [
                                {**entry, "provider": provider} for entry in entries
                            ],
                            "lastModified": int(
                                parsedate_to_datetime(modified).timestamp() * 1000
                            )
                            if modified
                            else 0,
                        }
                    }
                staged_cache.write_text(
                    metadata_text(public) if config.name == "pi" else body
                )
                cache_hash = sha256_hex(staged_cache.read_bytes())
            request = {
                "harness": config.name,
                "version": config.version,
                "command": list(installation.command),
                "node": installation.node,
                "pi_package": str(installation.pi_package),
                "native": native,
                "provider": provider,
                "model": model,
                "cache": str(staged_cache) if staged_cache else None,
                "work": str(work),
                "network_isolated": offline,
                "authenticated": runtime is not None,
                "auth_mode": runtime.spec.mode if runtime is not None else None,
                "require_auth_custody": runtime is not None
                and runtime.claim is not None,
                "refresh": refresh,
                "allow_config_execution": allow_config_execution,
                "discovery": prepared.discovery or "native",
                "config_directory": None,
            }
            if prepared.resources:
                from tetrabench.resources import (
                    AGENT_RESOURCE_ROOT,
                    materialize_resources,
                    rewrite_resource_references,
                )

                destination = work / "resources"
                resources = [
                    item
                    for item in prepared.resources
                    if isinstance(item, SealedResource)
                ]
                if len(resources) != len(prepared.resources):
                    raise MetadataError("native discovery resources must be sealed")
                materialize_resources(resources, destination)
                if layers.config_directory is not None:
                    request["config_directory"] = layers.config_directory.replace(
                        AGENT_RESOURCE_ROOT, str(destination), 1
                    )
                rewritten = rewrite_resource_references(native, resources)
                request["native"] = json.loads(
                    metadata_text(rewritten).replace(
                        AGENT_RESOURCE_ROOT, str(destination)
                    )
                )
            argv = [*prefix, sys.executable, "-m", "tetrabench.native_discovery_worker"]
            if runtime:
                result = runtime.run(
                    argv, stdin=metadata_text(request).encode(), timeout=35
                )
                if result.returncode and runtime.claim is not None:
                    runtime._ambiguous = True
            else:
                result = run_native(
                    argv,
                    environment=environment,
                    cwd=work,
                    stdin=metadata_text(request).encode(),
                    timeout=35,
                )
            if runtime is not None:
                from tetrabench.discovery_auth import assert_safe_metadata

                assert_safe_metadata(runtime, result.output)
            if result.returncode:
                failure = parse_metadata(result.output)
                raise MetadataError(
                    "native metadata unavailable: "
                    + failure.get("unavailable", "initialization failed")
                )
            captured = parse_metadata(result.output)
            if runtime is not None and runtime.claim is not None:
                custody = captured.get("auth_custody")
                if (
                    not isinstance(custody, dict)
                    or custody.get("schema_version") != 1
                    or type(custody.get("credential_processes")) is not int
                    or not 1 <= custody["credential_processes"] <= 32
                    or type(custody.get("graceful_credential_processes")) is not int
                    or custody["credential_processes"]
                    != custody["graceful_credential_processes"]
                ):
                    runtime._ambiguous = True
                    raise AuthError("native metadata credential completion is unproven")
            if "unavailable" in captured:
                raise MetadataError(
                    "native metadata interface unavailable for selected route"
                )
            identity = _identity(prepared, captured["route"], observed=True)
            observation = observation_from_json(
                identity,
                metadata_text(captured["payload"]),
                observed_at=datetime.now(UTC).isoformat(),
                method=captured["method"],
            )
            snapshot = discover(identity, observation=observation)
            from tetrabench.discovery_auth import authentication_evidence

            metadata = parse_metadata(snapshot.metadata_json)
            metadata["authentication"] = authentication_evidence(runtime)
            metadata["catalog_origin"] = captured.get(
                "catalog_origin", "native-origin-undisclosed"
            )
            if "route_provenance" in captured:
                metadata["route_provenance"] = captured["route_provenance"]
            snapshot = snapshot.model_copy(
                update={"metadata_json": metadata_text(metadata)}
            )
            unknown = []
            if not identity.endpoints:
                unknown.append("endpoint")
            if identity.protocol in {"codex-native", "claude-native"}:
                unknown.append("wire_protocol")
            snapshot = snapshot.model_copy(
                update={
                    "unverified_fields": tuple(unknown),
                    "verification_policy": verification_policy,
                }
            )
            if refresh_evidence is not None:
                snapshot = snapshot.model_copy(
                    update={
                        "evidence": (*snapshot.evidence, refresh_evidence),
                    }
                )
            limitations = [*snapshot.limitations]
            if metadata.get("route_provenance", {}).get("kind") == (
                "native-version-default"
            ):
                limitations.append(
                    "Route uses pinned Codex built-in defaults and observed native "
                    "auth mode, not an observed HTTP endpoint or account capability."
                )
            if offline:
                limitations.append(
                    "External network disabled by Linux network namespace."
                )
            if runtime is None:
                limitations.append(
                    "No authentication read; auth-specific catalog richness unproven."
                )
            else:
                limitations.append(
                    "Declared credentials provided and native auth mode observed; "
                    "provider metadata HTTP fetch and account entitlement are not "
                    "established. Native catalogs may be bundled, cached, or heuristic."
                )
            if identity.route_status == "unknown":
                limitations.append(
                    "Native route endpoint/protocol omitted; choices are descriptive."
                )
            if unknown:
                limitations.append(
                    "Native configuration/metadata support; undisclosed route fields "
                    "remain unverified under the native-startup policy."
                )
            if cache_hash:
                limitations.append("Native cache copy SHA-256: " + cache_hash)
            snapshot = snapshot.model_copy(update={"limitations": tuple(limitations)})
            if runtime is not None:
                assert_safe_metadata(runtime, snapshot.model_dump(mode="json"))
            return snapshot
    except (
        OSError,
        ValueError,
        AuthError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
    ) as error:
        if runtime is not None:
            # Never turn a failed authorized auth/metadata operation into a silent
            # anonymous fallback, or retain upstream secret-bearing diagnostics.
            if runtime.claim is not None:
                runtime._ambiguous = True
            raise AuthError(
                "authenticated metadata collection failed; no native output retained"
            ) from None
        action = (
            str(error)
            if isinstance(error, MetadataError)
            else "native installation unavailable"
        )
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                action,
                "Install the pinned consumer and check namespace support. "
                "No provider credentials were acquired by the collector.",
            ),
        )


def inspect_installed_handler(
    config: HarnessConfig,
    *,
    base: Path,
    public_catalog: str | None = None,
    **options: Any,
) -> dict[str, Any]:
    snapshot = collect_installed(config, base=base, **options)
    refresh = options.get("refresh", False)
    if public_catalog is not None:
        if not refresh:
            raise MetadataError("public catalog requires explicit refresh")
        text = refresh_public_metadata(public_catalog, refresh=True)
        value = parse_metadata(text)
        if "openrouter.ai/" in public_catalog:
            schema = "openrouter"
            rows = value["data"] if isinstance(value["data"], list) else [value["data"]]
            rows = [
                row for row in rows if row.get("id") == snapshot.identity.resolved_model
            ]
        else:
            schema = "models.dev"
            provider, _, model = config.model.partition("/")
            rows = [value.get(provider, {}).get("models", {}).get(model)]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise MetadataError("public catalog model missing or ambiguous")
        row = rows[0]
        evidence = make_evidence(
            public_catalog,
            text,
            datetime.now(UTC).isoformat(),
            "explicit unauthenticated public model metadata GET",
        )
        if snapshot.identity.route_status == "bound":
            snapshot = attach_catalog(
                snapshot,
                metadata_text(row),
                evidence=evidence,
                schema=schema,
                refresh=True,
            )
        else:
            snapshot = CapabilitySnapshot(
                identity=snapshot.identity,
                status="unknown",
                controls=(
                    *snapshot.controls,
                    *catalog_controls(row, evidence, schema=schema),
                ),
                evidence=(*snapshot.evidence, evidence),
                limitations=(
                    *snapshot.limitations,
                    "Descriptive public catalog; native route/binding unresolved.",
                ),
            )
    elif (
        refresh
        and options.get("runtime") is None
        and config.name not in {"opencode", "pi"}
    ):
        snapshot = snapshot.model_copy(
            update={
                "limitations": (
                    *snapshot.limitations,
                    "No public catalog URL selected; no online refresh attempted. "
                    "Use public_catalog or an approved native auth runtime.",
                )
            }
        )
    return {
        "capability": snapshot.model_dump(mode="json"),
        "verification_policy": snapshot.verification_policy,
        "unverified_fields": list(snapshot.unverified_fields),
        "snapshot_sha256": snapshot.digest,
        "suggestions": list(suggested_snippets(snapshot)),
        "inference_validated": False,
    }


def adopt_installed_handler(
    path: Path,
    *,
    control: str,
    select: str | None = None,
    budget: int | None = None,
    write: bool = False,
    accept_normalization: bool = False,
    **options: Any,
) -> dict[str, Any]:
    """Collect metadata and adopt in one command, without a capture input file."""
    import tomllib

    from tetrabench.harnesses import read_config_text
    from tetrabench.reasoning import adopt_handler

    document = read_config_text(path)
    config = HarnessConfig.model_validate(tomllib.loads(document)["harness"])
    report = inspect_installed_handler(config, base=path.parent, **options)
    snapshot = CapabilitySnapshot.model_validate_json(json.dumps(report["capability"]))
    return adopt_handler(
        path,
        snapshot,
        snapshot.identity,
        control=control,
        select=select,
        budget=budget,
        write=write,
        accept_normalization=accept_normalization,
        expected_file_sha256=sha256_hex(document.encode()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Control-only native reasoning discovery"
    )
    parser.add_argument("command", choices=["inspect", "adopt"])
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--native-modules", type=Path)
    parser.add_argument("--node")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--public-catalog")
    parser.add_argument("--allow-config-execution", action="store_true")
    parser.add_argument("--no-native-cache", action="store_true")
    parser.add_argument("--control")
    parser.add_argument("--select")
    parser.add_argument("--budget", type=int)
    parser.add_argument("--accept-normalization", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        import tomllib

        from tetrabench.harnesses import read_config_text

        config = HarnessConfig.model_validate(
            tomllib.loads(read_config_text(args.harness))["harness"]
        )
        options: dict[str, Any] = dict(
            modules=args.native_modules,
            node=args.node,
            public_catalog=args.public_catalog,
            refresh=args.refresh,
            allow_config_execution=args.allow_config_execution,
            reuse_native_cache=not args.no_native_cache,
        )
        if args.command == "adopt":
            if args.control is None:
                raise MetadataError("adoption requires --control")
            result = adopt_installed_handler(
                args.harness,
                control=args.control,
                select=args.select,
                budget=args.budget,
                write=args.write,
                accept_normalization=args.accept_normalization,
                **options,
            )
        else:
            result = inspect_installed_handler(
                config, base=args.harness.parent, **options
            )
        print(json.dumps(result))
    except (ValueError, OSError, KeyError):
        print('{"error":"native discovery configuration invalid"}')
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
