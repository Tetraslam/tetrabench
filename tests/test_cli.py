from __future__ import annotations

from importlib.metadata import entry_points, version
from pathlib import Path

from typer.testing import CliRunner

from tetrabench.cli import app

ROOT = Path(__file__).parents[1]
runner = CliRunner()


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
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "not attempted cloud controller checks" in result.stdout
    assert "not attempted storage provider checks" in result.stdout


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
