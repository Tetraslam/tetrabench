from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tetrabench.authoring import create_task, initialize_project
from tetrabench.catalog import load_catalog
from tetrabench.categories import create_category
from tetrabench.cli import app
from tetrabench.integrity import ArtifactVerificationReport


def test_category_create_cli_then_task_authoring(tmp_path):
    root = initialize_project(tmp_path / "project")
    documentation = root / "benchmarks" / "new.md"
    documentation.write_text("# New category\n")
    result = CliRunner().invoke(
        app,
        [
            "category-create",
            "science",
            "--readme",
            "new.md",
            "--project",
            str(root),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"status":"created"' in result.stdout
    path, fixture = create_task(root, "science", "simple-task")
    assert path.is_dir()
    assert fixture == "benchmarks/tasks/science/simple-task"
    catalog = load_catalog(root, "benchmarks/catalog.toml")
    assert catalog.sections["science"].tasks == []
    assert catalog.sections["example"].tasks[0].id == "hello-tetrabench"


def test_category_mutation_preserves_duplicates_and_uses_existing_lock(tmp_path):
    root = initialize_project(tmp_path / "project")
    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(create_category, root, name, "example/README.md")
            for name in ("one", "two")
        ]
        for future in futures:
            future.result()
    before = (root / "benchmarks/catalog.toml").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        create_category(root, "one", "example/README.md")
    assert (root / "benchmarks/catalog.toml").read_bytes() == before
    assert set(load_catalog(root, "benchmarks/catalog.toml").sections) == {
        "one",
        "two",
        "example",
    }


def test_category_does_not_follow_documentation_symlink(tmp_path):
    root = initialize_project(tmp_path / "project")
    (root / "benchmarks/link.md").symlink_to(root / "benchmarks/example/README.md")
    before = (root / "benchmarks/catalog.toml").read_bytes()
    with pytest.raises(ValueError, match="symlink"):
        create_category(root, "blocked", "link.md")
    assert (root / "benchmarks/catalog.toml").read_bytes() == before


@pytest.mark.parametrize(
    ("state", "exit_code"), [("verified", 0), ("failed", 3), ("refused", 3)]
)
def test_verify_cli_uses_audit_service(monkeypatch, tmp_path, state, exit_code):
    calls = []

    class Service:
        def verify(self, run_id):
            calls.append(run_id)
            return ArtifactVerificationReport(run_id=run_id, state=state)

    monkeypatch.setattr(
        "tetrabench.cli.RunReferenceStore",
        lambda: type("References", (), {"read": lambda self, run: None})(),
    )
    monkeypatch.setattr(
        "tetrabench.engines.modal.legacy_verification_service",
        lambda profile: Service(),
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["artifacts", "verify", "one", "--json"])
    assert result.exit_code == exit_code, result.output
    assert calls == ["one"]
    assert f'"state":"{state}"' in result.stdout
    assert not list(Path(tmp_path).iterdir())


@pytest.mark.parametrize("state", ["failed", "refused"])
def test_human_audit_details_match_for_recorded_and_legacy_routes(monkeypatch, state):
    from types import SimpleNamespace

    from tetrabench.models import ResolvedAwsStorageConfig
    from tetrabench.records import ContentObject
    from tetrabench.run_reference import RunReference

    missing = ContentObject(
        sha256="a" * 64,
        key="tenant/objects/sha256/" + "a" * 64,
        size=11,
        media_type="application/json",
    )
    corrupt = ContentObject(
        sha256="b" * 64,
        key="tenant/objects/sha256/" + "b" * 64,
        size=12,
        media_type="application/json",
    )
    report = ArtifactVerificationReport(
        run_id="one",
        state=state,
        objects_total=3,
        objects_verified=1,
        bytes_total=39,
        bytes_verified=16,
        missing=(missing,),
        corrupt=(corrupt,),
        reasons=("terminal changed [not markup]",),
    )
    reference = RunReference(
        run_id="one",
        engine="modal",
        request_sha256="c" * 64,
        storage=ResolvedAwsStorageConfig(
            provider="aws", bucket="bucket", region="us-east-1"
        ),
        app_name="app",
        function_name="controller",
        environment_name="env",
    )
    service = SimpleNamespace(verify=lambda run_id: report)
    monkeypatch.setattr("tetrabench.cli.get_engine", lambda name: service)
    monkeypatch.setattr(
        "tetrabench.engines.modal.legacy_verification_service", lambda profile: service
    )
    outputs = []
    for route in (reference, None):
        monkeypatch.setattr(
            "tetrabench.cli.RunReferenceStore",
            lambda route=route: SimpleNamespace(read=lambda run_id: route),
        )
        result = CliRunner().invoke(app, ["artifacts", "verify", "one"])
        assert result.exit_code == 3, result.output
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert "Missing: 1; corrupt: 1" in outputs[0]
    assert "16/39 bytes" in outputs[0]
    for descriptor in (missing, corrupt):
        assert descriptor.key in outputs[0]
        assert f"size={descriptor.size}" in outputs[0]
        assert f"sha256={descriptor.sha256}" in outputs[0]
    assert "Reason: terminal changed [not markup]" in outputs[0]
