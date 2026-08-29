from __future__ import annotations

from dataclasses import dataclass

import pytest

from tetrabench.canonical_json import sha256_hex
from tetrabench.models import ResolvedPlan, ResolvedStorageConfig
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.records import (
    AdmissionRecord,
    ArtifactBinding,
    ArtifactInventoryEntry,
    ConflictRunState,
    ContentObject,
    ContextManifest,
    RequestRecord,
    TerminalRecord,
    TerminalRunState,
    UnknownRunState,
    new_admission,
    transition_admission,
)
from tetrabench.remote import RemoteResultService, RemoteRunDiscovery
from tetrabench.s3 import AdmissionRead
from tetrabench.storage import content_object_key


def _request() -> RequestRecord:
    plan = ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "systems-design",
            "controller": {"kind": "modal"},
            "execution": {"kind": "modal"},
            "storage": {
                "provider": "aws",
                "bucket": "bucket",
                "region": "us-west-2",
            },
            "selection": {},
            "harbor": {},
            "context": (),
            "trials": ({"task_id": "task", "harbor_task": "task"},),
            "runnable": True,
            "not_runnable_reasons": (),
        }
    )
    manifest = ContextManifest(schema_version=1, files=())
    return RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )


def _content(data: bytes, media_type: str = "application/json") -> ContentObject:
    digest = sha256_hex(data)
    return ContentObject(
        sha256=digest,
        key=content_object_key(digest),
        size=len(data),
        media_type=media_type,
    )


def _terminal(outcome: str = "succeeded") -> TerminalRecord:
    request = _request()
    entries = tuple(
        ArtifactInventoryEntry(
            logical_path=f"job/{name}.json",
            content=_content(name.encode()),
        )
        for name in ("config", "lock", "result")
    )
    bindings = tuple(
        ArtifactBinding(logical_path=item.logical_path, sha256=item.content.sha256)
        for item in entries
    )
    return TerminalRecord.model_validate(
        {
            "schema_version": 1,
            "run_id": "run-1",
            "request_sha256": sha256_hex(canonical_model_bytes(request)),
            "winning_attempt_id": "attempt-1",
            "outcome": outcome,
            "harbor_version": "0.22.0",
            "artifacts": entries,
            "harbor_config": bindings[0] if outcome == "succeeded" else None,
            "harbor_lock": bindings[1] if outcome == "succeeded" else None,
            "harbor_result": bindings[2] if outcome == "succeeded" else None,
            "evidence": (),
            "warnings": (),
        }
    )


def _admission(state: str = "prepared") -> AdmissionRecord:
    prepared = new_admission(_request(), timestamp="2026-08-29T12:00:00Z")
    if state == "prepared":
        return prepared
    running = transition_admission(
        prepared,
        "running",
        timestamp="2026-08-29T12:00:01Z",
        owner_function_call_id="fc-1",
    )
    if state == "running":
        return running
    if state == "failed":
        return transition_admission(running, "failed", timestamp="2026-08-29T12:00:02Z")
    if state == "cancelled":
        cancelling = transition_admission(
            running, "cancelling", timestamp="2026-08-29T12:00:02Z"
        )
        return transition_admission(
            cancelling, "cancelled", timestamp="2026-08-29T12:00:03Z"
        )
    raise AssertionError(state)


@dataclass
class _Store:
    state: object
    admission: AdmissionRecord | None = None
    native_result: bytes = b"native"

    def __post_init__(self) -> None:
        request = _request()
        self.request = request
        storage = request.plan.storage
        assert storage is not None
        self.storage: ResolvedStorageConfig = storage

    def read_run_state(self, run_id: str):
        _ = run_id
        return self.state

    def read_admission(self, run_id: str):
        _ = run_id
        return (
            AdmissionRead(self.admission, '"etag"')
            if self.admission is not None
            else None
        )

    def read_request(self, run_id: str, request_sha256: str, request_object_key: str):
        _ = run_id, request_sha256, request_object_key
        return self.request

    def read_content(self, descriptor):
        _ = descriptor
        return self.native_result

    def discover_runs(self):
        return RemoteRunDiscovery(run_ids=("run-1",), malformed_keys=())


def _terminal_state(terminal: TerminalRecord) -> TerminalRunState:
    digest = sha256_hex(canonical_model_bytes(terminal))
    return TerminalRunState(run_id="run-1", terminal_sha256=digest, terminal=terminal)


def test_remote_result_unknown_needs_no_receipt_or_admission() -> None:
    report = RemoteResultService(_Store(UnknownRunState(run_id="run-1"))).result(
        "run-1"
    )

    assert report.state == "unknown"
    assert report.artifacts == ()


@pytest.mark.parametrize("state", ["prepared", "running", "failed", "cancelled"])
def test_remote_result_exposes_every_nonterminal_admission_state(state: str) -> None:
    report = RemoteResultService(
        _Store(UnknownRunState(run_id="run-1"), _admission(state))
    ).result("run-1")

    assert report.state == "nonterminal"
    assert report.admission_state == state


def test_remote_result_terminal_exposes_reward_and_native_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = _terminal()
    store = _Store(_terminal_state(terminal))
    monkeypatch.setattr("tetrabench.remote._standard_reward", lambda _data: "0.75")

    report = RemoteResultService(store).result("run-1")

    assert report.state == "terminal"
    assert report.outcome == "succeeded"
    assert report.reward == "0.75"
    assert tuple(item.logical_path for item in report.artifacts) == (
        "job/config.json",
        "job/lock.json",
        "job/result.json",
    )


def test_remote_result_terminal_binding_mismatch_is_conflict() -> None:
    terminal = _terminal("failed")
    admission = _admission("failed").model_copy(update={"request_sha256": "f" * 64})

    report = RemoteResultService(_Store(_terminal_state(terminal), admission)).result(
        "run-1"
    )

    assert report.state == "conflict"
    assert any("binding" in reason for reason in report.reasons)


def test_remote_terminal_without_admission_rejects_wrong_storage() -> None:
    terminal = _terminal()
    store = _Store(_terminal_state(terminal))
    store.storage = store.storage.model_copy(update={"region": "us-east-1"})

    report = RemoteResultService(store).result("run-1")

    assert report.state == "conflict"
    assert any("storage" in reason for reason in report.reasons)


def test_remote_result_preserves_authoritative_conflict() -> None:
    report = RemoteResultService(
        _Store(
            ConflictRunState(
                run_id="run-1",
                terminal_sha256s=(),
                reasons=("malformed terminal",),
            )
        )
    ).result("run-1")

    assert report.state == "conflict"
    assert report.reasons == ("malformed terminal",)


def test_remote_runs_reads_discovered_ids_without_local_state() -> None:
    report = RemoteResultService(_Store(UnknownRunState(run_id="run-1"))).runs()

    assert tuple(item.run_id for item in report.runs) == ("run-1",)
    assert report.runs[0].state == "unknown"
