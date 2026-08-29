from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import harbor.environments.modal as harbor_modal_module
import modal
import pytest
from harbor.environments.modal import ModalEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from tetrabench.controller_runtime import credential_free_harbor_environment
from tetrabench.harbor import (
    ATTEMPT_LABEL,
    PLAN_LABEL,
    RUN_LABEL,
    ModalChildObserver,
    S3ChildIdentitySource,
    TetrabenchModalEnvironment,
    _registered_event_sink,
    child_event_sink,
)
from tetrabench.records import AttemptEvent


class _EventStore:
    @staticmethod
    def read_attempt_events(run_id):
        return (
            AttemptEvent(
                schema_version=1,
                run_id=run_id,
                attempt_id="attempt-1",
                sequence=0,
                type="modal-child",
                payload={
                    "sandbox_id": "sb-persisted",
                    "session_id": "harbor-session",
                },
            ),
            AttemptEvent(
                schema_version=1,
                run_id=run_id,
                attempt_id="attempt-1",
                sequence=1,
                type="other-event",
                payload={"sandbox_id": "sb-ignored"},
            ),
        )


class _NoChildIdentities:
    @staticmethod
    def list_child_ids(run_id: str) -> tuple[str, ...]:
        del run_id
        return ()


class _SandboxHandle:
    def __init__(self, object_id, active):
        self.object_id = object_id
        self.active = active
        self.waits = []

    def poll(self):
        return None if self.object_id in self.active else 137

    def terminate(self, *, wait):
        self.waits.append(wait)
        self.active.discard(self.object_id)


class _SandboxApi:
    def __init__(self):
        self.active = {"sb-persisted", "sb-tagged"}
        self.listed = set(self.active)
        self.handles = {}
        self.list_calls = []

    def list(self, *, app_id, tags):
        self.list_calls.append((app_id, tags))
        return [self.from_id(item) for item in sorted(self.listed)]

    def from_id(self, sandbox_id):
        return self.handles.setdefault(
            sandbox_id,
            _SandboxHandle(sandbox_id, self.active),
        )


def test_persisted_child_identity_source_reads_only_modal_child_events() -> None:
    assert S3ChildIdentitySource(_EventStore()).list_child_ids("run-1") == (
        "sb-persisted",
    )


def test_real_observer_uses_harbor_app_tags_persisted_ids_and_waits() -> None:
    sandboxes = _SandboxApi()
    lookups = []

    def app_lookup(name, **kwargs):
        lookups.append((name, kwargs))
        return SimpleNamespace(app_id="ap-harbor")

    observer = ModalChildObserver(
        S3ChildIdentitySource(_EventStore()),
        environment_name="tetrabench-default",
        app_lookup=app_lookup,
        sandbox_type=sandboxes,
    )
    result = observer.sweep("run-1")
    assert result.remaining_child_ids == ()
    assert lookups == [
        (
            "__harbor__",
            {
                "environment_name": "tetrabench-default",
                "create_if_missing": False,
            },
        ),
        (
            "__harbor__",
            {
                "environment_name": "tetrabench-default",
                "create_if_missing": False,
            },
        ),
    ]
    assert sandboxes.list_calls[0] == (
        "ap-harbor",
        {RUN_LABEL: "run-1"},
    )
    assert all(handle.waits == [True] for handle in sandboxes.handles.values())


def test_missing_harbor_app_is_an_empty_child_set() -> None:
    observer = ModalChildObserver(
        _NoChildIdentities(),
        environment_name="tetrabench-default",
        app_lookup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            modal.exception.NotFoundError("missing")
        ),
        sandbox_type=_SandboxApi(),
    )

    result = observer.sweep("run-1")

    assert result.remaining_child_ids == ()
    assert result.evidence == "terminated 0 child sandbox(es); 0 remain visible"


def test_persisted_terminal_child_is_not_terminated_again() -> None:
    sandboxes = _SandboxApi()
    sandboxes.active.clear()
    sandboxes.listed.clear()
    observer = ModalChildObserver(
        S3ChildIdentitySource(_EventStore()),
        environment_name="tetrabench-default",
        app_lookup=lambda *_args, **_kwargs: SimpleNamespace(app_id="ap-harbor"),
        sandbox_type=sandboxes,
    )

    result = observer.sweep("run-1")

    assert result.remaining_child_ids == ()
    assert sandboxes.handles["sb-persisted"].waits == []


def test_required_child_labels_are_distinct() -> None:
    assert len({RUN_LABEL, ATTEMPT_LABEL, PLAN_LABEL}) == 3
    labels = (RUN_LABEL, ATTEMPT_LABEL, PLAN_LABEL)
    assert all(label.startswith("tetrabench.") for label in labels)


def test_controller_event_sink_registry_is_invocation_scoped_and_cleared() -> None:
    events = []
    with child_event_sink(events.append) as key:
        assert _registered_event_sink(key) is not None
    with pytest.raises(RuntimeError, match="unavailable or expired"):
        _registered_event_sink(key)


def test_custom_environment_is_the_pinned_public_import_path_subclass() -> None:
    assert issubclass(TetrabenchModalEnvironment, ModalEnvironment)
    assert TetrabenchModalEnvironment.start is not ModalEnvironment.start
    assert TetrabenchModalEnvironment.stop is not ModalEnvironment.stop


def test_custom_environment_rejects_modal_sandbox_v2_before_harbor_start(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="rejects Modal sandbox v2"):
        TetrabenchModalEnvironment(
            environment_dir=tmp_path,
            environment_name="task",
            session_id="harbor-owned-session",
            trial_paths=object(),
            task_env_config=object(),
            run_id="run-1",
            attempt_id="attempt-1",
            plan_sha256="f" * 64,
            event_sink_key="test-sink",
            observation_path=str(tmp_path / "children.jsonl"),
            modal_sandbox_v2=True,
        )


def test_modal_sandbox_environment_payload_excludes_ambient_provider_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_names = {
        "AWS_ACCOUNT_ID",
        "aws_access_key_id",
        "AwS_AlternateCredential",
        "TIGRIS_SECRET_ACCESS_KEY",
        "tigris_storage_secret_access_key",
        "TiGrIs_AlternateCredential",
        "bOtO_cOnFiG",
        "bOtOcOrE_TcP_KeEpAlIvE",
    }
    for index, name in enumerate(sorted(provider_names)):
        monkeypatch.setenv(name, f"ambient-sensitive-{index}")
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.23\n")
    trial_paths = TrialPaths(tmp_path / "trial")
    safe_payload = {name: "unavailable" for name in provider_names}
    captured_payloads: list[dict[str, str]] = []
    monkeypatch.setattr(
        harbor_modal_module,
        "_cached_env_secret",
        lambda value: captured_payloads.append(dict(value)) or object(),
    )

    with credential_free_harbor_environment():
        assert provider_names.isdisjoint(__import__("os").environ)
        environment = TetrabenchModalEnvironment(
            environment_dir=environment_dir,
            environment_name="task",
            session_id="harbor-owned-session",
            trial_paths=trial_paths,
            task_env_config=EnvironmentConfig(env=safe_payload),
            run_id="run-1",
            attempt_id="attempt-1",
            plan_sha256="f" * 64,
            event_sink_key="test-sink",
            observation_path=str(tmp_path / "children.jsonl"),
        )
        sandbox_secrets = environment._secrets_config()

    assert len(sandbox_secrets) == 1
    assert captured_payloads == [safe_payload]
    assert all(
        __import__("os").environ[name].startswith("ambient-sensitive-")
        for name in provider_names
    )
