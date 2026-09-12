"""Profile-specific deployable Modal controller App construction."""

from __future__ import annotations

import http.client
import io
import json
import os
import re

# The installed interpreter bounds DNS, TLS, and response time in a killable worker.
import subprocess  # nosec B404
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from email.parser import BytesParser
from hashlib import sha256
from importlib.metadata import distribution, version
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import modal

from tetrabench.canonical_json import sha256_hex
from tetrabench.controller_runtime import (
    CONTROLLER_ROOT,
    ControllerRuntime,
    parse_controller_invocation,
)
from tetrabench.diagnostics import sanitize_error
from tetrabench.harbor import ModalChildObserver, S3ChildIdentitySource
from tetrabench.harbor_runner import HarborRunner
from tetrabench.models import ProjectConfig
from tetrabench.preflight import CONTROLLER_PYTHON_VERSION, check_runtime
from tetrabench.s3 import create_s3_store

CONTROLLER_TIMEOUT_SECONDS = 24 * 60 * 60
CONTROLLER_WHEEL_ENV = "TETRABENCH_CONTROLLER_WHEEL"
REMOTE_ARTIFACT_ROOT = "/opt/tetrabench-dist"
UV_VERSION = "0.11.21"
MAX_WHEEL_BYTES = 32 * 1024 * 1024
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30


def _pypi_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"pypi.org", "files.pythonhosted.org"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("controller artifact URL is not on the public PyPI origin")
    return parsed.netloc, parsed.path


def _download(url: str, limit: int) -> bytes:
    """Bound the entire request, including DNS and slow HTTP headers, to 30s."""
    _pypi_url(url)
    try:
        # The executable and module are fixed; URLs are data arguments, not shell code.
        result = subprocess.run(  # nosec B603
            [sys.executable, "-I", "-m", "tetrabench.modal_app", url, str(limit)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            "could not download the installed controller release from PyPI within 30s; "
            f"retry or set {CONTROLLER_WHEEL_ENV} to its original wheel"
        ) from error
    if len(result.stdout) > limit:
        raise ValueError("controller artifact download exceeds its size limit")
    return result.stdout


def _read_public_url(url: str, limit: int) -> bytes:
    """Worker-side bounded read; no redirects, proxies, or ambient credentials."""
    hostname, path = _pypi_url(url)
    connection = http.client.HTTPSConnection(hostname, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    try:
        connection.request("GET", path, headers={"Accept-Encoding": "identity"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError(
                    "PyPI could not provide the installed controller release"
                )
            content_length = response.getheader("Content-Length")
            if content_length is not None and not 0 <= int(content_length) <= limit:
                raise ValueError("controller artifact download exceeds its size limit")
            payload = response.read(limit + 1)
            if len(payload) > limit:
                raise ValueError("controller artifact download exceeds its size limit")
            return payload
    finally:
        connection.close()


def _published_wheel(release: str, filename: str) -> bytes:
    """Resolve one exact release, then verify the wheel's immutable digest."""
    try:
        metadata = json.loads(
            _download(
                f"https://pypi.org/pypi/tetrabench/{release}/json",
                MAX_RELEASE_METADATA_BYTES,
            )
        )
        matches = [
            item
            for item in metadata["urls"]
            if item["filename"] == filename and item["packagetype"] == "bdist_wheel"
        ]
        if len(matches) != 1:
            raise ValueError(
                "PyPI has no unique wheel for the installed controller release"
            )
        artifact = matches[0]
        digest = artifact["digests"]["sha256"]
        size = artifact["size"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not 0 < size <= MAX_WHEEL_BYTES:
            raise ValueError(
                "PyPI controller artifact has invalid size or digest metadata"
            )
        payload = _download(artifact["url"], MAX_WHEEL_BYTES)
        if len(payload) != size or sha256_hex(payload) != digest:
            raise ValueError("PyPI controller wheel size or SHA-256 does not match")
        return payload
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("PyPI returned invalid controller release metadata") from error


def _wheel_bytes(
    filename: str, release: str, direct_url: dict, original_hash: str | None
) -> bytes:
    selected = os.environ.get(CONTROLLER_WHEEL_ENV)
    if not selected and original_hash is None:
        # An installer may retain the source URL without retaining its byte identity.
        # In that case only the canonical, digest-bound published wheel is authority.
        return _published_wheel(release, filename)
    if selected:
        path = Path(selected)
        if path.name != filename:
            raise ValueError(
                "controller wheel filename differs from the installed version"
            )
    else:
        origin = urlsplit(direct_url.get("url", ""))
        path = Path(unquote(origin.path))
        if not (
            origin.scheme == "file"
            and origin.netloc in {"", "localhost"}
            and path.is_absolute()
            and path.name == filename
        ):
            return _published_wheel(release, filename)
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_WHEEL_BYTES + 1)
    except FileNotFoundError:
        if selected:
            raise ValueError(
                "the explicitly selected controller wheel does not exist"
            ) from None
        return _published_wheel(release, filename)
    if len(payload) > MAX_WHEEL_BYTES:
        raise ValueError("controller wheel exceeds 32 MiB")
    if original_hash is None and payload != _published_wheel(release, filename):
        raise ValueError(
            "selected controller wheel differs from the published artifact"
        )
    return payload


def _controller_wheel() -> tuple[str, bytes]:
    """Resolve a release artifact, never a checkout or a site-packages upload."""
    package = distribution("tetrabench")
    if not re.fullmatch(r"[A-Za-z0-9_.+]+", package.version):
        raise ValueError("installed controller has an invalid release version")
    filename = f"tetrabench-{package.version}-py3-none-any.whl"
    direct_url = json.loads(package.read_text("direct_url.json") or "{}")
    if direct_url.get("dir_info", {}).get("editable"):
        raise ValueError(
            "controller deployment requires a wheel installation, not editable source"
        )
    archive_info = direct_url.get("archive_info", {})
    # uv retains a supplied URL hash fragment even when archive_info is empty.
    fragments = (
        archive_info.get("hash", ""),
        urlsplit(direct_url.get("url", "")).fragment,
    )
    recorded_hashes = {
        value
        for value in (
            archive_info.get("hashes", {}).get("sha256"),
            *(
                value.removeprefix("sha256=")
                for value in fragments
                if value.startswith("sha256=")
            ),
        )
        if value
    }
    if len(recorded_hashes) > 1 or any(
        not re.fullmatch(r"[0-9a-f]{64}", value) for value in recorded_hashes
    ):
        raise ValueError(
            "controller installation has invalid or conflicting artifact hashes"
        )
    original_hash = next(iter(recorded_hashes), None)
    payload = _wheel_bytes(filename, package.version, direct_url, original_hash)
    if original_hash and original_hash != sha256_hex(payload):
        raise ValueError(
            "controller wheel differs from the originally installed artifact"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ValueError("controller artifact is not a valid wheel archive") from error
    with archive:
        names = archive.namelist()
        metadata_root = f"tetrabench-{package.version}.dist-info"
        if len(set(names)) != len(names):
            raise ValueError("controller wheel contains duplicate entries")
        if sum(entry.file_size for entry in archive.infolist()) > MAX_WHEEL_BYTES:
            raise ValueError("expanded controller wheel exceeds 32 MiB")
        if f"{metadata_root}/METADATA" not in names:
            raise ValueError("controller wheel lacks installed-version metadata")
        for name in names:
            parts = PurePosixPath(name).parts
            if (
                not parts
                or parts[0] not in {"tetrabench", metadata_root}
                or ".." in parts
                or "\\" in name
                or name.endswith("/")
            ):
                raise ValueError("controller wheel contains an unexpected path")
            if name.startswith("tetrabench/"):
                # Compare to the actual imported package, not merely its metadata.
                installed = Path(__file__).parent.joinpath(*parts[1:])
                if not installed.is_file() or installed.read_bytes() != archive.read(
                    name
                ):
                    raise ValueError(
                        "controller wheel differs from the installed package"
                    )
            elif name != f"{metadata_root}/RECORD":
                installed = Path(str(package.locate_file(name)))
                if not installed.is_file() or installed.read_bytes() != archive.read(
                    name
                ):
                    raise ValueError(
                        "controller wheel metadata differs from the installation"
                    )
        metadata_bytes = archive.read(f"{metadata_root}/METADATA")
        info = BytesParser().parsebytes(metadata_bytes)
        if (
            info["Name"] != "tetrabench"
            or info["Version"] != package.version
            or metadata_bytes
            != Path(str(package.locate_file(f"{metadata_root}/METADATA"))).read_bytes()
        ):
            raise ValueError("controller wheel metadata differs from the installation")
        required = {
            "tetrabench/modal_app.py",
            "tetrabench/_distribution/pyproject.toml",
            "tetrabench/_distribution/uv.lock",
        }
        if not required <= set(names):
            raise ValueError("controller wheel lacks its runtime dependency lock")
        installed_files = {
            str(item)
            for item in package.files or ()
            if str(item).startswith("tetrabench/") and not str(item).endswith(".pyc")
        }
        if installed_files != {
            name for name in names if name.startswith("tetrabench/")
        }:
            raise ValueError(
                "controller wheel file inventory differs from installation"
            )
        installed_modules = {
            f"tetrabench/{module.relative_to(Path(__file__).parent).as_posix()}"
            for module in Path(__file__).parent.rglob("*.py")
        }
        if installed_modules != {name for name in names if name.endswith(".py")}:
            raise ValueError(
                "controller installation contains unexpected Python modules"
            )
    return filename, payload


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
            "tetrabench_version": version("tetrabench"),
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
    # New clients must not resolve a controller deployed by another release.
    release = re.sub(r"[^a-z0-9-]+", "-", version("tetrabench").lower())
    return ControllerDeploymentSpec(
        profile=profile,
        app_name=app_name,
        function_name=config.controller.function_name,
        environment_name=f"{app_name}-{key}-v{release}",
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
    wheel_filename: str
    wheel_sha256: str
    # Modal reads local image inputs lazily, through App.deploy / Image.build.
    artifacts: tempfile.TemporaryDirectory[str] = field(repr=False, compare=False)


def build_modal_controller(
    spec: ControllerDeploymentSpec,
    *,
    modal_module: Any = modal,
) -> ModalControllerBundle:
    """Resolve the installed wheel and build the graph without contacting Modal."""
    check_runtime("controller_build")
    if not 0 < spec.timeout_seconds <= CONTROLLER_TIMEOUT_SECONDS:
        raise ValueError("controller timeout must be between one second and 24 hours")
    wheel_name, wheel_bytes = _controller_wheel()
    artifacts = tempfile.TemporaryDirectory(prefix="tetrabench-controller-")
    artifact_root = Path(artifacts.name)
    local_wheel = artifact_root / wheel_name
    local_wheel.write_bytes(wheel_bytes)
    with zipfile.ZipFile(local_wheel) as archive:
        for name in ("pyproject.toml", "uv.lock"):
            (artifact_root / name).write_bytes(
                archive.read(f"tetrabench/_distribution/{name}")
            )
    wheel_digest = sha256_hex(wheel_bytes)
    remote_wheel = f"{REMOTE_ARTIFACT_ROOT}/{wheel_name}"
    image = (
        modal_module.Image.debian_slim(python_version=CONTROLLER_PYTHON_VERSION)
        .uv_sync(
            str(artifact_root),
            frozen=True,
            extra_options="--no-default-groups",
            uv_version=UV_VERSION,
        )
        .add_local_file(
            local_wheel,
            remote_wheel,
            copy=True,
        )
        .run_commands(
            f"echo '{wheel_digest}  {remote_wheel}' | sha256sum --check --strict",
            "/.uv/uv pip install --python /.uv/.venv/bin/python "
            f"--no-deps {remote_wheel}",
            "/.uv/uv pip check --python /.uv/.venv/bin/python",
        )
    )
    volume = modal_module.Volume.from_name(spec.volume_name, create_if_missing=True)
    secret = modal_module.Secret.from_name(
        spec.secret_name, environment_name=spec.environment_name
    )
    app = modal_module.App(spec.app_name)

    @app.function(
        name=spec.function_name,
        image=image,
        retries=0,
        serialized=True,
        include_source=False,
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
        from tetrabench.runtime_auth import make_credential_context

        result = ControllerRuntime(
            store,
            volume,
            HarborRunner(
                credential_context=make_credential_context(
                    engine="modal",
                    consumer_id=function_call_id,
                    run_id=invocation.run_id,
                    artifact_buckets=[invocation.storage.bucket],
                    forbidden_runtime_roots=[Path(spec.controller_root)],
                )
            ),
            observer,
            controller_root=Path(spec.controller_root),
        ).run(invocation, function_call_id=function_call_id)
        return {
            "attempt_id": result.attempt_id,
            "detail": result.detail,
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
        wheel_filename=wheel_name,
        wheel_sha256=wheel_digest,
        artifacts=artifacts,
    )


def ensure_modal_environment(
    spec: ControllerDeploymentSpec,
    *,
    modal_module: Any = modal,
    client: Any | None = None,
) -> Any:
    """Resolve the explicitly provisioned namespace, without creating resources."""
    check_runtime("controller_deploy")
    try:
        environment = modal_module.Environment.from_name(
            spec.environment_name,
            create_if_missing=False,
            client=client,
        )
        environment.hydrate(client=client)
        return environment
    except modal.exception.Error as error:
        raise sanitize_error(error, operation="modal_environment") from None


def deploy_controller(
    spec: ControllerDeploymentSpec,
    *,
    modal_module: Any = modal,
) -> dict[str, object]:
    """Deploy one already-confirmed profile App."""
    check_runtime("controller_deploy")
    bundle = build_modal_controller(spec, modal_module=modal_module)
    try:
        try:
            client = modal_module.Client.from_env()
        except modal.exception.Error as error:
            raise sanitize_error(error, operation="modal_auth") from None
        ensure_modal_environment(spec, modal_module=modal_module, client=client)
        try:
            bundle.secret.hydrate(client=client)
        except modal.exception.Error as error:
            raise sanitize_error(error, operation="modal_secret") from None
        try:
            bundle.app.deploy(
                name=spec.app_name,
                environment_name=spec.environment_name,
                client=client,
            )
        except modal.exception.Error as error:
            raise sanitize_error(error, operation="controller_deploy") from None
        return spec.as_dict() | {
            "deployed": True,
            "wheel_filename": bundle.wheel_filename,
            "wheel_sha256": bundle.wheel_sha256,
        }
    finally:
        bundle.artifacts.cleanup()


def invocation_arguments(invocation: Any) -> tuple[bytes, str]:
    """Return the only two arguments accepted by the deployed Function."""
    from tetrabench.plan import canonical_model_bytes

    payload = canonical_model_bytes(invocation)
    return payload, sha256_hex(payload)


if __name__ == "__main__":
    sys.stdout.buffer.write(_read_public_url(sys.argv[1], int(sys.argv[2])))
