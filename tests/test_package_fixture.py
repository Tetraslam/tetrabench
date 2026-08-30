from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path


def test_fixture_is_source_only_and_absent_from_installed_wheel(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    source = next(tmp_path.glob("*.tar.gz"))
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
        assert not any("authority-fencing" in name for name in names)
        assert not any(
            name.endswith("tests/test_authority_fencing_task.py") for name in names
        )
        assert not any(
            name.endswith("tools/run_authority_fencing_admission.py") for name in names
        )
