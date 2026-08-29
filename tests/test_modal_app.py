from __future__ import annotations

from importlib.metadata import metadata, version
from pathlib import Path
from types import SimpleNamespace

import modal
import pytest
from typer.testing import CliRunner

from tetrabench.canonical_json import loads_canonical_json
from tetrabench.cli import app
from tetrabench.config import load_project_config
from tetrabench.modal_app import (
    CONTROLLER_TIMEOUT_SECONDS,
    REMOTE_PROJECT_ROOT,
    RUNTIME_REQUIREMENTS,
    build_modal_controller,
    controller_deployment_spec,
    deploy_controller,
)

ROOT = Path(__file__).parents[1]
runner = CliRunner()


class _Image:
    def __init__(self, operations):
        self.operations = operations

    def pip_install(self, *packages):
        self.operations.append(("pip_install", packages))
        return self

    def add_local_dir(self, local_path, remote_path, *, copy, ignore):
        self.operations.append(
            ("project", (Path(local_path), remote_path, copy, tuple(ignore)))
        )
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

    def secret_from_name(self, name):
        self.operations.append(("secret", name))
        return SimpleNamespace(name=name)

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


def test_image_and_function_dependency_contract() -> None:
    fake = _FakeModal()
    bundle = build_modal_controller(_spec(), modal_module=fake)
    options = fake.apps[0].function_options
    project_operation = next(item for item in fake.operations if item[0] == "project")
    assert (project_operation[1][0] / "pyproject.toml").is_file()
    assert project_operation[1][1:3] == (REMOTE_PROJECT_ROOT, True)
    assert (
        "pip_install",
        (*RUNTIME_REQUIREMENTS, REMOTE_PROJECT_ROOT),
    ) in fake.operations
    assert options["serialized"] is True
    assert options["retries"] == 0
    assert options["timeout"] == CONTROLLER_TIMEOUT_SECONDS
    assert options["volumes"] == {
        "/tetrabench/controller": bundle.volume,
    }
    assert options["secrets"] == [bundle.secret]


def test_real_modal_154_constructs_dynamic_serialized_function() -> None:
    assert modal.__version__ == "1.5.4"
    bundle = build_modal_controller(_spec())
    assert tuple(bundle.app.registered_functions) == ("controller",)


def test_installed_distribution_metadata_has_exact_runtime_dependencies() -> None:
    package = metadata("tetrabench")
    requirements = package.get_all("Requires-Dist") or []
    assert version("tetrabench") == "0.1.0"
    assert "harbor[modal]==0.22.0" in requirements
    assert "modal==1.5.4" in requirements


def test_deploy_ensures_environment_before_app_deploy() -> None:
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

    deploy_controller(_spec(), modal_module=fake)

    environment_index = next(
        index for index, item in enumerate(fake.operations) if item[0] == "environment"
    )
    deploy_index = next(
        index for index, item in enumerate(fake.operations) if item[0] == "deploy"
    )
    assert environment_index < deploy_index
    assert fake.operations[environment_index][1][1] == {
        "create_if_missing": True,
        "client": client,
    }
    assert fake.operations[environment_index + 1] == ("hydrate", client)


def test_environment_creation_failure_prevents_deploy() -> None:
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

    with pytest.raises(modal.exception.AuthError, match="not authenticated"):
        deploy_controller(_spec(), modal_module=fake)

    assert not any(item[0] == "deploy" for item in fake.operations)


def test_controller_info_is_no_cloud_and_json_lists_exact_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        "tetrabench.cli.deploy_controller",
        lambda _spec: pytest.fail("info attempted deployment"),
    )
    result = runner.invoke(app, ["controller", "info", "--json"])
    assert result.exit_code == 0
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report["environment_name"] == "tetrabench-default"
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
    monkeypatch.setattr("tetrabench.cli.deploy_controller", calls.append)
    result = runner.invoke(app, ["controller", "deploy", "--yes", "--json"])
    assert result.exit_code == 0
    assert len(calls) == 1
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report["deployed"] is True


def test_controller_deploy_json_without_yes_is_dry() -> None:
    result = runner.invoke(app, ["controller", "deploy", "--json"])
    assert result.exit_code == 2
    report = loads_canonical_json(result.stderr.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert "requires --yes" in str(report["error"])
