"""Profile-specific deployable Modal controller App construction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import modal

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller_runtime import (
    CONTROLLER_ROOT,
    ControllerRuntime,
    parse_controller_invocation,
)
from tetrabench.harbor import ModalChildObserver, S3ChildIdentitySource
from tetrabench.harbor_runner import HarborRunner
from tetrabench.models import ProjectConfig
from tetrabench.s3 import create_s3_store

CONTROLLER_TIMEOUT_SECONDS = 24 * 60 * 60
PROJECT_SOURCE_ROOT = Path(__file__).parents[2]
REMOTE_PROJECT_ROOT = "/opt/tetrabench-src"
RUNTIME_REQUIREMENTS = (
    "harbor[modal]==0.22.0",
    "modal==1.5.4",
)


def _profile_key(profile: str | None) -> str:
    if profile is None:
        return "default"
    value = re.sub(r"[^a-z0-9-]+", "-", profile.lower()).strip("-")
    if not value:
        raise ValueError("profile name has no Modal-safe characters")
    digest = sha256(profile.encode("utf-8")).hexdigest()[:8]
    return f"{value[:39]}-{digest}"


@dataclass(frozen=True, slots=True)
class ControllerDeploymentSpec:
    profile: str | None
    app_name: str
    function_name: str
    environment_name: str
    volume_name: str
    secret_name: str
    controller_root: str = str(CONTROLLER_ROOT)
    timeout_seconds: int = CONTROLLER_TIMEOUT_SECONDS

    def as_dict(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "controller_root": self.controller_root,
            "environment_name": self.environment_name,
            "function_name": self.function_name,
            "harbor_version": "0.22.0",
            "modal_version": "1.5.4",
            "profile": self.profile,
            "schema_version": 1,
            "secret_name": self.secret_name,
            "timeout_seconds": self.timeout_seconds,
            "volume_name": self.volume_name,
        }


def controller_deployment_spec(
    config: ProjectConfig,
    profile: str | None,
) -> ControllerDeploymentSpec:
    if config.controller.kind != "modal" or config.execution.kind != "modal":
        raise ValueError(
            "controller deployment requires Modal controller and execution"
        )
    if config.storage is None:
        raise ValueError("controller deployment requires storage configuration")
    if config.controller.secret_name is None:
        raise ValueError("controller deployment requires a named S3 credential Secret")
    key = _profile_key(profile)
    app_name = config.controller.app_name
    return ControllerDeploymentSpec(
        profile=profile,
        app_name=app_name,
        function_name=config.controller.function_name,
        environment_name=f"{app_name}-{key}",
        volume_name=f"{app_name}-{key}-controller",
        secret_name=config.controller.secret_name,
    )


@dataclass(frozen=True, slots=True)
class ModalControllerBundle:
    spec: ControllerDeploymentSpec
    app: Any
    image: Any
    volume: Any
    secret: Any


def build_modal_controller(
    spec: ControllerDeploymentSpec,
    *,
    modal_module: Any = modal,
) -> ModalControllerBundle:
    """Build the App graph without deploying or invoking it."""
    if not 0 < spec.timeout_seconds <= CONTROLLER_TIMEOUT_SECONDS:
        raise ValueError("controller timeout must be between one second and 24 hours")
    image = (
        modal_module.Image.debian_slim(python_version="3.12")
        .add_local_dir(
            PROJECT_SOURCE_ROOT,
            REMOTE_PROJECT_ROOT,
            copy=True,
            ignore=(".git", ".venv", "dist", "__pycache__"),
        )
        .pip_install(*RUNTIME_REQUIREMENTS, REMOTE_PROJECT_ROOT)
    )
    volume = modal_module.Volume.from_name(spec.volume_name, create_if_missing=True)
    secret = modal_module.Secret.from_name(spec.secret_name)
    app = modal_module.App(spec.app_name)

    @app.function(
        name=spec.function_name,
        image=image,
        retries=0,
        serialized=True,
        timeout=spec.timeout_seconds,
        volumes={spec.controller_root: volume},
        secrets=[secret],
    )
    def controller(invocation_json: bytes, invocation_sha256: str) -> dict[str, object]:
        invocation = parse_controller_invocation(invocation_json, invocation_sha256)
        function_call_id = modal_module.current_function_call_id()
        if not function_call_id:
            raise RuntimeError("Modal did not expose the current FunctionCall ID")
        store = create_s3_store(invocation.storage)
        observer = ModalChildObserver(
            S3ChildIdentitySource(store),
            environment_name=spec.environment_name,
        )
        result = ControllerRuntime(
            store,
            volume,
            HarborRunner(),
            observer,
            controller_root=Path(spec.controller_root),
        ).run(invocation, function_call_id=function_call_id)
        return {
            "attempt_id": result.attempt_id,
            "run_id": result.run_id,
            "state": result.state,
            "terminal_sha256": result.terminal_sha256,
        }

    return ModalControllerBundle(
        spec=spec,
        app=app,
        image=image,
        volume=volume,
        secret=secret,
    )


def ensure_modal_environment(
    spec: ControllerDeploymentSpec,
    *,
    modal_module: Any = modal,
    client: Any | None = None,
) -> Any:
    """Create or resolve the profile Environment before deployment."""
    environment = modal_module.Environment.from_name(
        spec.environment_name,
        create_if_missing=True,
        client=client,
    )
    environment.hydrate(client=client)
    return environment


def deploy_controller(
    spec: ControllerDeploymentSpec,
    *,
    modal_module: Any = modal,
) -> None:
    """Deploy one already-confirmed profile App."""
    client = modal_module.Client.from_env()
    ensure_modal_environment(spec, modal_module=modal_module, client=client)
    bundle = build_modal_controller(spec, modal_module=modal_module)
    bundle.app.deploy(
        name=spec.app_name,
        environment_name=spec.environment_name,
        client=client,
    )


def invocation_arguments(invocation: Any) -> tuple[bytes, str]:
    """Return the only two arguments accepted by the deployed Function."""
    from tetrabench.plan import canonical_model_bytes

    payload = canonical_model_bytes(invocation)
    return payload, sha256_hex(payload)
