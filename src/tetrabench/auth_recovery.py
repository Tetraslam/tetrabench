"""Read-only stop evidence for explicit auth reseed, using existing run records."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from tetrabench.run_reference import RunReference, RunReferenceStore, process_identity


class ReferenceReader(Protocol):
    def read(self, run_id: str) -> RunReference | None: ...


def _local_stopped(reference: RunReference, owner: str) -> bool:
    from tetrabench.docker_lifecycle import (
        DockerBinding,
        _compose_client_active,
        _owned_containers,
        _stopped_owner,
        _verify_lock,
        _verify_output,
    )
    from tetrabench.local_control import read_owner_control, read_owner_stopped
    from tetrabench.plan import parse_canonical_model

    if (
        reference.process is None
        or reference.output_directory is None
        or reference.output_identity is None
    ):
        return False
    if owner != f"local-{reference.process.pid}-{reference.run_id}":
        return False
    output, identity = Path(reference.output_directory), reference.output_identity
    _verify_output(output, identity)
    control = read_owner_control(output)
    if control is None or control.reference != reference:
        return False
    closed = read_owner_stopped(output) == control
    try:
        alive = process_identity(reference.process.pid) == reference.process
    except (FileNotFoundError, ProcessLookupError):
        alive = False
    if alive and not closed:
        return False
    with _stopped_owner(output, identity) as descriptor:
        if descriptor is None:
            return False
        binding = parse_canonical_model(
            (output / "docker-binding.json").read_bytes(), DockerBinding
        )
        for _ in range(2):
            _verify_output(output, identity)
            _verify_lock(output, descriptor)
            if _compose_client_active(output) or _owned_containers(output, binding):
                return False
            time.sleep(0.05)
        return True


def _modal_stopped(reference: RunReference, owner: str) -> bool:
    import modal

    from tetrabench.controller import ModalControllerClient
    from tetrabench.harbor import ModalChildObserver, S3ChildIdentitySource
    from tetrabench.s3 import create_s3_store

    if (
        reference.storage is None
        or reference.app_name is None
        or reference.function_name is None
        or reference.environment_name is None
    ):
        return False
    controller = ModalControllerClient(
        reference.app_name,
        reference.function_name,
        environment_name=reference.environment_name,
    )
    if controller.inspect(owner).state not in {"succeeded", "failed", "expired"}:
        return False
    # Use the existing artifact read authority for durable child IDs, never the
    # credential bucket's client. This function performs no cleanup mutation.
    store = create_s3_store(reference.storage)
    if not any(
        event.type == "attempt-started"
        and isinstance(event.payload, dict)
        and event.payload.get("function_call_id") == owner
        for event in store.read_attempt_events(reference.run_id)
    ):
        return False
    identities = S3ChildIdentitySource(store)
    observer = ModalChildObserver(
        identities, environment_name=reference.environment_name
    )
    for _ in range(2):
        if observer._listed(reference.run_id):
            return False
        for child_id in identities.list_child_ids(reference.run_id):
            try:
                if modal.Sandbox.from_id(child_id).poll() is None:
                    return False
            except modal.exception.NotFoundError:
                continue
        time.sleep(0.05)
    return controller.inspect(owner).state in {"succeeded", "failed", "expired"}


def prove_previous_consumer_stopped(
    owner: str,
    run_id: str | None,
    *,
    references: ReferenceReader | None = None,
    cli_runtime_parent: Path | None = None,
) -> bool:
    """Missing identity or provider evidence is refusal, never permission."""
    if run_id is None:
        from tetrabench.auth_operations import prove_cli_operation_stopped

        return cli_runtime_parent is not None and prove_cli_operation_stopped(
            owner, cli_runtime_parent
        )
    try:
        reference = (references or RunReferenceStore()).read(run_id)
        if reference is None:
            return False
        if reference.engine == "docker":
            return _local_stopped(reference, owner)
        if reference.engine == "modal":
            return _modal_stopped(reference, owner)
    except Exception:
        # Provider errors can contain auth details. Report only failed proof.
        return False
    return False
