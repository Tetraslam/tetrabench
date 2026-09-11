"""Resource execution preparation is separate from native result reconstruction."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from harbor.models.job.lock import JobLock, TaskLock, TrialLock, VerifierLock
from harbor.models.job.result import JobResult, JobStats
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult

from tetrabench.authoring import initialize_project
from tetrabench.canonical_json import sha256_hex
from tetrabench.context import materialize_sealed_context
from tetrabench.engines.docker import DockerEngine
from tetrabench.harbor import (
    ATTEMPT_LABEL,
    ENVIRONMENT_IMPORT_PATH,
    PLAN_LABEL,
    RUN_LABEL,
)
from tetrabench.harbor_api import Harbor022Api
from tetrabench.harbor_runner import HarborRunner, compile_harbor_job
from tetrabench.harness_config import HarnessConfig, SealedResource
from tetrabench.local_execution import local_paths
from tetrabench.models import ConfigOverrides
from tetrabench.plan import canonical_model_bytes
from tetrabench.resources import AGENT_RESOURCE_ROOT, materialize_resources
from tetrabench.run_reference import RunReference, process_identity
from tetrabench.submission import prepare_run


class SyntheticCompletedHarbor(Harbor022Api):
    def execute(self, config):
        assert (
            Path(config.agents[0].skills[0]) / "SKILL.md"
        ).read_text() == "Skill body\n"
        job = config.jobs_dir / config.job_name
        trial = job / "trial-one"
        (trial / "artifacts").mkdir(parents=True)
        (trial / "artifacts/workspace").mkdir()
        (trial / "artifacts/workspace/answer.txt").write_text("Synthetic output\n")
        trial_config = TrialConfig(
            task=config.tasks[0], agent=config.agents[0], environment=config.environment
        )
        task = config.tasks[0].path
        lock = TrialLock(
            task=TaskLock(
                name=task.name, type="local", digest="sha256:" + "a" * 64, path=task
            ),
            agent=config.agents[0],
            environment=config.environment,
            verifier=VerifierLock(),
        )
        native_trial = TrialResult.model_validate(
            {
                "task_name": task.name,
                "trial_name": trial.name,
                "trial_uri": trial.as_uri(),
                "task_id": {"path": task},
                "task_checksum": "a" * 64,
                "config": trial_config,
                "agent_info": {"name": "synthetic", "version": "1"},
                "verifier_result": {"rewards": {"reward": 1}},
                "started_at": datetime.now(UTC),
                "finished_at": datetime.now(UTC),
            }
        )
        result = JobResult(
            id=uuid4(),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            n_total_trials=1,
            stats=JobStats(n_completed_trials=1),
            trial_results=[native_trial],
        )
        (job / "config.json").write_text(config.model_dump_json())
        (job / "lock.json").write_text(
            JobLock(
                n_concurrent_trials=config.n_concurrent_trials,
                retry=config.retry,
                trials=[lock],
            ).model_dump_json()
        )
        (job / "result.json").write_text(result.model_dump_json())
        (trial / "config.json").write_text(trial_config.model_dump_json())
        (trial / "lock.json").write_text(lock.model_dump_json())
        (trial / "result.json").write_text(native_trial.model_dump_json())
        (trial / "artifacts/manifest.json").write_text(
            json.dumps(
                [
                    {
                        "source": "/logs/artifacts",
                        "destination": "artifacts/logs/artifacts",
                        "type": "directory",
                        "status": "empty",
                        "service": None,
                    },
                    {
                        "source": "/workspace",
                        "destination": "artifacts/workspace",
                        "type": "directory",
                        "status": "ok",
                        "service": "main",
                    },
                ],
                indent=2,
            )
        )
        return result


def tree_state(root):
    return {
        str(path.relative_to(root)): (
            path.lstat().st_ino,
            path.lstat().st_mode,
            path.lstat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in [root, *root.rglob("*")]
    }


@pytest.mark.parametrize("resource_state", ["existing", "missing", "changed"])
def test_completed_docker_result_and_status_never_rematerialize(
    tmp_path, monkeypatch, resource_state
):
    root = initialize_project(tmp_path / "project")
    text = "Skill body\n"
    harness = HarnessConfig(
        name="claude-code",
        version="2.1.267",
        model="anthropic/claude-opus-5",
        env={"ANTHROPIC_API_KEY": "${SYNTHETIC_UNUSED_KEY}"},
        resources=[
            SealedResource(
                destination="skills/example/SKILL.md",
                text=text,
                sha256=sha256_hex(text.encode()),
            )
        ],
    )
    prepared = prepare_run(
        root,
        "example",
        run_id="resource-run",
        overrides=ConfigOverrides(harness=harness),
    )
    output = tmp_path / "output"
    output.mkdir()
    paths = local_paths(output)
    paths.context.mkdir()
    materialize_sealed_context(prepared.sealed_context, paths.context)
    paths.request.write_bytes(canonical_model_bytes(prepared.request))
    labels = {
        RUN_LABEL: prepared.request.run_id,
        ATTEMPT_LABEL: output.name,
        PLAN_LABEL: prepared.request.plan_sha256,
    }
    options: dict[str, Any] = dict(
        environment_import_path=ENVIRONMENT_IMPORT_PATH, labels=labels
    )
    # Pure compilation works before resource creation and is byte-stable afterward.
    expected = compile_harbor_job(
        prepared.request, paths, api=Harbor022Api(), **options
    )
    assert not (output / "harness-resources").exists()
    executed = HarborRunner(SyntheticCompletedHarbor()).run(
        prepared.request, paths, **options
    )
    assert executed.reward == "1"
    with pytest.raises(ValueError, match="fresh directory"):
        HarborRunner(SyntheticCompletedHarbor()).run(prepared.request, paths, **options)
    assert (
        compile_harbor_job(
            prepared.request, paths, api=Harbor022Api(), **options
        ).model_dump_json()
        == expected.model_dump_json()
    )
    resources = output / "harness-resources"
    if resource_state == "missing":
        shutil.rmtree(resources)
    elif resource_state == "changed":
        (resources / "skills/example/SKILL.md").write_text("Do not trust mutable bytes")
    reference = RunReference(
        run_id=prepared.request.run_id,
        engine="docker",
        process=process_identity(os.getpid()),
        output_directory=str(output),
        request_sha256=sha256_hex(paths.request.read_bytes()),
        output_identity=(output.stat().st_dev, output.stat().st_ino),
    )
    before = tree_state(output)
    monkeypatch.setattr(
        "tetrabench.engines.docker.observe_cleanup", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "tetrabench.harbor_runner.materialize_resources",
        lambda *a, **k: pytest.fail("result read tried to materialize"),
    )
    engine = DockerEngine()
    for read in (engine.result, engine.status, engine.result):
        report = read(reference)
        assert report.state == "terminal" and report.reward == "1"
        assert tree_state(output) == before
    # The mutable native config still cannot replace sealed expected configuration.
    path = output / "harbor-job/config.json"
    value = json.loads(path.read_text())
    value["agents"][0]["kwargs"]["harness"]["resources"][0]["text"] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        engine.result(reference)


@pytest.mark.parametrize("suffix", ["json", "jsonc", "toml"])
def test_private_resource_relocation_preserves_original_and_escaping(tmp_path, suffix):
    from tetrabench.harness_config import NativeConfig
    from tetrabench.harnesses import parse_native

    value = {
        "prompt": "{file:" + AGENT_RESOURCE_ROOT + "/prompt.md}",
        "unrelated": AGENT_RESOURCE_ROOT + "-other/prompt.md",
    }
    if suffix == "toml":
        import toml

        text = toml.dumps(value)
    else:
        text = json.dumps(value)
    resources = [
        SealedResource(
            destination="config." + suffix, text=text, sha256=sha256_hex(text.encode())
        )
    ]
    original = resources[0].model_dump_json()
    target = tmp_path / 'private "quoted" \\root'
    materialize_resources(resources, target, relocate_references=True)
    copied = parse_native(
        NativeConfig(format=suffix, text=(target / ("config." + suffix)).read_text())
    )
    assert copied == {**value, "prompt": "{file:" + str(target) + "/prompt.md}"}
    assert resources[0].model_dump_json() == original
    before = tree_state(target)
    with pytest.raises(ValueError, match="fresh directory"):
        materialize_resources(resources, target, relocate_references=True)
    assert tree_state(target) == before
    plain = tmp_path / "execution"
    materialize_resources(resources, plain)
    assert (plain / ("config." + suffix)).read_text() == text


@pytest.mark.parametrize("relocate", [False, True])
def test_resource_digest_is_verified_before_any_materialization(tmp_path, relocate):
    resource = SealedResource(
        destination="rules.md", text="sealed", sha256=sha256_hex(b"sealed")
    ).model_copy(update={"text": "untrusted"})
    destination = tmp_path / "resources"
    with pytest.raises(ValueError, match="digest mismatch"):
        materialize_resources([resource], destination, relocate_references=relocate)
    assert not destination.exists()


@pytest.mark.parametrize("seed", ["0", "3"])
def test_completed_resource_reads_across_native_retry_set_hash_orders(tmp_path, seed):
    # Seed 3 changes Harbor 0.22.0's default retry-set iteration order on a JSON
    # round-trip. Run the real lifecycle assertions in fresh processes so prior
    # tests, import state and the parent interpreter's random seed cannot hide it.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_resource_lifecycle.py::"
            "test_completed_docker_result_and_status_never_rematerialize",
            "--basetemp=" + str(tmp_path / "child-tests"),
            "-p",
            "no:cacheprovider",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "PYTHONHASHSEED": seed,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
