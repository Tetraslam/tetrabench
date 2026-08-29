from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from tetrabench.canonical_json import loads_canonical_json
from tetrabench.plan import canonical_model_bytes
from tetrabench.receipts import (
    ControllerCallReceipt,
    PhysicalSubmissionAttempt,
    ReceiptConflictError,
    ReceiptStore,
    SubmissionReceipt,
    SubmissionTransition,
    append_submission_attempt,
    record_spawn_return,
)


def _attempt(name: str = "submit-1") -> PhysicalSubmissionAttempt:
    return PhysicalSubmissionAttempt(
        attempt_id=name,
        transitions=(SubmissionTransition(sequence=0, type="admission-observed"),),
    )


def _receipt() -> SubmissionReceipt:
    return SubmissionReceipt(
        schema_version=2,
        run_id="run-1",
        request_sha256="1" * 64,
        plan_sha256="2" * 64,
        context_manifest_sha256="3" * 64,
        attempts=(_attempt(),),
    )


def test_receipt_store_durably_creates_each_missing_directory_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tetrabench.receipts as receipts_module

    fsynced_directories: list[Path] = []
    fsynced_files: list[Path] = []
    real_fsync = receipts_module.os.fsync

    def recording_fsync(descriptor: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            fsynced_directories.append(path)
        else:
            fsynced_files.append(path)
        real_fsync(descriptor)

    monkeypatch.setattr(receipts_module.os, "fsync", recording_fsync)
    store = ReceiptStore(tmp_path / "first" / "second" / "state")
    receipt = _receipt()
    store.write(receipt)

    data = store.path_for(receipt.run_id).read_bytes()
    assert data == canonical_model_bytes(receipt)
    assert loads_canonical_json(data) == receipt.model_dump(mode="json")
    assert fsynced_directories == [
        tmp_path,
        tmp_path / "first",
        tmp_path / "first" / "second",
        store.root,
    ]
    assert len(fsynced_files) == 1
    assert fsynced_files[0].parent == store.root
    assert store.read("run-1") == receipt
    assert store.path_for("run-1").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("crash_after_parent", range(3))
def test_receipt_root_creation_stops_at_each_unfsynced_crash_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_parent: int,
) -> None:
    import tetrabench.receipts as receipts_module

    class SimulatedCrash(RuntimeError):
        pass

    components = [
        tmp_path / "first",
        tmp_path / "first" / "second",
        tmp_path / "first" / "second" / "state",
    ]
    parent_boundaries = [tmp_path, *components[:-1]]
    real_fsync = receipts_module.os.fsync

    def crashing_fsync(descriptor: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if path == parent_boundaries[crash_after_parent]:
            raise SimulatedCrash
        real_fsync(descriptor)

    monkeypatch.setattr(receipts_module.os, "fsync", crashing_fsync)
    store = ReceiptStore(components[-1])

    with pytest.raises(SimulatedCrash):
        store.write(_receipt())

    assert all(path.is_dir() for path in components[: crash_after_parent + 1])
    assert all(not path.exists() for path in components[crash_after_parent + 1 :])
    assert not store.path_for("run-1").exists()


def test_receipt_history_appends_attempts_and_spawn_evidence(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    prepared = _receipt()
    store.write(prepared)
    spawned = record_spawn_return(
        prepared,
        ControllerCallReceipt(call_id="fc-1"),
    )
    store.write(spawned)
    recovered = append_submission_attempt(spawned, _attempt("submit-2"))
    store.write(recovered)

    assert tuple(item.type for item in spawned.attempts[0].transitions) == (
        "admission-observed",
        "spawn-returned",
    )
    assert spawned.attempts[0].controller_calls == (
        ControllerCallReceipt(call_id="fc-1"),
    )
    assert tuple(item.attempt_id for item in recovered.attempts) == (
        "submit-1",
        "submit-2",
    )

    rewritten = recovered.model_copy(update={"attempts": (_attempt("other"),)})
    with pytest.raises(ReceiptConflictError, match="append-only"):
        store.write(rewritten)


def test_receipt_models_are_strict_frozen_and_secret_free(tmp_path: Path) -> None:
    receipt = _receipt()
    with pytest.raises(ValidationError):
        SubmissionReceipt.model_validate(receipt.model_dump() | {"token": "secret"})
    with pytest.raises(ValidationError):
        PhysicalSubmissionAttempt(
            attempt_id="submit-1",
            transitions=(SubmissionTransition(sequence=0, type="spawn-returned"),),
        )
    assert receipt.model_config["frozen"] is True

    ReceiptStore(tmp_path).write(receipt)
    text = (tmp_path / "run-1.json").read_text(encoding="utf-8")
    for secret_name in ("secret", "password", "credential", "access_key"):
        assert secret_name not in text.lower()
