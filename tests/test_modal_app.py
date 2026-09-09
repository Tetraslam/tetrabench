from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from importlib.metadata import Distribution, metadata, version
from pathlib import Path
from types import SimpleNamespace

import modal
import pytest
from typer.testing import CliRunner

from tetrabench.canonical_json import loads_canonical_json
from tetrabench.cli import app
from tetrabench.config import load_project_config
from tetrabench.diagnostics import DiagnosticError, PreflightError
from tetrabench.modal_app import (
    CONTROLLER_TIMEOUT_SECONDS,
    CONTROLLER_WHEEL_ENV,
    REMOTE_ARTIFACT_ROOT,
    UV_VERSION,
    _controller_wheel,
    _download,
    _read_public_url,
    build_modal_controller,
    controller_deployment_spec,
    deploy_controller,
)

ROOT = Path(__file__).parents[1]
runner = CliRunner()


class _Image:
    def __init__(self, operations):
        self.operations = operations

    def uv_sync(self, project_dir, **kwargs):
        self.operations.append(("uv_sync", (Path(project_dir), kwargs)))
        return self

    def add_local_file(self, local_path, remote_path, *, copy):
        self.operations.append(("wheel", (Path(local_path), remote_path, copy)))
        return self

    def run_commands(self, *commands):
        self.operations.append(("commands", commands))
        return self


class _App:
    def __init__(self, name, operations):
        self.name = name
        self.operations = operations
        self.function_options = None
        self.function_body = None

    def function(self, **kwargs):
        self.function_options = kwargs

        def decorate(function):
            self.function_body = function
            return function

        return decorate

    def deploy(self, **kwargs):
        self.operations.append(("deploy", kwargs))


class _FakeModal:
    def __init__(self):
        self.operations = []
        self.apps = []
        self.Image = SimpleNamespace(debian_slim=self.debian_slim)
        self.Volume = SimpleNamespace(from_name=self.volume_from_name)
        self.Secret = SimpleNamespace(from_name=self.secret_from_name)
        self.Client = SimpleNamespace(from_env=lambda: None)
        self.Environment = SimpleNamespace()
        self.App = self.app

    def debian_slim(self, *, python_version):
        self.operations.append(("debian", python_version))
        return _Image(self.operations)

    def volume_from_name(self, name, *, create_if_missing):
        self.operations.append(("volume", (name, create_if_missing)))
        return SimpleNamespace(name=name)

    def secret_from_name(self, name, *, environment_name):
        self.operations.append(("secret", (name, environment_name)))
        return SimpleNamespace(
            name=name,
            hydrate=lambda *, client: self.operations.append(
                ("secret_hydrate", client)
            ),
        )

    def app(self, name):
        app = _App(name, self.operations)
        self.apps.append(app)
        return app


def _spec():
    config = load_project_config(ROOT)
    return controller_deployment_spec(config, "gpu-lab")


def test_profile_specific_names_are_exact_and_secret_is_name_only() -> None:
    spec = _spec()
    assert spec.app_name == "tetrabench"
    assert spec.function_name == "controller"
    assert spec.environment_name.startswith("tetrabench-gpu-lab-")
    assert spec.environment_name.endswith("-v0-2-0")
    assert spec.volume_name.startswith("tetrabench-gpu-lab-")
    assert spec.volume_name.endswith("-controller")
    assert spec.secret_name == "tetrabench-controller"
    assert spec.controller_root == "/tetrabench/controller"


def test_profile_name_normalization_cannot_alias_distinct_profiles() -> None:
    config = load_project_config(ROOT)
    underscore = controller_deployment_spec(config, "gpu_lab")
    hyphen = controller_deployment_spec(config, "gpu-lab")
    assert underscore.environment_name != hyphen.environment_name
    assert underscore.volume_name != hyphen.volume_name


def test_releases_cannot_resolve_each_others_controller(monkeypatch) -> None:
    monkeypatch.setattr("tetrabench.modal_app.version", lambda _: "0.1.0")
    first = _spec()
    monkeypatch.setattr("tetrabench.modal_app.version", lambda _: "0.2.0")
    second = _spec()
    assert first.environment_name != second.environment_name
    assert first.app_name == second.app_name
    assert first.function_name == second.function_name


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    output = tmp_path_factory.mktemp("modal-wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return next(output.glob("*.whl"))


def test_documented_uv_tool_install_retains_original_wheel_hash(built_wheel, tmp_path):
    assert not tmp_path.resolve().is_relative_to(ROOT.resolve())
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / built_wheel.name
    shutil.copyfile(built_wheel, wheel)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    blocks = re.findall(r"```console\n(.*?)```", (ROOT / "README.md").read_text(), re.S)
    install = [block for block in blocks if "file://$wheel#sha256=$digest" in block]
    assert len(install) == 1
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "PYTHON", "TETRABENCH_", "AWS_", "MODAL_"))
        and key != "VIRTUAL_ENV"
    }
    environment.update(
        UV_TOOL_DIR=str(tmp_path / "tools"),
        UV_TOOL_BIN_DIR=str(tmp_path / "bin"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        UV_NO_CONFIG="true",
    )
    installed = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", install[0]],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert installed.returncode == 0, installed.stderr
    python = tmp_path / "tools/tetrabench/bin/python"
    probe = """
import hashlib
import json
import os
import sys
from importlib.metadata import distribution
from pathlib import Path
from unittest.mock import patch

import tetrabench
from tetrabench.modal_app import _controller_wheel

work = Path.cwd()
assert Path(tetrabench.__file__).is_relative_to(work / "tools")
assert "TETRABENCH_CONTROLLER_WHEEL" not in os.environ
package = distribution("tetrabench")
origin = json.loads(package.read_text("direct_url.json"))
with patch("tetrabench.modal_app._download", side_effect=AssertionError("PyPI access")):
    name, payload = _controller_wheel()
assert hashlib.sha256(payload).hexdigest() == sys.argv[1]
assert payload == (work / "dist" / name).read_bytes()
assert sys.argv[1] in json.dumps(origin)
print(json.dumps({"wheel_sha256": sys.argv[1], "origin": origin}))
"""
    resolved = subprocess.run(
        [str(python), "-I", "-c", probe, digest],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(resolved.stdout)["wheel_sha256"] == digest
    installed_cli = tmp_path / "bin/tetrabench"
    subprocess.run(
        [str(installed_cli), "init", str(tmp_path / "project")],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        timeout=30,
    )


def test_documented_modal_secret_uses_only_named_process_environment(monkeypatch):
    blocks = re.findall(r"```console\n(.*?)```", (ROOT / "README.md").read_text(), re.S)
    commands = [block for block in blocks if "modal.Secret.objects.create(" in block]
    assert len(commands) == 1
    source = commands[0].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-controller-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-controller-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-forward")
    calls = []
    monkeypatch.setattr(
        modal,
        "Secret",
        SimpleNamespace(
            objects=SimpleNamespace(
                create=lambda *args, **kwargs: calls.append((args, kwargs))
            )
        ),
    )
    exec(compile(source, "README.md:Modal Secret", "exec"), {})
    assert calls == [
        (
            (
                "tetrabench-controller",
                {
                    "AWS_ACCESS_KEY_ID": "synthetic-controller-id",
                    "AWS_SECRET_ACCESS_KEY": "synthetic-controller-secret",
                },
            ),
            {"environment_name": "ENVIRONMENT_FROM_INFO"},
        )
    ]


@pytest.fixture
def controller_artifact(built_wheel, tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(installed)
    package = Distribution.at(next(installed.glob("*.dist-info")))
    monkeypatch.setattr("tetrabench.modal_app.distribution", lambda _: package)
    monkeypatch.setattr(
        "tetrabench.modal_app.__file__", str(installed / "tetrabench/modal_app.py")
    )
    monkeypatch.setenv(CONTROLLER_WHEEL_ENV, str(built_wheel))
    _record_origin(installed, built_wheel, built_wheel.read_bytes())
    return built_wheel, installed


def test_image_and_function_dependency_contract(controller_artifact) -> None:
    fake = _FakeModal()
    bundle = build_modal_controller(_spec(), modal_module=fake)
    options = fake.apps[0].function_options
    wheel, _ = controller_artifact
    operation = next(item for item in fake.operations if item[0] == "wheel")
    assert operation[1][0].read_bytes() == wheel.read_bytes()
    assert operation[1][1:] == (f"{REMOTE_ARTIFACT_ROOT}/{wheel.name}", True)
    sync = next(item for item in fake.operations if item[0] == "uv_sync")
    assert set(path.name for path in sync[1][0].iterdir()) == {
        wheel.name,
        "pyproject.toml",
        "uv.lock",
    }
    assert sync[1][1] == {
        "frozen": True,
        "extra_options": "--no-default-groups",
        "uv_version": UV_VERSION,
    }
    commands = next(item[1] for item in fake.operations if item[0] == "commands")
    assert bundle.wheel_sha256 in commands[0]
    assert "--no-deps" in commands[1]
    assert options["serialized"] is True
    assert options["include_source"] is False
    assert options["retries"] == 0
    assert options["timeout"] == CONTROLLER_TIMEOUT_SECONDS
    assert options["volumes"] == {
        "/tetrabench/controller": bundle.volume,
    }
    assert options["secrets"] == [bundle.secret]


def test_real_modal_154_constructs_dynamic_serialized_function(
    controller_artifact,
) -> None:
    assert modal.__version__ == "1.5.4"
    bundle = build_modal_controller(_spec())
    assert tuple(bundle.app.registered_functions) == ("controller",)


def test_installed_distribution_metadata_has_exact_runtime_dependencies() -> None:
    package = metadata("tetrabench")
    requirements = package.get_all("Requires-Dist") or []
    assert version("tetrabench") == "0.2.0"
    assert package["Requires-Python"] == "<3.13,>=3.12"
    assert "harbor[modal]==0.22.0" in requirements
    assert "modal==1.5.4" in requirements


def test_deploy_ensures_environment_before_app_deploy(controller_artifact) -> None:
    fake = _FakeModal()
    client = object()
    environment = SimpleNamespace(
        hydrate=lambda *, client: fake.operations.append(("hydrate", client))
    )
    fake.Client = SimpleNamespace(from_env=lambda: client)
    fake.Environment = SimpleNamespace(
        from_name=lambda name, **kwargs: (
            fake.operations.append(("environment", (name, kwargs))) or environment
        )
    )

    report = deploy_controller(_spec(), modal_module=fake)
    wheel, _ = controller_artifact
    assert report["wheel_filename"] == wheel.name
    assert report["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert report["deployed"] is True

    environment_index = next(
        index for index, item in enumerate(fake.operations) if item[0] == "environment"
    )
    deploy_index = next(
        index for index, item in enumerate(fake.operations) if item[0] == "deploy"
    )
    assert environment_index < deploy_index
    assert fake.operations[environment_index][1][1] == {
        "create_if_missing": False,
        "client": client,
    }
    assert fake.operations[environment_index + 1] == ("hydrate", client)
    assert fake.operations[deploy_index - 1] == ("secret_hydrate", client)


def test_environment_creation_failure_prevents_deploy(controller_artifact) -> None:
    fake = _FakeModal()
    client = object()
    fake.Client = SimpleNamespace(from_env=lambda: client)
    fake.Environment = SimpleNamespace(
        from_name=lambda *_args, **_kwargs: SimpleNamespace(
            hydrate=lambda **_kwargs: (_ for _ in ()).throw(
                modal.exception.AuthError("not authenticated")
            )
        )
    )

    with pytest.raises(DiagnosticError, match="Modal authentication") as caught:
        deploy_controller(_spec(), modal_module=fake)
    assert caught.value.code == "authentication_required"
    assert caught.value.exception_type == "AuthError"

    assert not any(item[0] == "deploy" for item in fake.operations)


def test_unavailable_published_wheel_rejects_before_provider_client(
    controller_artifact, monkeypatch
) -> None:
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV, raising=False)
    _, installed = controller_artifact
    (next(installed.glob("*.dist-info")) / "direct_url.json").unlink()
    monkeypatch.setattr(
        "tetrabench.modal_app._download",
        lambda *_: (_ for _ in ()).throw(ValueError("PyPI release unavailable")),
    )
    fake = _FakeModal()
    fake.Client.from_env = lambda: pytest.fail("constructed a provider client")
    with pytest.raises(ValueError, match="PyPI release unavailable"):
        deploy_controller(_spec(), modal_module=fake)
    assert fake.operations == []


def _record_origin(installed: Path, wheel: Path, payload: bytes) -> None:
    (next(installed.glob("*.dist-info")) / "direct_url.json").write_text(
        json.dumps(
            {
                "url": wheel.as_uri(),
                "archive_info": {
                    "hashes": {"sha256": hashlib.sha256(payload).hexdigest()}
                },
            }
        )
    )


def _mock_pypi(monkeypatch, wheel, payload=None, digest=None):
    data = payload if payload is not None else wheel.read_bytes()
    url = f"https://files.pythonhosted.org/packages/ab/cd/{wheel.name}"
    metadata = json.dumps(
        {
            "urls": [
                {
                    "filename": wheel.name,
                    "packagetype": "bdist_wheel",
                    "url": url,
                    "size": len(data),
                    "digests": {"sha256": digest or hashlib.sha256(data).hexdigest()},
                }
            ]
        }
    ).encode()
    calls = []

    def download(selected, limit):
        calls.append((selected, limit))
        return metadata if selected.endswith("/json") else data

    monkeypatch.setattr("tetrabench.modal_app._download", download)
    return calls


def test_original_local_wheel_resolves_without_override(
    controller_artifact, monkeypatch
) -> None:
    wheel, installed = controller_artifact
    _record_origin(installed, wheel, wheel.read_bytes())
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    monkeypatch.setattr(
        "tetrabench.modal_app._download", lambda *_: pytest.fail("network")
    )
    assert _controller_wheel() == (wheel.name, wheel.read_bytes())


def test_pypi_install_resolves_exact_version_without_override(
    controller_artifact, monkeypatch
) -> None:
    wheel, _ = controller_artifact
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    # Registry installations have no PEP 610 direct URL.
    _, installed = controller_artifact
    (next(installed.glob("*.dist-info")) / "direct_url.json").unlink()
    calls = _mock_pypi(monkeypatch, wheel)
    assert _controller_wheel() == (wheel.name, wheel.read_bytes())
    assert calls[0][0] == "https://pypi.org/pypi/tetrabench/0.2.0/json"
    assert calls[1][0].endswith(wheel.name)


def test_missing_original_local_wheel_falls_back_to_matching_pypi(
    controller_artifact, tmp_path, monkeypatch
) -> None:
    wheel, installed = controller_artifact
    _record_origin(installed, tmp_path / wheel.name, wheel.read_bytes())
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    _mock_pypi(monkeypatch, wheel)
    assert _controller_wheel() == (wheel.name, wheel.read_bytes())


def test_pypi_digest_mismatch_fails_before_provider(controller_artifact, monkeypatch):
    wheel, installed = controller_artifact
    (next(installed.glob("*.dist-info")) / "direct_url.json").unlink()
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    _mock_pypi(monkeypatch, wheel, digest="0" * 64)
    fake = _FakeModal()
    with pytest.raises(ValueError, match="SHA-256 does not match"):
        deploy_controller(_spec(), modal_module=fake)
    assert fake.operations == []


def test_pypi_same_version_different_installed_code_is_rejected(
    controller_artifact, monkeypatch
) -> None:
    wheel, installed = controller_artifact
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    (next(installed.glob("*.dist-info")) / "direct_url.json").unlink()
    _mock_pypi(monkeypatch, wheel)
    (installed / "tetrabench/modal_app.py").write_text("# other build\n")
    with pytest.raises(ValueError, match="differs from the installed package"):
        _controller_wheel()


def test_automatic_image_build_never_uploads_installation_parent(
    controller_artifact, monkeypatch
) -> None:
    wheel, installed = controller_artifact
    (installed / "private-eval-and-credentials.txt").write_text("not image input")
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    (next(installed.glob("*.dist-info")) / "direct_url.json").unlink()
    _mock_pypi(monkeypatch, wheel)
    fake = _FakeModal()
    bundle = build_modal_controller(_spec(), modal_module=fake)
    uploaded = next(item[1][0] for item in fake.operations if item[0] == "wheel")
    assert uploaded.read_bytes() == wheel.read_bytes()
    assert {path.name for path in uploaded.parent.iterdir()} == {
        wheel.name,
        "pyproject.toml",
        "uv.lock",
    }
    assert fake.apps[0].function_options["include_source"] is False
    bundle.artifacts.cleanup()


@pytest.mark.parametrize(
    "status, declared, body, message",
    [
        (302, None, b"", "could not provide"),
        (200, "101", b"", "size limit"),
        (200, None, b"x" * 101, "size limit"),
        (200, "100", b"x" * 100, None),
    ],
)
def test_download_bounds_and_redirect_refusal(
    monkeypatch, status, declared, body, message
) -> None:
    class Response:
        def __init__(self):
            self.status = status
            self.remaining = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def getheader(self, _name):
            return declared

        def read(self, count):
            result, self.remaining = self.remaining[:count], self.remaining[count:]
            return result

    closed = []
    connection = SimpleNamespace(
        request=lambda *_args, **_kwargs: None,
        getresponse=Response,
        sock=None,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        "tetrabench.modal_app.http.client.HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )
    if message is None:
        assert (
            _read_public_url("https://pypi.org/pypi/tetrabench/0.1.0/json", 100) == body
        )
    else:
        with pytest.raises(ValueError, match=message):
            _read_public_url("https://pypi.org/pypi/tetrabench/0.1.0/json", 100)
    assert closed == [True]


def test_download_timeout_is_bounded_and_redacted(monkeypatch):
    original = subprocess.run

    def hung_request(_command, **kwargs):
        assert kwargs["timeout"] == 0.1
        return original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    monkeypatch.setattr("tetrabench.modal_app.DOWNLOAD_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(
        "tetrabench.modal_app.subprocess.run",
        hung_request,
    )
    start = time.monotonic()
    with pytest.raises(ValueError, match="could not download") as caught:
        _download("https://pypi.org/pypi/tetrabench/0.1.0/json", 100)
    assert time.monotonic() - start < 5
    assert "time.sleep" not in str(caught.value)


def test_downloader_uses_only_the_installed_interpreter(monkeypatch):
    calls = []

    def download(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=b"verified-by-caller")

    monkeypatch.setattr("tetrabench.modal_app.subprocess.run", download)
    url = "https://files.pythonhosted.org/packages/a/tetrabench.whl"
    assert _download(url, 100) == b"verified-by-caller"
    assert calls[0][0] == [
        sys.executable,
        "-I",
        "-m",
        "tetrabench.modal_app",
        url,
        "100",
    ]
    assert calls[0][1]["timeout"] == 30
    assert "shell" not in calls[0][1]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://evil.example/a.whl",
        "http://files.pythonhosted.org/a.whl",
        "https://pypi.org@evil.example/a.whl",
    ],
)
def test_download_rejects_non_pypi_urls(url, monkeypatch):
    monkeypatch.setattr(
        "tetrabench.modal_app.http.client.HTTPSConnection",
        lambda *_args, **_kwargs: pytest.fail("opened connection"),
    )
    with pytest.raises(ValueError, match="public PyPI origin"):
        _download(url, 100)


def test_installed_module_mutation_rejects_wheel(controller_artifact) -> None:
    _, installed = controller_artifact
    (installed / "tetrabench/modal_app.py").write_text("# changed\n")
    with pytest.raises(ValueError, match="differs from the installed package"):
        _controller_wheel()


def test_extra_installed_module_rejects_wheel(controller_artifact) -> None:
    _, installed = controller_artifact
    (installed / "tetrabench/unexpected.py").write_text("# extra\n")
    with pytest.raises(ValueError, match="unexpected Python modules"):
        _controller_wheel()


def test_editable_install_rejects_deployment(controller_artifact) -> None:
    _, installed = controller_artifact
    metadata_dir = next(installed.glob("*.dist-info"))
    (metadata_dir / "direct_url.json").write_text('{"dir_info":{"editable":true}}')
    with pytest.raises(ValueError, match="not editable source"):
        _controller_wheel()


def test_original_artifact_hash_is_checked(controller_artifact) -> None:
    _, installed = controller_artifact
    metadata_dir = next(installed.glob("*.dist-info"))
    (metadata_dir / "direct_url.json").write_text(
        json.dumps({"archive_info": {"hashes": {"sha256": "0" * 64}}})
    )
    with pytest.raises(ValueError, match="originally installed artifact"):
        _controller_wheel()


def test_image_uses_retained_snapshot_not_mutable_input(
    controller_artifact, tmp_path, monkeypatch
) -> None:
    fake = _FakeModal()
    wheel, _ = controller_artifact
    input_wheel = tmp_path / wheel.name
    original = wheel.read_bytes()
    input_wheel.write_bytes(original)
    monkeypatch.setenv(CONTROLLER_WHEEL_ENV, str(input_wheel))
    bundle = build_modal_controller(_spec(), modal_module=fake)
    copied = next(item[1][0] for item in fake.operations if item[0] == "wheel")
    input_wheel.write_bytes(b"changed after graph construction")
    assert copied != input_wheel
    assert copied.read_bytes() == original
    bundle.artifacts.cleanup()
    assert not copied.exists()


@pytest.mark.parametrize("extra", ["../outside", "eval-repo/secret", "tetrabench/x.py"])
def test_wheel_extra_paths_fail_before_image_or_provider(
    controller_artifact, tmp_path, monkeypatch, extra
) -> None:
    wheel, installed = controller_artifact
    changed = tmp_path / wheel.name
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(changed, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr(extra, b"unexpected")
    _record_origin(installed, changed, changed.read_bytes())
    monkeypatch.setenv(CONTROLLER_WHEEL_ENV, str(changed))
    fake = _FakeModal()
    with pytest.raises(ValueError):
        deploy_controller(_spec(), modal_module=fake)
    assert fake.operations == []


def test_wheel_wrong_release_filename_fails(controller_artifact, tmp_path, monkeypatch):
    wheel, _ = controller_artifact
    changed = tmp_path / "tetrabench-0.1.0-py3-none-any.whl"
    changed.write_bytes(wheel.read_bytes())
    monkeypatch.setenv(CONTROLLER_WHEEL_ENV, str(changed))
    with pytest.raises(ValueError, match="installed version"):
        _controller_wheel()


def test_corrupt_wheel_is_rejected_before_provider(
    controller_artifact, tmp_path, monkeypatch
) -> None:
    wheel, installed = controller_artifact
    changed = tmp_path / wheel.name
    changed.write_bytes(b"not a zip")
    _record_origin(installed, changed, changed.read_bytes())
    monkeypatch.setenv(CONTROLLER_WHEEL_ENV, str(changed))
    fake = _FakeModal()
    with pytest.raises(ValueError, match="valid wheel archive"):
        deploy_controller(_spec(), modal_module=fake)
    assert fake.operations == []


def test_uv_hash_fragment_resolves_original_wheel(controller_artifact, monkeypatch):
    wheel, installed = controller_artifact
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    origin = {
        "url": f"{wheel.as_uri()}#sha256={digest}",
        "archive_info": {},
    }
    (next(installed.glob("*.dist-info")) / "direct_url.json").write_text(
        json.dumps(origin)
    )
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    monkeypatch.setattr(
        "tetrabench.modal_app._download", lambda *_: pytest.fail("network")
    )
    assert _controller_wheel() == (wheel.name, wheel.read_bytes())


def test_byte_different_repacked_wheel_is_rejected(
    controller_artifact, tmp_path, monkeypatch
):
    wheel, installed = controller_artifact
    copied = tmp_path / wheel.name
    copied.write_bytes(wheel.read_bytes())
    _record_origin(installed, copied, copied.read_bytes())
    with zipfile.ZipFile(copied, "a") as archive:
        archive.comment = b"same installed files, different release bytes"
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    with pytest.raises(ValueError, match="originally installed artifact"):
        _controller_wheel()


def test_unhashed_local_origin_requires_canonical_published_wheel(
    controller_artifact, monkeypatch
):
    wheel, installed = controller_artifact
    (next(installed.glob("*.dist-info")) / "direct_url.json").write_text(
        json.dumps({"url": wheel.as_uri(), "archive_info": {}})
    )
    monkeypatch.delenv(CONTROLLER_WHEEL_ENV)
    calls = _mock_pypi(monkeypatch, wheel)
    assert _controller_wheel() == (wheel.name, wheel.read_bytes())
    assert len(calls) == 2


@pytest.mark.parametrize("filename", ["entry_points.txt", "WHEEL"])
def test_installed_packaging_metadata_must_match_wheel(controller_artifact, filename):
    _, installed = controller_artifact
    (next(installed.glob("*.dist-info")) / filename).write_text(
        "unexpected code source"
    )
    with pytest.raises(ValueError, match="metadata differs"):
        _controller_wheel()


def test_controller_info_is_no_cloud_and_json_lists_exact_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        "tetrabench.cli.deploy_controller",
        lambda _spec: pytest.fail("info attempted deployment"),
    )
    monkeypatch.setattr(
        "tetrabench.modal_app._controller_wheel",
        lambda: pytest.fail("info attempted artifact resolution"),
    )
    result = runner.invoke(app, ["controller", "info", "--json"])
    assert result.exit_code == 0
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report["environment_name"] == "tetrabench-default-v0-2-0"
    assert report["volume_name"] == "tetrabench-default-controller"
    assert report["secret_name"] == "tetrabench-controller"


def test_controller_deploy_confirmation_refusal_has_no_cloud_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("tetrabench.cli.deploy_controller", calls.append)
    result = runner.invoke(app, ["controller", "deploy"], input="n\n")
    assert result.exit_code == 1
    assert calls == []
    assert "no cloud mutation attempted" in result.stderr


def test_controller_deploy_yes_json_invokes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.chdir(ROOT)
    digest = "a" * 64

    def deploy(spec):
        calls.append(spec)
        return spec.as_dict() | {
            "deployed": True,
            "wheel_filename": "tetrabench-0.2.0-py3-none-any.whl",
            "wheel_sha256": digest,
        }

    monkeypatch.setattr("tetrabench.cli.deploy_controller", deploy)
    result = runner.invoke(app, ["controller", "deploy", "--yes", "--json"])
    assert result.exit_code == 0
    assert len(calls) == 1
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report["deployed"] is True
    assert report["wheel_sha256"] == digest
    assert report["wheel_filename"] == "tetrabench-0.2.0-py3-none-any.whl"


def test_controller_deploy_json_without_yes_is_dry() -> None:
    result = runner.invoke(app, ["controller", "deploy", "--json"])
    assert result.exit_code == 2
    report = loads_canonical_json(result.stderr.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert "requires --yes" in str(report["error"])


@pytest.mark.parametrize("entrypoint", [build_modal_controller, deploy_controller])
@pytest.mark.parametrize("python_version", [(3, 11, 9), (3, 13, 0), (3, 14, 0)])
def test_unsupported_python_fails_before_artifact_or_provider(
    monkeypatch, entrypoint, python_version
):
    # sys.version_info is read at invocation time, not cached during import.
    monkeypatch.setattr("tetrabench.preflight.sys.version_info", python_version)
    monkeypatch.setattr(
        "tetrabench.modal_app._controller_wheel", lambda: pytest.fail("artifact access")
    )
    fake = _FakeModal()
    fake.Client.from_env = lambda: pytest.fail("provider client")
    with pytest.raises(
        PreflightError, match=r"--python 3\.12 tetrabench==0\.2\.0"
    ) as caught:
        entrypoint(_spec(), modal_module=fake)
    assert caught.value.code == "unsupported_python"
    assert fake.operations == []


def test_unsupported_platform_fails_before_artifact_or_provider(monkeypatch):
    monkeypatch.setattr("tetrabench.preflight.sys.platform", "darwin")
    monkeypatch.setattr(
        "tetrabench.modal_app._controller_wheel", lambda: pytest.fail("artifact access")
    )
    with pytest.raises(PreflightError, match="requires Linux"):
        deploy_controller(_spec(), modal_module=_FakeModal())


@pytest.mark.parametrize("resource", ["environment", "secret"])
def test_missing_namespace_or_secret_prevents_costly_deployment(
    controller_artifact, resource
):
    fake = _FakeModal()

    def missing(**_kwargs):
        raise modal.exception.NotFoundError("https://private.invalid/?token=secret")

    fake.Environment.from_name = lambda *_args, **_kwargs: SimpleNamespace(
        hydrate=missing if resource == "environment" else lambda **_kwargs: None
    )
    if resource == "secret":
        fake.Secret.from_name = lambda *_args, **_kwargs: SimpleNamespace(
            hydrate=missing
        )
    with pytest.raises(DiagnosticError) as caught:
        deploy_controller(_spec(), modal_module=fake)
    assert caught.value.code == f"modal_{resource}_missing"
    assert caught.value.operation == f"modal_{resource}"
    assert "private.invalid" not in str(caught.value)
    assert "token=secret" not in str(caught.value)
    assert not any(item[0] == "deploy" for item in fake.operations)


def test_unknown_deploy_error_preserves_safe_context_and_cleans_snapshot(
    controller_artifact, monkeypatch
):
    fake = _FakeModal()
    fake.Environment.from_name = lambda *_args, **_kwargs: SimpleNamespace(
        hydrate=lambda **_kwargs: None
    )

    def fail_deploy(self, **_kwargs):
        raise modal.exception.InvalidError("credential-value https://private.invalid")

    monkeypatch.setattr(_App, "deploy", fail_deploy)
    with pytest.raises(DiagnosticError) as caught:
        deploy_controller(_spec(), modal_module=fake)
    assert caught.value.code == "provider_request_failed"
    assert caught.value.operation == "controller_deploy"
    assert caught.value.exception_type == "InvalidError"
    assert "credential-value" not in str(caught.value)
    uploaded = next(item[1][0] for item in fake.operations if item[0] == "wheel")
    assert not uploaded.exists()
