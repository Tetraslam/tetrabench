from __future__ import annotations

from types import SimpleNamespace
from typing import TypedDict

from tetrabench.auth_recovery import prove_previous_consumer_stopped
from tetrabench.docker_lifecycle import DockerBinding, execution_owner
from tetrabench.local_control import initialize_owner
from tetrabench.models import ResolvedTigrisStorageConfig
from tetrabench.plan import canonical_model_bytes
from tetrabench.run_reference import RunReference, process_identity


class References:
    def __init__(self, reference):
        self.reference = reference

    def read(self, run_id):
        return self.reference if self.reference.run_id == run_id else None


class LocalState(TypedDict):
    children: tuple[str, ...]
    compose_active: bool
    censuses: int


class ModalState(TypedDict):
    call: str
    child: int | None
    listed: dict[str, object]
    polls: int


def test_local_reseed_uses_closed_owner_and_two_empty_physical_censuses(
    tmp_path, monkeypatch
):
    import os

    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    metadata = directory.stat()
    reference = RunReference(
        run_id="synthetic",
        engine="docker",
        request_sha256="a" * 64,
        output_directory=str(directory),
        output_identity=(metadata.st_dev, metadata.st_ino),
        process=process_identity(os.getpid()),
    )
    references = References(reference)
    owner = f"local-{os.getpid()}-synthetic"
    with execution_owner(directory) as lock_id:
        initialize_owner(reference, lock_id)
        assert not prove_previous_consumer_stopped(
            owner, "synthetic", references=references
        )
    (directory / "docker-binding.json").write_bytes(
        canonical_model_bytes(DockerBinding(daemon_id="SYNTHETIC_DAEMON"))
    )
    state: LocalState = {
        "children": ("SYNTHETIC_CONTAINER",),
        "compose_active": False,
        "censuses": 0,
    }

    def containers(output, binding):
        assert output == directory and binding.daemon_id == "SYNTHETIC_DAEMON"
        state["censuses"] += 1
        return state["children"]

    monkeypatch.setattr(
        "tetrabench.docker_lifecycle._compose_client_active",
        lambda _: state["compose_active"],
    )
    monkeypatch.setattr("tetrabench.docker_lifecycle._owned_containers", containers)
    assert not prove_previous_consumer_stopped(
        owner, "synthetic", references=references
    )
    state["children"] = ()
    before = state["censuses"]
    assert prove_previous_consumer_stopped(owner, "synthetic", references=references)
    assert state["censuses"] == before + 2
    assert not prove_previous_consumer_stopped(
        "other-owner", "synthetic", references=references
    )


def test_modal_reseed_requires_owner_run_binding_and_all_retained_children_stopped(
    monkeypatch,
):
    reference = RunReference(
        run_id="synthetic",
        engine="modal",
        request_sha256="a" * 64,
        storage=ResolvedTigrisStorageConfig(provider="tigris", bucket="artifacts"),
        app_name="controller",
        function_name="run",
        environment_name="test-env",
    )
    state: ModalState = {"call": "running", "child": None, "listed": {}, "polls": 0}

    class Controller:
        def __init__(self, *args, **kwargs):
            pass

        def inspect(self, owner):
            assert owner in {"fc-SYNTHETIC", "fc-WRONG"}
            return SimpleNamespace(state=state["call"])

    class Store:
        def read_attempt_events(self, run_id):
            assert run_id == "synthetic"
            return [
                SimpleNamespace(
                    type="attempt-started", payload={"function_call_id": "fc-SYNTHETIC"}
                ),
                SimpleNamespace(
                    type="modal-child", payload={"sandbox_id": "sb-SYNTHETIC"}
                ),
            ]

    class Sandbox:
        def poll(self):
            state["polls"] += 1
            return state["child"]

    def sandbox(identifier):
        assert identifier == "sb-SYNTHETIC"
        return Sandbox()

    monkeypatch.setattr("tetrabench.controller.ModalControllerClient", Controller)
    monkeypatch.setattr("tetrabench.s3.create_s3_store", lambda _: Store())
    monkeypatch.setattr(
        "tetrabench.harbor.ModalChildObserver._listed", lambda *args: state["listed"]
    )
    monkeypatch.setattr("modal.Sandbox.from_id", sandbox)
    references = References(reference)
    assert not prove_previous_consumer_stopped(
        "fc-SYNTHETIC", "synthetic", references=references
    )
    state["call"] = "failed"
    assert not prove_previous_consumer_stopped(
        "fc-SYNTHETIC", "synthetic", references=references
    )
    state["child"] = 137
    assert prove_previous_consumer_stopped(
        "fc-SYNTHETIC", "synthetic", references=references
    )
    assert state["polls"] >= 3
    assert not prove_previous_consumer_stopped(
        "fc-WRONG", "synthetic", references=references
    )
    assert not prove_previous_consumer_stopped(
        "fc-SYNTHETIC", None, references=references
    )
