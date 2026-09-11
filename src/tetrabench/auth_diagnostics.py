"""Reference-only onboarding checks; online metadata is separately authorized."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tetrabench.auth_config import (
    EnvAuthReference,
    NativeAuthReference,
    authentication_environment_names,
)
from tetrabench.harness_config import HarnessConfig

if TYPE_CHECKING:
    from tetrabench.modal_app import ControllerDeploymentSpec


def _check(status: str, source: str, action: str) -> dict[str, Any]:
    return {"status": status, "source": source, "action": action}


def check_native_prerequisites(
    harness: str,
    version: str,
    *,
    purpose: Literal["login", "metadata"] = "login",
    executable: str | None = None,
    modules: Path | None = None,
    node: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Probe installed versions offline; never install, login or read native auth.

    Call before login or metadata collection, not as an API-key eval prerequisite.
    Failure text/output is discarded; only an entire recognized version response
    is returned. Network and PID namespaces fail closed, as in native discovery.
    """
    from tetrabench.harnesses import get_harness
    from tetrabench.native_discovery import native_installation
    from tetrabench.nativeauth import (
        PI_NODE_MIN_VERSION,
        isolated_auth_environment,
        pi_node_version_supported,
        run_native,
    )

    if purpose not in {"login", "metadata"} or not re.fullmatch(
        r"[0-9]{1,10}\.[0-9]{1,10}\.[0-9]{1,10}", version
    ):
        raise ValueError("native prerequisites require a purpose and exact version")
    package = get_harness(harness).package
    command = f"npm install --global {package}@{version}"
    report = {
        **_check("blocked", "local executable lookup", command),
        "purpose": purpose,
        "install_command": command,
        "expected_version": version,
        "observed_version": None,
    }
    if harness == "pi":
        minimum = ".".join(str(part) for part in PI_NODE_MIN_VERSION)
        report.update(
            node_required=f">={minimum}",
            node_observed=None,
            package_dist="unproven",
            action=(
                f"Use Node >={minimum} and pi on PATH, then {command}. "
                "For auth login, --executable /path/to/node and "
                "--pi-module /path/to/dist/index.js select an explicit installation."
            ),
        )
    env = dict(os.environ if environment is None else environment)
    try:
        # Respect the caller's PATH, not an unrelated ambient interactive install.
        path = env.get("PATH", os.defpath)
        selected = executable or shutil.which(
            "claude" if harness == "claude-code" else harness, path=path
        )
        if modules is None and not selected:
            return report
        installation = native_installation(
            harness,
            modules=modules,
            node=node or shutil.which("node", path=path) or "node",
            executable=Path(selected) if selected else None,
        )
        if harness == "pi":
            if (
                installation.pi_package is None
                or not (installation.pi_package / "dist/index.js").is_file()
            ):
                report["package_dist"] = "missing"
                report["source"] = "local Pi package dist/index.js"
                return report
            report["package_dist"] = "present"
        unshare = shutil.which("unshare", path=path)
        if not unshare:
            report.update(
                status="unproven",
                action="Provide Linux unshare with user/PID/network namespaces; retry.",
            )
            return report
        prefix = [
            unshare,
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            "--kill-child",
            "--net",
        ]
        with tempfile.TemporaryDirectory(prefix="tetrabench-prerequisite-") as tmp:
            work = Path(tmp)
            isolated = isolated_auth_environment(
                harness,
                work,
                base={"PATH": str(Path(installation.node).parent) + os.pathsep + path},
            )
            if harness == "pi":
                result = run_native(
                    [*prefix, installation.node, "--version"],
                    environment=isolated,
                    cwd=work,
                    timeout=15,
                )
                match = re.fullmatch(
                    rb"v(\d{1,10}\.\d{1,10}\.\d{1,10})\s*", result.output
                )
                if result.returncode or match is None:
                    report["source"] = "isolated Node --version (failed)"
                    return report
                report["node_observed"] = match[1].decode("ascii")
                if not pi_node_version_supported(report["node_observed"]):
                    report["source"] = "isolated Node --version"
                    return report
            result = run_native(
                [*prefix, *installation.command, "--version"],
                environment=isolated,
                cwd=work,
                timeout=15,
            )
            pattern = {
                "codex": rb"codex-cli (\d{1,10}\.\d{1,10}\.\d{1,10})\s*",
                "claude-code": rb"(\d{1,10}\.\d{1,10}\.\d{1,10}) \(Claude Code\)\s*",
            }.get(harness, rb"(\d{1,10}\.\d{1,10}\.\d{1,10})\s*")
            match = re.fullmatch(pattern, result.output)
            report["source"] = "isolated native --version"
            if result.returncode or match is None:
                report.update(
                    status="unproven",
                    action=(
                        "Native version probe failed or output was unrecognized. "
                        f"Check namespace support and installation: {command}"
                    ),
                )
                return report
            report["observed_version"] = match[1].decode("ascii")
            if report["observed_version"] == version:
                report.update(status="ok", action="No installation change needed.")
            else:
                report["action"] = (
                    f"Installed version differs from the harness pin. {command}"
                )
    except Exception:
        # Native launch/package/parser failures may carry credentials or private paths.
        report.update(status="unproven", source="local native prerequisite failure")
    return report


def _profile_report(
    config: HarnessConfig,
    *,
    engine: Literal["docker", "modal"],
    online: bool,
    auth_config: Path | None,
    environment: Mapping[str, str],
    artifact_buckets: Sequence[str],
) -> dict[str, Any]:
    from tetrabench.auth_config import ProfileAuthReference
    from tetrabench.auth_profiles import (
        LocalAuthBackend,
        load_auth_config_file,
        load_auth_profile,
        profile_store,
    )

    if config.auth is None or not isinstance(
        config.auth.reference, (NativeAuthReference, ProfileAuthReference)
    ):
        raise ValueError("profile diagnostics require a native auth reference")
    ref = config.auth.reference
    result = {
        **_check(
            "blocked",
            "local auth profile configuration",
            "Configure the selected eval auth profile.",
        ),
        "backend": None,
        "generation": ref.generation if isinstance(ref, NativeAuthReference) else None,
        "state": "unproven",
    }
    try:
        private = load_auth_config_file(auth_config, environment=environment)
        selected = private.profiles.get(ref.profile)
        if selected is not None:
            result.update(
                backend=selected.backend.kind,
                profile_generation=selected.generation,
            )
        if isinstance(ref, NativeAuthReference):
            profile = load_auth_profile(private, ref, config.name)
        else:
            if selected is None or selected.harness != config.name:
                raise ValueError("selected profile does not match harness")
            profile = selected
        backend = profile.backend
        result["backend"] = backend.kind
        if isinstance(backend, LocalAuthBackend):
            if engine == "modal":
                result["action"] = (
                    "Modal OAuth requires a separate private S3 auth backend."
                )
                return result
            if not Path(backend.state_directory).exists():
                result.update(
                    state="absent",
                    action="Run auth login for the selected eval profile.",
                )
                return result
        else:
            refs = [backend.access_key, backend.secret_key]
            if backend.session_token is not None:
                refs.append(backend.session_token)
            names = [item.name for item in refs]
            result["credential_names"] = names
            result["missing_env_names"] = sorted(
                name for name in names if not environment.get(name)
            )
            if (
                len(set(names)) != len(names)
                or any(not name.startswith("TETRABENCH_AUTH_") for name in names)
                or backend.storage.bucket in artifact_buckets
            ):
                result["action"] = (
                    "Use distinct TETRABENCH_AUTH_* references "
                    "and a separate private auth bucket."
                )
                return result
            if not online:
                result.update(
                    status="unproven",
                    action="Use --online for auth backend reads; "
                    "controller credential values remain unobserved.",
                )
                return result
        result["source"] = (
            "local auth authority"
            if backend.kind == "local"
            else "declared S3 auth authority (submitter credentials)"
        )
        if isinstance(backend, LocalAuthBackend):
            from tetrabench.auth_bootstrap import read_local_current

            snapshot = read_local_current(
                Path(backend.state_directory), profile.binding, ref.profile
            )
        else:
            store = profile_store(
                profile,
                engine=engine,
                environment=environment,
                artifact_buckets=artifact_buckets,
            )
            snapshot = store.read(ref.profile)
        if snapshot is None:
            result.update(
                state="absent", action="Run auth login for the selected eval profile."
            )
            return result
        state = snapshot.state
        result.update(state=state.phase, observed_generation=state.generation)
        if (state.harness, state.binding, state.generation) != (
            config.name,
            ref.binding if isinstance(ref, NativeAuthReference) else profile.binding,
            ref.generation
            if isinstance(ref, NativeAuthReference)
            else (profile.generation or state.generation),
        ):
            result["action"] = (
                "Auth authority differs from the harness, binding or generation. "
                "Resolve the reference; do not silently reseed."
            )
        elif state.phase == "ready":
            from tetrabench.nativeauth import inspect_native_store

            native = inspect_native_store(config.name, state.native)
            result.update(
                status="ok",
                native_validity=native.validity,
                native_validity_source=native.source,
                expiry_source=native.expiry_source,
                action="State presence is not account verification; "
                "native refresh may be needed at use time.",
            )
        elif state.phase == "claimed":
            result["action"] = (
                "Profile is claimed; prove its owner stopped before recovery. "
                "Do not copy its refresh lineage."
            )
        else:
            result["action"] = "Run auth login for the selected eval profile."
    except Exception:
        result.update(
            status="blocked",
            action="Cannot read or bind the auth profile. Check private configuration, "
            "generation and backend access; no values retained.",
        )
    return result


def check_controller_metadata(
    spec: ControllerDeploymentSpec,
    *,
    online: bool = False,
    modal_module: Any = None,
) -> dict[str, Any]:
    """Read named Modal resource metadata, never invoke/build/deploy a controller."""
    report = {
        **_check(
            "not_attempted",
            "selected Modal deployment names",
            "Use --online for read-only controller metadata checks.",
        ),
        "remote_runtime_checked": _check(
            "unproven",
            "no controller invocation",
            "Resource existence does not prove deployed version, Secret values, "
            "auth-profile transport or child readiness.",
        ),
        "credential_values_observed": False,
        "checks": [],
    }
    if not online:
        return report
    if modal_module is None:
        import modal as modal_module
    from tetrabench.modal_app import ensure_modal_environment

    source = "Modal environment metadata"
    try:
        ensure_modal_environment(spec, modal_module=modal_module)
        report["checks"].append(_check("ok", source, "Named environment exists."))
        source = "Modal Secret metadata"
        modal_module.Secret.from_name(
            spec.secret_name,
            environment_name=spec.environment_name,
        ).hydrate()
        report["checks"].append(
            _check("ok", source, "Named Secret exists; values and keys were not read.")
        )
        source = "Modal Function metadata"
        modal_module.Function.from_name(
            spec.app_name,
            spec.function_name,
            environment_name=spec.environment_name,
        ).hydrate()
        report["checks"].append(
            _check("ok", source, "Named Function exists; no controller invocation.")
        )
        report.update(
            status="ok",
            source="Modal resource metadata",
            action="Metadata only; remote runtime readiness remains unproven.",
        )
    except Exception:
        report.update(
            status="blocked",
            source=source,
            action="Cannot resolve the Modal resource. Check Modal login, environment, "
            "Secret and deployed Function; no provider message retained.",
        )
    return report


def _provider_report(
    config: HarnessConfig,
    *,
    base: Path,
    auth_config: Path | None,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    from tetrabench.discovery_auth import (
        assert_safe_metadata,
        metadata_auth_context,
    )
    from tetrabench.native_discovery import inspect_installed_handler

    result = {
        **_check(
            "unproven",
            "native discovery pipeline",
            "Catalogs do not establish account, entitlement or successful inference.",
        ),
        "auth_scope": "declared harness.auth only; "
        "native refresh/write-back permitted; no login or inference",
        "metadata_status": "not_attempted",
        "provider_metadata_fetch": "not-observed",
    }
    try:
        from tetrabench.auth_selection import resolve_harness_auth

        config = resolve_harness_auth(
            config, allow_online=True, config_path=auth_config, environment=environment
        )
        with metadata_auth_context(
            config,
            allow_authenticated_read=True,
            auth_config=auth_config,
            environment=environment,
        ) as runtime:
            if runtime is None:
                return result
            assert_safe_metadata(runtime, config.model_dump(mode="json"))
            metadata = inspect_installed_handler(
                config,
                base=base,
                runtime=runtime,
                allow_authenticated_read=True,
                refresh=False,
                allow_config_execution=False,
                reuse_native_cache=False,
            )
            assert_safe_metadata(runtime, metadata)
            status = metadata.get("capability", {}).get("status")
            if status in {"supported", "unsupported", "unknown", "unavailable"}:
                result["metadata_status"] = status
            # The current collector does not attest a provider GET. Successful
            # native or bundled catalog discovery must not set provider_checked=ok.
    except Exception:
        result.update(
            status="failed",
            metadata_status="failed",
            action="Metadata inspection failed; check native prerequisites and auth "
            "status. No provider response or credential values retained.",
        )
    return result


def doctor_auth_report(
    config: HarnessConfig | None,
    *,
    engine: Literal["docker", "modal"] = "docker",
    concurrency: int = 1,
    online: bool = False,
    check_provider: bool = False,
    auth_profile: str | None = None,
    auth_config: Path | None = None,
    environment: Mapping[str, str] | None = None,
    base: Path | None = None,
    artifact_buckets: Sequence[str] = (),
) -> dict[str, Any]:
    """Add auth evidence to doctor without replacing its existing storage checks.

    The caller seals/validates config and resolves any auth-profile override first.
    `online` permits backend reads, not model-provider traffic. `check_provider`
    separately authorizes the existing metadata custody pipeline and native refresh.
    """
    if (
        engine not in {"docker", "modal"}
        or type(concurrency) is not int
        or concurrency < 1
    ):
        raise ValueError("doctor requires a supported engine and positive concurrency")
    env = dict(os.environ if environment is None else environment)
    report: dict[str, Any] = {
        "configured": _check(
            "unproven",
            "selected harness",
            "Select explicit harness.auth to establish billing mode and reference.",
        ),
        "native_ready": _check(
            "not_required",
            "eval execution boundary",
            "Host-native CLI is needed for login/metadata, not API-key evals.",
        ),
        "provider_checked": _check(
            "not_attempted",
            "offline policy",
            "Use --check-provider for declared-auth metadata-only inspection.",
        ),
        "account_verified": _check(
            "unproven",
            "no account verification",
            "Catalogs cannot prove account identity, billing, quota or entitlement.",
        ),
        "remote_runtime_checked": _check(
            "not_attempted",
            "no controller execution",
            "Controller metadata cannot prove credentials or child readiness.",
        ),
        "billing_mode": None,
        "reference": None,
        "missing_env_names": [],
        "conflicting_env_names": [],
        "task_guidance": "Allow agent network access for installation and model "
        "endpoints, and time for installation and inference. The starter task is "
        "offline and too short for model agents. Keep the separate verifier offline.",
    }
    if config is None or config.auth is None:
        if auth_profile is not None or check_provider:
            report["configured"]["status"] = "blocked"
        return report
    spec = config.auth
    ref = spec.reference
    report.update(billing_mode=spec.mode, reference=ref.model_dump(mode="json"))
    report["configured"] = _check(
        "ok",
        "validated harness.auth and submitter environment names",
        "Configuration is not a provider authentication check.",
    )
    permitted = {ref.name} if isinstance(ref, EnvAuthReference) else set()
    report["conflicting_env_names"] = sorted(
        name for name in authentication_environment_names() - permitted if env.get(name)
    )
    if isinstance(ref, EnvAuthReference):
        report["missing_env_names"] = [] if env.get(ref.name) else [ref.name]
        if auth_profile is not None:
            report["configured"] = _check(
                "blocked",
                "auth profile selection",
                "An auth profile selects native OAuth, not an env credential.",
            )
    else:
        report["oauth_concurrency"] = _check(
            "ok" if concurrency == 1 else "blocked",
            "configured Harbor concurrency",
            "Use concurrency=1 and one consumer per OAuth refresh lineage; "
            "separate independent logins for parallel consumers.",
        )
        if auth_profile is not None and auth_profile != ref.profile:
            report["configured"] = _check(
                "blocked",
                "auth profile selection",
                "Auth profile differs from the harness reference; resolve selection.",
            )
            return report
        report["auth_profile"] = _profile_report(
            config,
            engine=engine,
            online=online,
            auth_config=auth_config,
            environment=env,
            artifact_buckets=artifact_buckets,
        )
        profile = report["auth_profile"]
        if profile["status"] != "ok":
            report["configured"] = _check(
                profile["status"], profile["source"], profile["action"]
            )
        if profile["state"] in {"absent", "logged_out"}:
            report["native_ready"] = check_native_prerequisites(
                config.name, config.version, environment=env
            )
        if concurrency != 1:
            report["configured"] = dict(report["oauth_concurrency"])
    if (report["missing_env_names"] or report["conflicting_env_names"]) and not (
        engine == "modal" and isinstance(ref, EnvAuthReference) and not check_provider
    ):
        report["configured"].update(
            status="blocked",
            action="Set missing references and unset conflicting environment names; "
            "values are never printed.",
        )
    if engine == "modal" and isinstance(ref, EnvAuthReference):
        report["credential_scope"] = (
            "submitter environment only; controller Secret values are unobserved"
        )
        if not check_provider and report["configured"]["status"] == "ok":
            report["configured"]["action"] = (
                "The controller resolves this env reference. Submitter key presence "
                "or absence does not establish remote readiness."
            )
    if check_provider:
        report["provider_checked"]["source"] = "explicit --check-provider request"
        if report["configured"]["status"] == "blocked":
            report["provider_checked"].update(
                status="unproven",
                action="Resolve configuration blockers before metadata inspection.",
            )
            return report
        report["native_ready"] = check_native_prerequisites(
            config.name, config.version, purpose="metadata", environment=env
        )
        if report["native_ready"]["status"] != "ok":
            report["provider_checked"].update(
                status="failed",
                metadata_status="not_attempted",
                action="Requested check could not run; resolve native metadata "
                "prerequisites first. No provider metadata request was attempted.",
            )
            return report
        report["provider_checked"] = _provider_report(
            config,
            base=base or Path.cwd(),
            auth_config=auth_config,
            environment=env,
        )
    return report
