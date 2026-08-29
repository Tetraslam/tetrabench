from __future__ import annotations

from importlib.metadata import entry_points, version
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from tetrabench.canonical_json import loads_canonical_json
from tetrabench.cli import app
from tetrabench.lifecycle import RunStatus
from tetrabench.s3 import S3Store

ROOT = Path(__file__).parents[1]
runner = CliRunner()


class _DoctorClient:
    def __init__(self, head_error: ClientError | None = None) -> None:
        self.head_error = head_error
        self.operations: list[tuple[str, dict[str, Any]]] = []

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.operations.append(("head_bucket", kwargs))
        if self.head_error is not None:
            raise self.head_error
        return {}

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
    assert checks[-3:] == [
        {"name": "storage_bucket", "status": "not_attempted"},
        {"name": "storage_prefix", "status": "not_attempted"},
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
    client = _DoctorClient()
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
    assert result.stderr == ""
    assert len(selected_configs) == 1
    assert selected_configs[0].provider == provider
    assert client.operations == [
        ("head_bucket", {"Bucket": "doctor-bucket"}),
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
        "bucket": "doctor-bucket",
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
    assert result.stdout.endswith("\n")
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("AccessDenied", "Tigris storage authentication or authorization failed"),
        ("NoSuchBucket", "Tigris storage bucket not found: doctor-bucket"),
    ],
)
def test_doctor_online_reports_auth_and_not_found_on_stderr(
    tmp_path: Path,
    monkeypatch,
    code: str,
    message: str,
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
    assert message in result.stderr
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
    expected_error = "AWS storage authentication or authorization failed"
    expected_error += " (InvalidAccessKeyId)"
    assert report == {
        "error": expected_error,
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
    result = runner.invoke(app, ["cancel", "run-1"])
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
