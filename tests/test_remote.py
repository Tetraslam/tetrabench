from __future__ import annotations

from collections.abc import Callable
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
from tetrabench.rewards import (
    ControllerResultV1,
    ControllerResultV2,
    SectionRewardSummary,
    TaskRewardSummary,
    TrialReward,
)
from tetrabench.s3 import AdmissionRead
from tetrabench.storage import content_object_key


def _request(reward_policy: str | None = None) -> RequestRecord:
    trial = {"task_id": "task", "harbor_task": "task"}
    if reward_policy is not None:
        trial["reward_policy"] = reward_policy
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
            "trials": (trial,),
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


def _legacy_controller_result() -> bytes:
    return canonical_model_bytes(
        ControllerResultV1(
            schema_version=1,
            run_id="run-1",
            attempt_id="attempt-1",
            outcome="succeeded",
            harbor_version="0.22.0",
            modal_version="1.5.4",
            tetrabench_version="0.1.0",
        )
    )


def _terminal(
    outcome: str = "succeeded",
    *,
    request: RequestRecord | None = None,
    controller_result: bytes | None = None,
) -> TerminalRecord:
    request = request or _request()
    if controller_result is None:
        controller_result = _legacy_controller_result().replace(
            b'"outcome":"succeeded"', f'"outcome":"{outcome}"'.encode()
        )
    entries = (
        *(
            ArtifactInventoryEntry(
                logical_path=f"job/{name}.json",
                content=_content(name.encode()),
            )
            for name in ("config", "lock", "result")
        ),
        ArtifactInventoryEntry(
            logical_path="attempts/attempt-1/controller-result.json",
            content=_content(controller_result),
        ),
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
    request_override: RequestRecord | None = None
    extra_content: dict[str, bytes] | None = None

    def __post_init__(self) -> None:
        request = self.request_override or _request()
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
        if self.extra_content is not None and descriptor.sha256 in self.extra_content:
            return self.extra_content[descriptor.sha256]
        controller = _legacy_controller_result()
        if descriptor.sha256 == sha256_hex(controller):
            return controller
        for outcome in ("failed", "cancelled"):
            candidate = controller.replace(
                b'"outcome":"succeeded"', f'"outcome":"{outcome}"'.encode()
            )
            if descriptor.sha256 == sha256_hex(candidate):
                return candidate
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
    assert report.summary_status == "legacy_unavailable"
    assert tuple(item.logical_path for item in report.artifacts) == (
        "attempts/attempt-1/controller-result.json",
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


def _binary_summary() -> SectionRewardSummary:
    return SectionRewardSummary(
        policy="binary",
        aggregate_kind="binary_pass_rate",
        task_count=1,
        sample_count=1,
        pass_count=1,
        aggregate="1",
        trials=(
            TrialReward(
                task_id="task",
                trial_name="trial-one",
                policy="binary",
                value="1",
            ),
        ),
        tasks=(
            TaskRewardSummary(
                task_id="task",
                policy="binary",
                sample_count=1,
                pass_count=1,
                aggregate="1",
            ),
        ),
    )


def _binary_controller_result() -> tuple[RequestRecord, bytes]:
    request = _request("binary")
    controller = canonical_model_bytes(
        ControllerResultV2(
            schema_version=2,
            run_id="run-1",
            attempt_id="attempt-1",
            outcome="succeeded",
            request_sha256=sha256_hex(canonical_model_bytes(request)),
            plan_sha256=request.plan_sha256,
            harbor_version="0.22.0",
            modal_version="1.5.4",
            tetrabench_version="0.1.0",
            summary=_binary_summary(),
        )
    )
    return request, controller


def _remote_summary_report(controller: bytes):
    request = _request("binary")
    terminal = _terminal(request=request, controller_result=controller)
    descriptor = next(
        item.content
        for item in terminal.artifacts
        if item.logical_path.endswith("controller-result.json")
    )
    store = _Store(
        _terminal_state(terminal),
        request_override=request,
        extra_content={descriptor.sha256: controller},
    )
    return RemoteResultService(store).result("run-1")


def test_remote_validates_new_summary_and_rejects_tampering() -> None:
    _request_record, controller = _binary_controller_result()

    report = _remote_summary_report(controller)
    assert report.state == "terminal"
    assert report.summary_status == "available"
    assert report.summary == _binary_summary()
    assert report.reward == "1"

    tampered = controller.replace(b'"pass_count":1', b'"pass_count":0', 1)
    report = _remote_summary_report(tampered)
    assert report.state == "conflict"
    assert report.reasons == ("invalid controller result: ValidationError",)


@pytest.mark.parametrize(
    "value",
    ["2", "-0", "0.0", "-0.0", "00", "0e0", "0E+0", "1.0", "01", "1e0", "1E+0"],
)
def test_remote_rejects_every_alternate_binary_trial_value(value: str) -> None:
    _request_record, controller = _binary_controller_result()
    tampered = controller.replace(b'"value":"1"', f'"value":"{value}"'.encode())

    report = _remote_summary_report(tampered)

    assert report.state == "conflict"
    assert report.reasons == ("invalid controller result: ValidationError",)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda value: value.replace(b'"pass_count":1', b'"pass_count":0').replace(
            b'"aggregate":"1"', b'"aggregate":"0"'
        ),
        lambda value: value.replace(b'"sample_count":1', b'"sample_count":2').replace(
            b'"aggregate":"1"', b'"aggregate":"0.5"'
        ),
    ],
)
def test_remote_rejects_coherent_counts_and_rates_not_derived_from_trials(
    tamper: Callable[[bytes], bytes],
) -> None:
    _request_record, controller = _binary_controller_result()

    report = _remote_summary_report(tamper(controller))

    assert report.state == "conflict"
    assert report.reasons == ("invalid controller result: ValidationError",)


def test_remote_runs_reads_discovered_ids_without_local_state() -> None:
    report = RemoteResultService(_Store(UnknownRunState(run_id="run-1"))).runs()

    assert tuple(item.run_id for item in report.runs) == ("run-1",)
    assert report.runs[0].state == "unknown"
