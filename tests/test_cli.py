from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from importlib.metadata import entry_points, version
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from botocore.exceptions import ClientError, CredentialRetrievalError
from modal.exception import Error as ModalError
from typer.testing import CliRunner

from tetrabench.artifacts import ArtifactPullRefusedError, ArtifactPullResult
from tetrabench.canonical_json import loads_canonical_json
from tetrabench.cli import app
from tetrabench.controller_runtime import HarborRunResult
from tetrabench.lifecycle import CancellationResult, RecoveryResult, RunStatus
from tetrabench.remote import (
    MalformedRemoteKey,
    RemoteResult,
    RemoteRunsReport,
)
from tetrabench.s3 import S3IntegrityError, S3Store

ROOT = Path(__file__).parents[1]
runner = CliRunner()


def _local_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    task = project / "tasks/fixture"
    task.parent.mkdir(parents=True)
    shutil.copytree(ROOT / "tests/fixtures/harbor_task", task)
    (project / "benchmarks").mkdir()
    (project / "benchmarks/catalog.toml").write_text(
        """\
schema_version = 1
[sections.systems-design]
readme = "systems.md"
tasks = [{ id = "fixture", harbor_task = "tasks/fixture" }]
[sections.github-workflow]
readme = "github.md"
tasks = []
""",
        encoding="utf-8",
    )
    (project / "benchmarks/systems.md").write_text("systems", encoding="utf-8")
    (project / "benchmarks/github.md").write_text("github", encoding="utf-8")
    (project / "tetrabench.toml").write_text(
        """\
schema_version = 1
catalog_path = "benchmarks/catalog.toml"
[controller]
kind = "modal"
[execution]
kind = "modal"
""",
        encoding="utf-8",
    )
    user_path = tmp_path / "config.toml"
    user_path.write_text(
        """\
schema_version = 1
[profiles.local.controller]
kind = "local"
[profiles.local.execution]
kind = "docker"
[profiles.cloud]
[profiles.mixed.execution]
kind = "docker"
""",
        encoding="utf-8",
    )
    return project, user_path


class _DoctorClient:
    def __init__(
        self,
        head_error: ClientError | None = None,
        *,
        location: object = "iad",
    ) -> None:
        self.head_error = head_error
        self.location = location
        self.operations: list[tuple[str, dict[str, Any]]] = []

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.operations.append(("head_bucket", kwargs))
        if self.head_error is not None:
            raise self.head_error
        return {}

    def get_bucket_location(self, **kwargs: Any) -> dict[str, Any]:
        self.operations.append(("get_bucket_location", kwargs))
        return {"LocationConstraint": self.location}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.operations.append(("list_objects_v2", kwargs))
        return {"Contents": (), "IsTruncated": False}

    def put_object(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("doctor must not call put_object")

    def upload_fileobj(self, **_kwargs: Any) -> None:
        raise AssertionError("doctor must not call upload_fileobj")

    def head_object(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("doctor must not call head_object")

    def get_object(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("doctor must not call get_object")


def _profile_file(tmp_path: Path, provider: str) -> Path:
    region = '\nregion = "us-west-2"' if provider == "aws" else ""
    path = tmp_path / "user.toml"
    path.write_text(
        f'''\
schema_version = 1
[profiles.online.storage]
provider = "{provider}"
bucket = "doctor-bucket"
prefix = "tenant/v1"{region}
''',
        encoding="utf-8",
    )
    return path


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "test error"}},
        "HeadBucket",
    )


def _adversarial_client_error() -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "CredentialCode-AWS_ACCESS_KEY_ID",
                "Message": "AWS_SECRET_ACCESS_KEY=client-error-secret",
            }
        },
        "GetObject-TIGRIS_SECRET_ACCESS_KEY",
    )


def _adversarial_botocore_error() -> CredentialRetrievalError:
    return CredentialRetrievalError(
        provider="AWS_ACCESS_KEY_ID",
        error_msg="AWS_SECRET_ACCESS_KEY=botocore-error-secret",
    )


def _adversarial_modal_error() -> ModalError:
    return ModalError("MODAL_TOKEN_SECRET=modal-error-secret")


def test_version_and_installed_entrypoint() -> None:
    result = runner.invoke(app, ["--version"])
    scripts = {entry.name: entry for entry in entry_points(group="console_scripts")}

    assert result.exit_code == 0
    assert result.stdout == "0.1.0\n"
    assert scripts["tetrabench"].value == "tetrabench.cli:main"


def test_pinned_runtime_versions_are_installed() -> None:
    assert version("harbor") == "0.22.0"
    assert version("modal") == "1.5.4"
    assert version("pydantic") == "2.13.5"
    assert version("rfc8785") == "0.1.4"


def test_sections_human_output(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = runner.invoke(app, ["sections"])

    assert result.exit_code == 0
    assert "systems-design" in result.stdout
    assert "github-workflow" in result.stdout
    assert result.stderr == ""


def test_plan_json_uses_stable_stdout(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    first = runner.invoke(app, ["plan", "systems-design", "--json"])
    second = runner.invoke(app, ["plan", "systems-design", "--json"])

    assert first.exit_code == 0
    assert first.stdout == second.stdout
    assert first.stdout.startswith('{"context":[]')
    assert first.stdout.endswith("\n")
    assert first.stderr == ""


def test_plan_human_output_calls_empty_section_not_runnable(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = runner.invoke(app, ["plan", "systems-design"])

    assert result.exit_code == 0
    assert "Trials: 0" in result.stdout
    assert "Not runnable:" in result.stdout


@pytest.mark.parametrize("profile", ["cloud", "mixed"])
def test_run_rejects_nonlocal_profile_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"
    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr(
        "tetrabench.local_execution.HarborRunner",
        lambda: pytest.fail("rejected profile constructed HarborRunner"),
    )
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda _config: pytest.fail("run constructed an S3 client"),
    )

    result = runner.invoke(
        app,
        ["run", "systems-design", "--profile", profile, "--output", str(output)],
    )

    assert result.exit_code == 2
    assert not output.exists()
    assert result.stdout == ""


def test_run_never_overwrites_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "prior-run"
    sentinel.write_text("preserve", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr(
        "tetrabench.local_execution.HarborRunner",
        lambda: pytest.fail("existing output constructed HarborRunner"),
    )

    result = runner.invoke(
        app,
        ["run", "systems-design", "--profile", "local", "--output", str(output)],
    )

    assert result.exit_code == 2
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_run_invalid_task_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"
    (project / "tasks/fixture/instruction.md").unlink()
    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)

    result = runner.invoke(
        app,
        ["run", "systems-design", "--profile", "local", "--output", str(output)],
    )

    assert result.exit_code == 2
    assert "missing instruction.md" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("process_umask", [0o000, 0o777])
def test_run_retained_output_has_exact_private_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_umask: int,
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"

    class Runner:
        @staticmethod
        def validate_tasks(_request, _root) -> None:
            return None

        @staticmethod
        def run(_request, paths, **_kwargs) -> HarborRunResult:
            job = paths.jobs / "harbor-job"
            job.mkdir()
            job.chmod(0o700)
            return HarborRunResult(
                outcome="succeeded",
                reward="1",
                job_directory=job,
            )

    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)
    previous_umask = os.umask(process_umask)
    try:
        result = runner.invoke(
            app,
            ["run", "systems-design", "--profile", "local", "--output", str(output)],
        )
    finally:
        os.umask(previous_umask)

    assert result.exit_code == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o700


def test_run_failure_after_reservation_retains_private_empty_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"

    class Runner:
        @staticmethod
        def validate_tasks(_request, _root) -> None:
            return None

        @staticmethod
        def run(_request, _paths, **_kwargs):
            raise ValueError("failed before Harbor execution")

    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)

    result = runner.invoke(
        app,
        ["run", "systems-design", "--profile", "local", "--output", str(output)],
    )

    assert result.exit_code == 2
    assert "failed before Harbor execution" in result.stderr
    assert output.is_dir()
    assert list(output.iterdir()) == []
    assert stat.S_IMODE(output.stat().st_mode) == 0o700


def test_run_failure_after_reservation_retains_private_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"

    class Runner:
        @staticmethod
        def validate_tasks(_request, _root) -> None:
            return None

        @staticmethod
        def run(_request, paths, **_kwargs):
            injected = paths.root / "concurrent-content"
            injected.write_text("preserve", encoding="utf-8")
            raise ValueError("original pre-job failure")

    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)

    result = runner.invoke(
        app,
        ["run", "systems-design", "--profile", "local", "--output", str(output)],
    )

    assert result.exit_code == 2
    assert "original pre-job failure" in result.stderr
    assert (output / "concurrent-content").read_text(encoding="utf-8") == "preserve"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700


def test_run_failure_never_deletes_replacement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"
    moved_reservation = tmp_path / "moved-reservation"

    class Runner:
        @staticmethod
        def validate_tasks(_request, _root) -> None:
            return None

        @staticmethod
        def run(_request, paths, **_kwargs):
            paths.root.rename(moved_reservation)
            paths.root.mkdir(mode=0o750)
            (paths.root / "replacement-content").write_text(
                "preserve replacement", encoding="utf-8"
            )
            raise ValueError("failure after replacement race")

    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)

    result = runner.invoke(
        app,
        ["run", "systems-design", "--profile", "local", "--output", str(output)],
    )

    assert result.exit_code == 2
    assert "failure after replacement race" in result.stderr
    assert stat.S_IMODE(moved_reservation.stat().st_mode) == 0o700
    assert (output / "replacement-content").read_text(encoding="utf-8") == (
        "preserve replacement"
    )


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_run_propagates_unsuccessful_harbor_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"

    class Runner:
        @staticmethod
        def validate_tasks(_request, _root) -> None:
            return None

        @staticmethod
        def run(_request, paths, **_kwargs) -> HarborRunResult:
            job = paths.jobs / "harbor-job"
            job.mkdir()
            return HarborRunResult(
                outcome=cast(Literal["failed", "cancelled"], outcome),
                reward="0.25",
                job_directory=job,
            )

    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)

    result = runner.invoke(
        app,
        [
            "run",
            "systems-design",
            "--profile",
            "local",
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert result.stderr == ""
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report["outcome"] == outcome
    assert report["reward"] == "0.25"
    assert report["job_directory"] == str(output / "harbor-job")


def test_run_interrupt_preserves_visible_native_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"

    class Runner:
        @staticmethod
        def validate_tasks(_request, _root) -> None:
            return None

        @staticmethod
        def run(_request, paths, **_kwargs):
            job = paths.jobs / "harbor-job"
            job.mkdir()
            (job / "config.json").write_text("partial", encoding="utf-8")
            raise KeyboardInterrupt

    monkeypatch.chdir(project)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr("tetrabench.local_execution.HarborRunner", Runner)

    result = runner.invoke(
        app,
        [
            "run",
            "systems-design",
            "--profile",
            "local",
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 130
    assert result.stdout == ""
    report = loads_canonical_json(result.stderr.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report == {
        "evidence_path": str(output / "harbor-job"),
        "schema_version": 1,
        "status": "interrupted",
    }
    assert (output / "harbor-job/config.json").read_text() == "partial"


@pytest.mark.docker
def test_run_real_harbor_docker_from_temporary_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert docker.returncode == 0, "Docker daemon is required for the test suite"
    project, user_path = _local_project(tmp_path)
    output = tmp_path / "output"
    monkeypatch.chdir(project)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_ACCOUNT_ID",
        "aws_alternate_credential",
        "AwS_Mixed_Credential",
        "TIGRIS_SECRET_ACCESS_KEY",
        "tigris_alternate_credential",
        "TiGrIs_Mixed_Credential",
        "bOtO_cOnFiG",
        "bOtOcOrE_TcP_KeEpAlIvE",
    ):
        monkeypatch.setenv(name, f"local-controller-secret-{name.lower()}")
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda _config: pytest.fail("local run constructed an S3 client"),
    )

    class ForbiddenModal:
        def __init__(self, *_args, **_kwargs) -> None:
            pytest.fail("local run constructed a Modal client")

    monkeypatch.setattr("tetrabench.cli.ModalControllerClient", ForbiddenModal)

    result = runner.invoke(
        app,
        [
            "run",
            "systems-design",
            "--profile",
            "local",
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report == {
        "job_directory": str(output / "harbor-job"),
        "outcome": "succeeded",
        "reward": "1.0",
        "schema_version": 1,
    }
    assert (output / "harbor-job/config.json").is_file()
    assert (output / "harbor-job/lock.json").is_file()
    assert (output / "harbor-job/result.json").is_file()


def test_error_is_on_stderr(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sections"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "error:" in result.stderr


def test_doctor_does_not_attempt_provider_calls(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda _config: pytest.fail("offline doctor constructed a provider client"),
    )
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "not attempted cloud controller checks" in result.stdout
    assert "not attempted storage provider checks (offline)" in result.stdout
    assert "unproven storage writes (not attempted)" in result.stdout
    assert result.stderr == ""


def test_doctor_offline_json_is_canonical_without_provider_client(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda _config: pytest.fail("offline doctor constructed a provider client"),
    )

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode("utf-8"))
    assert isinstance(report, dict)
    assert report["mode"] == "offline"
    checks = report["checks"]
    assert isinstance(checks, list)
    assert checks[-4:] == [
        {"name": "storage_bucket", "status": "not_attempted"},
        {"name": "storage_prefix", "status": "not_attempted"},
        {"name": "admission_coordination", "status": "not_attempted"},
        {"name": "storage_writes", "status": "unproven"},
    ]


@pytest.mark.parametrize(
    ("provider", "display"),
    [("aws", "AWS"), ("tigris", "Tigris")],
)
def test_doctor_online_checks_selected_provider_without_mutation(
    tmp_path: Path,
    monkeypatch,
    provider: str,
    display: str,
) -> None:
    user_path = _profile_file(tmp_path, provider)
    client = _DoctorClient(location="us-west-2" if provider == "aws" else "iad")
    selected_configs: list[Any] = []

    def make_store(config) -> S3Store:
        selected_configs.append(config)
        return S3Store(config, client)

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr("tetrabench.cli.create_s3_store", make_store)

    result = runner.invoke(app, ["doctor", "--profile", "online", "--online"])

    assert result.exit_code == 0
    assert f"ok {display} bucket read access: doctor-bucket" in result.stdout
    assert (
        f"ok {display} prefix list access: s3://doctor-bucket/tenant/v1"
        in result.stdout
    )
    assert "unproven storage writes (not attempted)" in result.stdout
    assert "bucket location" in result.stdout
    assert "safe mutable admission coordination" in result.stdout
    assert result.stderr == ""
    assert len(selected_configs) == 1
    assert selected_configs[0].provider == provider
    assert client.operations == [
        ("head_bucket", {"Bucket": "doctor-bucket"}),
        ("get_bucket_location", {"Bucket": "doctor-bucket"}),
        (
            "list_objects_v2",
            {"Bucket": "doctor-bucket", "Prefix": "tenant/v1/", "MaxKeys": 1},
        ),
    ]


def test_doctor_json_is_canonical_machine_output(tmp_path: Path, monkeypatch) -> None:
    user_path = _profile_file(tmp_path, "tigris")
    client = _DoctorClient()
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda config: S3Store(config, client),
    )

    result = runner.invoke(
        app,
        ["doctor", "--profile", "online", "--online", "--json"],
    )

    assert result.exit_code == 0
    document = result.stdout.removesuffix("\n").encode("utf-8")
    report = loads_canonical_json(document)
    assert isinstance(report, dict)
    assert report["mode"] == "online"
    assert report["profile"] == "online"
    assert report["storage"] == {
        "admission_safe": True,
        "bucket": "doctor-bucket",
        "bucket_location": "iad",
        "location_type": "tigris-single-region",
        "prefix": "tenant/v1",
        "provider": "tigris",
        "provider_display": "Tigris",
    }
    checks = report["checks"]
    assert isinstance(checks, list)
    assert checks[-1] == {
        "name": "storage_writes",
        "status": "unproven",
    }
    assert checks[-2] == {"name": "admission_coordination", "status": "ok"}
    assert result.stdout.endswith("\n")
    assert result.stderr == ""


def test_doctor_online_reports_global_bucket_as_readable_but_admission_unsafe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_path = _profile_file(tmp_path, "tigris")
    client = _DoctorClient(location="global")
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda config: S3Store(config, client),
    )

    result = runner.invoke(
        app,
        ["doctor", "--profile", "online", "--online", "--json"],
    )

    assert result.exit_code == 0
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    storage = report["storage"]
    checks = report["checks"]
    assert isinstance(storage, dict)
    assert isinstance(checks, list)
    assert storage["bucket_location"] == "global"
    assert storage["admission_safe"] is False
    statuses: dict[str, object] = {}
    for item in checks:
        assert isinstance(item, dict)
        name = item["name"]
        assert isinstance(name, str)
        statuses[name] = item["status"]
    assert statuses["admission_coordination"] == "unsafe"
    assert not any(name.startswith("put") for name, _kwargs in client.operations)


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket"])
def test_doctor_online_redacts_provider_errors_on_stderr(
    tmp_path: Path,
    monkeypatch,
    code: str,
) -> None:
    user_path = _profile_file(tmp_path, "tigris")
    client = _DoctorClient(_client_error(code))
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda config: S3Store(config, client),
    )

    result = runner.invoke(app, ["doctor", "--profile", "online", "--online"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "error: provider request failed (provider_error)" in result.stderr
    assert code not in result.stderr
    assert "test error" not in result.stderr
    assert "storage writes; no mutation attempted" in result.stderr
    assert client.operations == [("head_bucket", {"Bucket": "doctor-bucket"})]


def test_doctor_json_error_is_canonical_stderr(tmp_path: Path, monkeypatch) -> None:
    user_path = _profile_file(tmp_path, "aws")
    client = _DoctorClient(_client_error("InvalidAccessKeyId"))
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda config: S3Store(config, client),
    )

    result = runner.invoke(
        app,
        ["doctor", "--profile", "online", "--online", "--json"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    report = loads_canonical_json(result.stderr.removesuffix("\n").encode("utf-8"))
    assert report == {
        "error": "provider request failed",
        "error_type": "provider_error",
        "mutation_attempted": False,
        "schema_version": 1,
        "storage_writes": "unproven",
    }


def test_doctor_checks_selected_profile_against_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "tetrabench.toml").write_text(
        """\
schema_version = 1
catalog_path = "catalog.toml"
""",
        encoding="utf-8",
    )
    (tmp_path / "catalog.toml").write_text(
        """\
schema_version = 1
[sections.systems-design]
readme = "systems.md"
tasks = []
[sections.github-workflow]
readme = "github.md"
tasks = []
""",
        encoding="utf-8",
    )
    (tmp_path / "systems.md").write_text("systems", encoding="utf-8")
    (tmp_path / "github.md").write_text("github", encoding="utf-8")
    user_path = tmp_path / "user.toml"
    user_path.write_text(
        """\
schema_version = 1
[profiles.bad.selection]
exclude = ["missing"]
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("tetrabench.config.default_user_config_path", lambda: user_path)

    result = runner.invoke(app, ["doctor", "--profile", "bad"])

    assert result.exit_code == 2
    assert "absent from catalog: missing" in result.stderr


def test_submit_empty_section_has_zero_s3_or_modal_side_effects(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        "tetrabench.cli.create_s3_store",
        lambda _config: pytest.fail("empty submit constructed S3 store"),
    )

    class ForbiddenModal:
        def __init__(self, *_args) -> None:
            pytest.fail("empty submit constructed Modal adapter")

    monkeypatch.setattr("tetrabench.cli.ModalControllerClient", ForbiddenModal)
    result = runner.invoke(app, ["submit", "systems-design"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "plan is not runnable" in result.stderr


def test_submit_empty_section_json_error_is_canonical_stderr(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = runner.invoke(app, ["submit", "systems-design", "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    report = loads_canonical_json(result.stderr.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report["schema_version"] == 1
    assert "not runnable" in str(report["error"])


@pytest.mark.parametrize(
    ("operation", "arguments", "error_factory"),
    [
        pytest.param(
            "doctor",
            ["doctor", "--profile", "online", "--online"],
            error_factory,
            id=f"doctor-{error_id}",
        )
        for error_id, error_factory in (
            ("client-error", _adversarial_client_error),
            ("botocore-error", _adversarial_botocore_error),
        )
    ]
    + [
        pytest.param(
            "controller_deploy",
            ["controller", "deploy", "--yes"],
            _adversarial_modal_error,
            id="controller-deploy-modal-error",
        )
    ]
    + [
        pytest.param(operation, arguments, error_factory, id=f"{operation}-{error_id}")
        for operation, arguments in (
            ("submit", ["submit", "systems-design"]),
            ("recover", ["recover", "run-1", "--yes"]),
            ("status", ["status", "run-1"]),
            ("cancel", ["cancel", "run-1", "--yes"]),
        )
        for error_id, error_factory in (
            ("client-error", _adversarial_client_error),
            ("botocore-error", _adversarial_botocore_error),
            ("modal-error", _adversarial_modal_error),
        )
    ]
    + [
        pytest.param(operation, arguments, error_factory, id=f"{operation}-{error_id}")
        for operation, arguments in (
            ("result", ["result", "run-1", "--profile", "remote"]),
            (
                "artifacts_pull",
                ["artifacts", "pull", "run-1", "OUTPUT", "--profile", "remote"],
            ),
            ("runs_remote", ["runs", "--remote", "--profile", "remote"]),
        )
        for error_id, error_factory in (
            ("client-error", _adversarial_client_error),
            ("botocore-error", _adversarial_botocore_error),
        )
    ],
)
@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
def test_remote_commands_redact_provider_exception_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    arguments: list[str],
    error_factory: Callable[[], Exception],
    json_output: bool,
) -> None:
    provider_error = error_factory()
    provider_error.__cause__ = RuntimeError("CHAINED_PROVIDER_CAUSE=chained-secret")

    def raise_provider_error(*_args, **_kwargs):
        raise provider_error

    class Service:
        submit = staticmethod(raise_provider_error)
        recover = staticmethod(raise_provider_error)
        status = staticmethod(raise_provider_error)
        cancel = staticmethod(raise_provider_error)
        result = staticmethod(raise_provider_error)
        pull = staticmethod(raise_provider_error)
        runs = staticmethod(raise_provider_error)

    if operation == "doctor":
        user_path = _profile_file(tmp_path, "aws")
        monkeypatch.chdir(ROOT)
        monkeypatch.setattr(
            "tetrabench.config.default_user_config_path", lambda: user_path
        )
        monkeypatch.setattr("tetrabench.cli.create_s3_store", raise_provider_error)
    elif operation == "controller_deploy":
        monkeypatch.chdir(ROOT)
        monkeypatch.setattr("tetrabench.cli.deploy_controller", raise_provider_error)
    elif operation == "submit":
        monkeypatch.setattr("tetrabench.cli.prepare_submission", raise_provider_error)
    elif operation == "recover":
        monkeypatch.setattr(
            "tetrabench.cli._recovery_service", lambda _profile: Service()
        )
    elif operation == "status":
        monkeypatch.setattr(
            "tetrabench.cli._status_service", lambda _profile: Service()
        )
    elif operation == "cancel":
        monkeypatch.setattr(
            "tetrabench.cli._cancellation_service", lambda _profile: Service()
        )
    elif operation in {"result", "runs_remote"}:
        monkeypatch.setattr(
            "tetrabench.cli._remote_result_service", lambda _profile: Service()
        )
    else:

        class Config:
            storage = object()

        monkeypatch.setattr(
            "tetrabench.cli.load_project_config", lambda *_args, **_kwargs: Config()
        )
        monkeypatch.setattr("tetrabench.cli.create_s3_store", lambda _storage: object())
        monkeypatch.setattr(
            "tetrabench.cli.ArtifactPullService", lambda _store: Service()
        )
        arguments = [
            str(tmp_path / "output") if item == "OUTPUT" else item for item in arguments
        ]

    if json_output:
        arguments = [*arguments, "--json"]
    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert result.stdout == ""
    for raw_field in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "TIGRIS_SECRET_ACCESS_KEY",
        "MODAL_TOKEN_SECRET",
        "CHAINED_PROVIDER_CAUSE",
        "secret",
        "args",
        "cause",
        "context",
        "message",
        "traceback",
    ):
        assert raw_field not in result.stderr
    if json_output:
        expected = {
            "error": "provider request failed",
            "error_type": "provider_error",
            "schema_version": 1,
        }
        if operation == "doctor":
            expected |= {
                "mutation_attempted": False,
                "storage_writes": "unproven",
            }
        assert (
            loads_canonical_json(result.stderr.removesuffix("\n").encode()) == expected
        )
    else:
        expected = "error: provider request failed (provider_error)\n"
        if operation == "doctor":
            expected += "unproven: storage writes; no mutation attempted\n"
        assert result.stderr == expected


@pytest.mark.parametrize(
    "error",
    [
        S3IntegrityError("locally validated terminal binding failed"),
        ValueError("local profile requires storage configuration"),
    ],
    ids=["integrity", "configuration"],
)
def test_remote_command_preserves_precise_local_error(
    monkeypatch, error: Exception
) -> None:
    class Service:
        @staticmethod
        def result(_run_id: str) -> RemoteResult:
            raise error

    monkeypatch.setattr(
        "tetrabench.cli._remote_result_service", lambda _profile: Service()
    )
    result = runner.invoke(app, ["result", "run-1", "--profile", "remote", "--json"])

    assert result.exit_code == 2
    assert loads_canonical_json(result.stderr.removesuffix("\n").encode()) == {
        "error": str(error),
        "schema_version": 1,
    }


def test_status_json_conflict_uses_stdout_and_exit_three(monkeypatch) -> None:
    class Service:
        @staticmethod
        def status(run_id: str) -> RunStatus:
            return RunStatus(
                run_id=run_id,
                state="conflict",
                detail="multiple terminals",
            )

    monkeypatch.setattr("tetrabench.cli._status_service", lambda _profile: Service())
    result = runner.invoke(app, ["status", "run-1", "--json"])
    assert result.exit_code == 3
    assert result.stderr == ""
    report = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(report, dict)
    assert report["state"] == "conflict"


def test_running_cancel_refuses_before_cas_without_real_child_observer(
    monkeypatch,
) -> None:
    from tetrabench.lifecycle import CancellationUnavailableError

    class Service:
        @staticmethod
        def cancel(_run_id: str):
            raise CancellationUnavailableError(
                "running cancellation requires the deployed Harbor child observer; "
                "admission was not changed"
            )

    monkeypatch.setattr(
        "tetrabench.cli._cancellation_service", lambda _profile: Service()
    )
    result = runner.invoke(app, ["cancel", "run-1", "--yes"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "child observer" in result.stderr
    assert "admission was not changed" in " ".join(result.stderr.split())


def test_runs_json_is_canonical_local_receipt_cache(monkeypatch) -> None:
    class Store:
        @staticmethod
        def list() -> tuple[()]:
            return ()

    monkeypatch.setattr("tetrabench.cli.ReceiptStore", Store)
    result = runner.invoke(app, ["runs", "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    assert loads_canonical_json(result.stdout.removesuffix("\n").encode()) == {
        "receipts": [],
        "schema_version": 1,
    }


@pytest.mark.parametrize(
    ("report", "exit_code"),
    [
        (RemoteResult(run_id="run-1", state="unknown"), 4),
        (
            RemoteResult(
                run_id="run-1",
                state="nonterminal",
                admission_state="running",
            ),
            0,
        ),
        (
            RemoteResult(
                run_id="run-1",
                state="terminal",
                outcome="succeeded",
                reward="1.0",
                terminal_sha256="a" * 64,
            ),
            0,
        ),
        (
            RemoteResult(
                run_id="run-1",
                state="terminal",
                outcome="failed",
                terminal_sha256="a" * 64,
            ),
            1,
        ),
        (
            RemoteResult(
                run_id="run-1",
                state="terminal",
                outcome="cancelled",
                terminal_sha256="a" * 64,
            ),
            1,
        ),
        (
            RemoteResult(
                run_id="run-1",
                state="conflict",
                reasons=("multiple terminals",),
            ),
            3,
        ),
    ],
)
def test_result_json_has_stable_states_and_exits(
    monkeypatch: pytest.MonkeyPatch,
    report: RemoteResult,
    exit_code: int,
) -> None:
    class Service:
        @staticmethod
        def result(_run_id: str) -> RemoteResult:
            return report

    monkeypatch.setattr(
        "tetrabench.cli._remote_result_service", lambda _profile: Service()
    )

    result = runner.invoke(app, ["result", "run-1", "--profile", "fresh", "--json"])

    assert result.exit_code == exit_code
    assert result.stderr == ""
    payload = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(payload, dict)
    assert payload["state"] == report.state
    assert payload["artifacts"] == []


def test_result_human_terminal_shows_outcome_reward_and_inventory(monkeypatch) -> None:
    from tetrabench.remote import RemoteArtifact

    report = RemoteResult(
        run_id="run-1",
        state="terminal",
        outcome="succeeded",
        reward="0.5",
        terminal_sha256="a" * 64,
        artifacts=(
            RemoteArtifact(
                logical_path="job/result.json",
                sha256="b" * 64,
                size=12,
                media_type="application/json",
            ),
        ),
    )

    class Service:
        @staticmethod
        def result(_run_id: str) -> RemoteResult:
            return report

    monkeypatch.setattr(
        "tetrabench.cli._remote_result_service", lambda _profile: Service()
    )

    result = runner.invoke(app, ["result", "run-1", "--profile", "fresh"])

    assert result.exit_code == 0
    assert "Outcome: succeeded" in result.stdout
    assert "Reward: 0.5" in result.stdout
    assert "job/result.json" in result.stdout


def test_runs_remote_surfaces_malformed_keys_and_exits_three(monkeypatch) -> None:
    malformed = MalformedRemoteKey(key="runs/bad", reason="invalid layout")

    class Service:
        @staticmethod
        def runs() -> RemoteRunsReport:
            return RemoteRunsReport(runs=(), malformed_keys=(malformed,))

    monkeypatch.setattr(
        "tetrabench.cli._remote_result_service", lambda _profile: Service()
    )

    result = runner.invoke(app, ["runs", "--remote", "--profile", "fresh", "--json"])

    assert result.exit_code == 3
    assert result.stderr == ""
    payload = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(payload, dict)
    assert payload["malformed_keys"] == [
        {"key": "runs/bad", "reason": "invalid layout"}
    ]


def test_runs_remote_requires_profile() -> None:
    result = runner.invoke(app, ["runs", "--remote", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    payload = loads_canonical_json(result.stderr.removesuffix("\n").encode())
    assert isinstance(payload, dict)
    assert payload["error"] == "runs --remote requires --profile"


def test_artifacts_pull_json_is_canonical(monkeypatch, tmp_path: Path) -> None:
    from tetrabench.remote import RemoteArtifact

    artifact = RemoteArtifact(
        logical_path="job/result.json",
        sha256="a" * 64,
        size=6,
        media_type="application/json",
    )

    class Config:
        storage = object()

    class Service:
        @staticmethod
        def pull(run_id: str, output: Path) -> ArtifactPullResult:
            return ArtifactPullResult(
                run_id=run_id,
                output_directory=str(output.absolute()),
                terminal_sha256="b" * 64,
                artifacts=(artifact,),
            )

    monkeypatch.setattr(
        "tetrabench.cli.load_project_config", lambda *_args, **_kwargs: Config()
    )
    monkeypatch.setattr("tetrabench.cli.create_s3_store", lambda _storage: object())
    monkeypatch.setattr("tetrabench.cli.ArtifactPullService", lambda _store: Service())
    output = tmp_path / "output"

    result = runner.invoke(
        app,
        [
            "artifacts",
            "pull",
            "run-1",
            str(output),
            "--profile",
            "fresh",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(payload, dict)
    assert payload["output_directory"] == str(output.absolute())
    assert payload["artifacts"] == [artifact.model_dump(mode="json")]


def test_artifacts_pull_limit_failure_is_deterministic_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Config:
        storage = object()

    class Service:
        @staticmethod
        def pull(_run_id: str, _output: Path) -> ArtifactPullResult:
            raise ArtifactPullRefusedError("terminal inventory exceeds max_total_bytes")

    monkeypatch.setattr(
        "tetrabench.cli.load_project_config", lambda *_args, **_kwargs: Config()
    )
    monkeypatch.setattr("tetrabench.cli.create_s3_store", lambda _storage: object())
    monkeypatch.setattr("tetrabench.cli.ArtifactPullService", lambda _store: Service())

    result = runner.invoke(
        app,
        [
            "artifacts",
            "pull",
            "run-1",
            str(tmp_path / "output"),
            "--profile",
            "fresh",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert loads_canonical_json(result.stderr.removesuffix("\n").encode()) == {
        "error": "terminal inventory exceeds max_total_bytes",
        "schema_version": 1,
    }


def test_cancel_decline_constructs_no_provider_service(monkeypatch) -> None:
    monkeypatch.setattr(
        "tetrabench.cli._cancellation_service",
        lambda _profile: pytest.fail("decline constructed provider service"),
    )

    result = runner.invoke(app, ["cancel", "run-1"], input="n\n")

    assert result.exit_code == 1
    assert "no cloud mutation attempted" in result.stderr


def test_cancel_json_requires_yes_before_provider_construction(monkeypatch) -> None:
    monkeypatch.setattr(
        "tetrabench.cli._cancellation_service",
        lambda _profile: pytest.fail("JSON refusal constructed provider service"),
    )

    result = runner.invoke(app, ["cancel", "run-1", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert loads_canonical_json(result.stderr.removesuffix("\n").encode()) == {
        "error": "cancel --json requires --yes",
        "schema_version": 1,
    }


def test_cancel_yes_preserves_machine_result_and_exit(monkeypatch) -> None:
    class Service:
        @staticmethod
        def cancel(run_id: str) -> CancellationResult:
            return CancellationResult(
                run_id=run_id,
                state="cancelled",
                controller_terminal_observed=True,
                terminal_proof_observed=False,
                cleanup_complete=True,
                sweeps=2,
            )

    monkeypatch.setattr(
        "tetrabench.cli._cancellation_service", lambda _profile: Service()
    )

    result = runner.invoke(app, ["cancel", "run-1", "--yes", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(payload, dict)
    assert payload["state"] == "cancelled"
    assert payload["cleanup_complete"] is True


def test_recover_requires_confirmation_before_service_call(monkeypatch) -> None:
    called = False

    def service(_profile):
        nonlocal called
        called = True
        raise AssertionError("service must not be constructed")

    monkeypatch.setattr("tetrabench.cli._recovery_service", service)
    result = runner.invoke(app, ["recover", "run-1"], input="n\n")

    assert result.exit_code == 1
    assert not called
    assert "no cloud mutation attempted" in result.stderr


def test_recover_json_requires_yes_without_service_call(monkeypatch) -> None:
    monkeypatch.setattr(
        "tetrabench.cli._recovery_service",
        lambda _profile: (_ for _ in ()).throw(AssertionError("must not construct")),
    )
    result = runner.invoke(app, ["recover", "run-1", "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert loads_canonical_json(result.stderr.removesuffix("\n").encode()) == {
        "error": "recover --json requires --yes",
        "schema_version": 1,
    }


def test_recover_yes_json_emits_canonical_result(monkeypatch) -> None:
    class Service:
        @staticmethod
        def recover(run_id: str) -> RecoveryResult:
            return RecoveryResult(
                run_id=run_id,
                state="spawned",
                prior_owner_function_call_id="fc-old",
                successor_function_call_id="fc-new",
                terminal_proof_observed=False,
                cleanup_complete=True,
                sweeps=2,
                detail="recovered",
            )

    monkeypatch.setattr("tetrabench.cli._recovery_service", lambda _profile: Service())
    result = runner.invoke(app, ["recover", "run-1", "--yes", "--json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = loads_canonical_json(result.stdout.removesuffix("\n").encode())
    assert isinstance(payload, dict)
    assert payload["state"] == "spawned"
    assert payload["successor_function_call_id"] == "fc-new"


@pytest.mark.parametrize("json_output", [False, True])
def test_recover_exits_unsuccessfully_until_cleanup_completes(
    monkeypatch, json_output: bool
) -> None:
    class Service:
        @staticmethod
        def recover(run_id: str) -> RecoveryResult:
            return RecoveryResult(
                run_id=run_id,
                state="terminal",
                prior_owner_function_call_id="fc-old",
                terminal_proof_observed=True,
                cleanup_complete=False,
                sweeps=1,
                detail="terminal proof observed; child cleanup failed",
            )

    monkeypatch.setattr("tetrabench.cli._recovery_service", lambda _profile: Service())
    arguments = ["recover", "run-1", "--yes"]
    if json_output:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 3
    if json_output:
        payload = loads_canonical_json(result.stdout.removesuffix("\n").encode())
        assert isinstance(payload, dict)
        assert payload["state"] == "terminal"
        assert payload["cleanup_complete"] is False
    else:
        assert "child cleanup failed" in result.stdout
