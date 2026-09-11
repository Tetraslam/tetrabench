from __future__ import annotations

import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def distributions(tmp_path_factory):
    output = tmp_path_factory.mktemp("distribution")
    root = Path(__file__).parents[1]
    subprocess.run(
        ["uv", "build", "--out-dir", str(output)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(output.glob("*.whl")), next(output.glob("*.tar.gz"))


def test_fixture_is_source_only_and_absent_from_installed_wheel(distributions) -> None:
    wheel, source = distributions
    with zipfile.ZipFile(wheel) as archive:
        assert not any("fixtures/harbor_task" in name for name in archive.namelist())
        assert not any(
            "fixtures/harbor_authority_task" in name for name in archive.namelist()
        )
        assert not any("benchmarks/catalog.toml" in name for name in archive.namelist())
        assert not any(
            "provider_consistency_probe" in name for name in archive.namelist()
        )
        assert not any("authority-fencing" in name for name in archive.namelist())
    with tarfile.open(source, "r:gz") as archive:
        names = archive.getnames()
        assert any(
            name.endswith("tests/fixtures/harbor_task/task.toml") for name in names
        )
        assert any(
            name.endswith("tests/fixtures/harbor_authority_task/task.toml")
            for name in names
        )
        assert any(
            name.endswith("tests/fixtures/harbor_authority_task/environment/forge.py")
            for name in names
        )
        assert any(
            name.endswith(
                "tests/fixtures/harbor_authority_task/tests/artifact_contract.json"
            )
            for name in names
        )
        assert any(name.endswith("benchmarks/catalog.toml") for name in names)
        assert any(
            name.endswith("benchmarks/tasks/systems-design/authority-fencing/task.toml")
            for name in names
        )
        assert not any(
            name.endswith("tests/test_authority_fencing_task.py") for name in names
        )
        assert not any(
            name.endswith("tests/test_authority_fencing_detached_admission.py")
            for name in names
        )
        assert not any(
            name.endswith("tools/run_authority_fencing_admission.py") for name in names
        )
        assert not any(
            name.endswith("tools/run_authority_fencing_detached_admission.py")
            for name in names
        )
        assert not any(
            name.endswith("tools/run_authority_fencing_calibration.py")
            for name in names
        )
        assert not any(
            name.endswith("tests/test_authority_fencing_calibration.py")
            for name in names
        )


def test_release_metadata_license_and_dependency_lock(distributions) -> None:
    wheel, source = distributions
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = BytesParser().parsebytes(
            archive.read(next(name for name in names if name.endswith("/METADATA")))
        )
        assert metadata["Name"] == "tetrabench"
        assert metadata["Version"] == project["project"]["version"]
        assert metadata["License-Expression"] == "MIT"
        assert metadata["Requires-Python"] == "<3.13,>=3.12"
        assert "Operating System :: POSIX :: Linux" in metadata.get_all(
            "Classifier", []
        )
        assert metadata.get_all("License-File") == ["LICENSE"]
        license_file = next(
            name for name in names if name.endswith("/licenses/LICENSE")
        )
        assert archive.read(license_file) == (root / "LICENSE").read_bytes()
        for name in ("pyproject.toml", "uv.lock"):
            assert (
                archive.read(f"tetrabench/_distribution/{name}")
                == (root / name).read_bytes()
            )
        lock = tomllib.loads(archive.read("tetrabench/_distribution/uv.lock").decode())
        own = next(
            package for package in lock["package"] if package["name"] == "tetrabench"
        )
        assert own["version"] == metadata["Version"]
        assert all(
            name.startswith(
                ("tetrabench/", f"tetrabench-{metadata['Version']}.dist-info/")
            )
            for name in names
        )
    with tarfile.open(source) as archive:
        license_member = next(
            item for item in archive.getmembers() if item.name.endswith("/LICENSE")
        )
        stream = archive.extractfile(license_member)
        assert stream is not None
        assert stream.read() == (root / "LICENSE").read_bytes()


def test_native_test_lock_is_source_only(distributions) -> None:
    wheel, source = distributions
    with zipfile.ZipFile(wheel) as archive:
        assert not any("node_modules/" in name for name in archive.namelist())
        assert not any("native_consumers/" in name for name in archive.namelist())
        assert archive.read("tetrabench/native_reasoning.mjs")
    with tarfile.open(source) as archive:
        names = archive.getnames()
        assert any(
            name.endswith("tools/native_consumers/package-lock.json") for name in names
        )
        assert any(
            name.endswith("tools/native_consumers/package.json") for name in names
        )
        assert any(name.endswith("tools/native_consumers/.npmrc") for name in names)
        assert any(name.endswith("tools/install_native_consumers.py") for name in names)
        assert any(name.endswith("tests/native_consumer_support.py") for name in names)
        assert not any("node_modules/" in name for name in names)
