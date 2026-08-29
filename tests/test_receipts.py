from __future__ import annotations

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


def test_receipt_store_fsyncs_new_root_parent_file_and_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tetrabench.receipts as receipts_module

    calls: list[int] = []
    real_fsync = receipts_module.os.fsync

    def recording_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(receipts_module.os, "fsync", recording_fsync)
    store = ReceiptStore(tmp_path / "new-parent" / "state")
    receipt = _receipt()
    store.write(receipt)

    data = store.path_for(receipt.run_id).read_bytes()
    assert data == canonical_model_bytes(receipt)
    assert loads_canonical_json(data) == receipt.model_dump(mode="json")
    assert len(calls) == 3
    assert store.read("run-1") == receipt
    assert store.path_for("run-1").stat().st_mode & 0o777 == 0o600


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
