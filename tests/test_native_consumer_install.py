"""Installer failure boundaries; no downloads run inside pytest."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).parents[1] / "tools/install_native_consumers.py"
SPEC = importlib.util.spec_from_file_location("install_native_consumers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_installer_strips_credentials_and_bounds_download_process(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-forward")
    monkeypatch.setenv("NPM_TOKEN", "synthetic-do-not-forward")
    monkeypatch.setenv("NPM_CONFIG_USERCONFIG", "/operator/private/npmrc")
    execute = Mock()
    monkeypatch.setattr(installer.subprocess, "run", execute)
    prefix = tmp_path / "consumers"
    installer.install(prefix)
    execute.assert_called_once()
    argv = execute.call_args.args[0]
    kwargs = execute.call_args.kwargs
    assert argv[:2] == ["npm", "ci"]
    assert "--ignore-scripts" in argv
    assert kwargs["timeout"] == 300
    assert kwargs["check"] is True
    assert kwargs["cwd"] == prefix
    environment = kwargs["env"]
    assert "synthetic-do-not-forward" not in str(environment)
    assert "NPM_TOKEN" not in environment
    assert environment["HOME"] == str(prefix / "home")
    assert environment["NPM_CONFIG_USERCONFIG"].startswith(str(prefix))
    assert prefix.stat().st_mode & 0o777 == 0o700


def test_installer_never_removes_an_existing_installation(tmp_path, monkeypatch):
    prefix = tmp_path / "consumers"
    prefix.mkdir()
    sentinel = prefix / "keep"
    sentinel.write_text("existing installation")
    execute = Mock()
    monkeypatch.setattr(installer.subprocess, "run", execute)
    with pytest.raises(FileExistsError):
        installer.install(prefix)
    execute.assert_not_called()
    assert sentinel.read_text() == "existing installation"


def test_installer_refuses_checkout_before_npm(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(installer.subprocess, "run", execute)
    with pytest.raises(ValueError, match="outside the checkout"):
        installer.install(SCRIPT.parent / "must-not-create")
    execute.assert_not_called()


def test_installer_refuses_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.platform, "machine", lambda: "aarch64")
    execute = Mock()
    monkeypatch.setattr(installer.subprocess, "run", execute)
    with pytest.raises(ValueError, match="Linux x86-64"):
        installer.install(tmp_path / "consumers")
    execute.assert_not_called()
