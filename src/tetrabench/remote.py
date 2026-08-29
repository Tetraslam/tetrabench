"""Receipt-independent remote result and run discovery."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol

from harbor.models.job.result import JobResult
from pydantic import Field, ValidationError

from tetrabench.lifecycle import (
    ActiveAdmissionBindingError,
    BindingStore,
    terminal_admission_conflicts,
    validate_admission_request_binding,
)
from tetrabench.models import FrozenRecord, NonEmptyString, Sha256
from tetrabench.records import (
    AdmissionRecord,
    ArtifactInventoryEntry,
    ConflictRunState,
    RunId,
    RunReadState,
    TerminalRunState,
    validate_run_id,
)
from tetrabench.s3 import AdmissionRead, S3IntegrityError


class RemoteReadStore(BindingStore, Protocol):
    def read_run_state(self, run_id: str) -> RunReadState: ...
    def read_admission(self, run_id: str) -> AdmissionRead | None: ...
    def read_content(self, descriptor) -> bytes: ...
    def discover_runs(self) -> RemoteRunDiscovery: ...


class RemoteArtifact(FrozenRecord):
    logical_path: NonEmptyString
    sha256: Sha256
    size: int = Field(ge=0)
    media_type: NonEmptyString


class RemoteResult(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RunId
    state: Literal["unknown", "nonterminal", "terminal", "conflict"]
    admission_state: (
        Literal[
            "prepared",
            "running",
            "recovering",
            "cancelling",
            "cancelled",
            "terminal",
            "failed",
        ]
        | None
    ) = None
    outcome: Literal["succeeded", "failed", "cancelled"] | None = None
    reward: str | None = None
    terminal_sha256: Sha256 | None = None
    artifacts: tuple[RemoteArtifact, ...] = ()
    reasons: tuple[NonEmptyString, ...] = ()


class MalformedRemoteKey(FrozenRecord):
    key: NonEmptyString
    reason: NonEmptyString


class RemoteRunDiscovery(FrozenRecord):
    schema_version: Literal[1] = 1
    run_ids: tuple[RunId, ...]
    malformed_keys: tuple[MalformedRemoteKey, ...]


class RemoteRunsReport(FrozenRecord):
    schema_version: Literal[1] = 1
    runs: tuple[RemoteResult, ...]
    malformed_keys: tuple[MalformedRemoteKey, ...]


def _standard_reward(data: bytes) -> str | None:
    result = JobResult.model_validate_json(data)
    values: list[Decimal] = []
    for trial in result.trial_results:
        verifier = trial.verifier_result
        if verifier is None or verifier.rewards is None:
            continue
        value = verifier.rewards.get("reward")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(Decimal(str(value)))
    return str(sum(values) / len(values)) if values else None


def _inventory(
    artifacts: tuple[ArtifactInventoryEntry, ...],
) -> tuple[RemoteArtifact, ...]:
    return tuple(
        RemoteArtifact(
            logical_path=item.logical_path,
            sha256=item.content.sha256,
            size=item.content.size,
            media_type=item.content.media_type,
        )
        for item in sorted(artifacts, key=lambda item: item.logical_path)
    )


def _admission_conflict(
    store: RemoteReadStore,
    admission: AdmissionRecord,
) -> str | None:
    try:
        validate_admission_request_binding(store, admission)
    except (
        ActiveAdmissionBindingError,
        OSError,
        S3IntegrityError,
        TypeError,
        ValueError,
    ) as error:
        return f"invalid admission binding: {error}"
    return None


class RemoteResultService:
    def __init__(self, store: RemoteReadStore) -> None:
        self._store = store

    def result(self, run_id: str) -> RemoteResult:
        run_id = validate_run_id(run_id)
        durable = self._store.read_run_state(run_id)
        admission_error: str | None = None
        try:
            observed = self._store.read_admission(run_id)
        except (OSError, S3IntegrityError, TypeError, ValueError) as error:
            observed = None
            admission_error = f"invalid admission record: {error}"
        admission = observed.record if observed is not None else None

        reasons: list[str] = []
        if isinstance(durable, ConflictRunState):
            reasons.extend(durable.reasons)
        if admission_error is not None:
            reasons.append(admission_error)
        if isinstance(durable, TerminalRunState):
            reasons.extend(
                terminal_admission_conflicts(self._store, durable, admission)
            )
        elif admission is not None:
            conflict = _admission_conflict(self._store, admission)
            if conflict is not None:
                reasons.append(conflict)
        if reasons:
            return RemoteResult(
                run_id=run_id,
                state="conflict",
                admission_state=admission.state if admission is not None else None,
                reasons=tuple(sorted(set(reasons))),
            )
        if isinstance(durable, TerminalRunState):
            terminal = durable.terminal
            reward: str | None = None
            if terminal.harbor_result is not None:
                descriptor = next(
                    item.content
                    for item in terminal.artifacts
                    if item.logical_path == terminal.harbor_result.logical_path
                    and item.content.sha256 == terminal.harbor_result.sha256
                )
                try:
                    reward = _standard_reward(self._store.read_content(descriptor))
                except (
                    S3IntegrityError,
                    TypeError,
                    ValueError,
                    ValidationError,
                ) as error:
                    return RemoteResult(
                        run_id=run_id,
                        state="conflict",
                        admission_state=(
                            admission.state if admission is not None else None
                        ),
                        terminal_sha256=durable.terminal_sha256,
                        artifacts=_inventory(terminal.artifacts),
                        reasons=(
                            f"invalid native Harbor result: {type(error).__name__}",
                        ),
                    )
            return RemoteResult(
                run_id=run_id,
                state="terminal",
                admission_state=admission.state if admission is not None else None,
                outcome=terminal.outcome,
                reward=reward,
                terminal_sha256=durable.terminal_sha256,
                artifacts=_inventory(terminal.artifacts),
            )
        if admission is None:
            return RemoteResult(run_id=run_id, state="unknown")
        return RemoteResult(
            run_id=run_id,
            state="nonterminal",
            admission_state=admission.state,
        )

    def runs(self) -> RemoteRunsReport:
        discovery = self._store.discover_runs()
        return RemoteRunsReport(
            runs=tuple(self.result(run_id) for run_id in discovery.run_ids),
            malformed_keys=discovery.malformed_keys,
        )
