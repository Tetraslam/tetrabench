#!/usr/bin/env python3
"""Independent semantic model and deterministic hidden-case inputs."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from schedule import FaultSchedule, derive_schedule


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def case_input_manifest(cases: dict[str, Any]) -> dict[str, Any]:
    case_inputs = []
    for item in cases["cases"]:
        value = {
            "gate_ids": item["gate_ids"],
            "id": item["id"],
            "scenario": item["scenario"],
            "seed": item["seed"],
        }
        if "fault_schedule_id" in item:
            value["fault_schedule_id"] = item["fault_schedule_id"]
        case_inputs.append(value)
    return {
        "cases": case_inputs,
        "fault_schedules": cases["fault_schedules"],
        "schema_version": cases["schema_version"],
        "task_id": cases["task_id"],
    }


def case_input_digest(cases: dict[str, Any]) -> str:
    return digest(case_input_manifest(cases))


def concurrent_claim_relation(
    effects: list[dict[str, Any]], winner: str, loser: str
) -> dict[str, Any]:
    by_worker = {item["arguments"]["worker"]: deepcopy(item) for item in effects}
    if set(by_worker) != {winner, loser} or len(effects) != 2:
        raise ValueError("concurrent claim identity mismatch")

    def as_role(worker: str, role: str, exit_code: int) -> dict[str, Any]:
        effect = by_worker[worker]
        if effect["operation"] != "claim" or effect["exit_code"] != exit_code:
            raise ValueError("concurrent claim result mismatch")
        effect["arguments"]["worker"] = role
        return effect

    return {
        "loser": as_role(loser, "loser", 2),
        "operation": "concurrent-claim-relation",
        "winner": as_role(winner, "winner", 0),
    }


class Model:
    def __init__(self, minimum: int, maximum: int):
        self.minimum = minimum
        self.maximum = maximum
        self.job: dict[str, Any] | None = None
        self.effects: list[dict[str, Any]] = []

    def _effect(
        self,
        operation: str,
        arguments: dict[str, Any],
        exit_code: int,
        idempotent: bool | None,
    ) -> None:
        self.effects.append(
            {
                "arguments": arguments,
                "exit_code": exit_code,
                "idempotent": idempotent,
                "operation": operation,
            }
        )

    def _tick(self, value: int) -> bool:
        return type(value) is int and self.minimum <= value <= self.maximum

    def _deadline(self, now: int, ttl: int) -> int | None:
        if not self._tick(now) or type(ttl) is not int or ttl <= 0:
            return None
        if ttl > self.maximum - now:
            return None
        return now + ttl

    def _current(self, worker: str, token: int, now: int) -> bool:
        assert self.job is not None
        return (
            self.job["status"] == "running"
            and self.job["worker_id"] == worker
            and self.job["fence_token"] == token
            and now < self.job["deadline"]
        )

    def create(self, job: str) -> int:
        if self.job is not None:
            self._effect("create", {"job": job}, 2, None)
            return 2
        self.job = {
            "deadline": None,
            "fence_token": 0,
            "job_id": job,
            "status": "pending",
            "terminal_op": None,
            "worker_id": None,
        }
        self._effect("create", {"job": job}, 0, False)
        return 0

    def inspect(self) -> int:
        assert self.job is not None
        self._effect("inspect", {"job": self.job["job_id"]}, 0, False)
        return 0

    def claim(
        self,
        worker: str,
        now: int,
        ttl: int,
        *,
        fault_checkpoint: str | None = None,
        fault_exit_code: int | None = None,
        trace_worker: str | None = None,
    ) -> int:
        assert self.job is not None
        arguments = {
            "job": self.job["job_id"],
            "now": now,
            "ttl": ttl,
            "worker": trace_worker or worker,
        }
        if fault_checkpoint is not None:
            arguments["fault"] = fault_checkpoint
        new_deadline = self._deadline(now, ttl)
        if (
            new_deadline is None
            or self.job["status"] in {"completed", "failed"}
            or (self.job["status"] == "running" and now < self.job["deadline"])
            or self.job["fence_token"] >= self.maximum
        ):
            self._effect("claim", arguments, 2, None)
            return 2
        if fault_checkpoint is not None:
            if fault_checkpoint != "claim.precommit" or fault_exit_code is None:
                raise ValueError("claim fault schedule checkpoint mismatch")
            self._effect("claim", arguments, fault_exit_code, None)
            return fault_exit_code
        self.job.update(
            deadline=new_deadline,
            fence_token=self.job["fence_token"] + 1,
            status="running",
            terminal_op=None,
            worker_id=worker,
        )
        self._effect("claim", arguments, 0, False)
        return 0

    def action(
        self,
        command: str,
        worker: str,
        token: int,
        now: int,
        *,
        ttl: int | None = None,
        fault_checkpoint: str | None = None,
        fault_exit_code: int | None = None,
    ) -> int:
        assert self.job is not None
        arguments = {
            "job": self.job["job_id"],
            "now": now,
            "token": token,
            "worker": worker,
        }
        if ttl is not None:
            arguments["ttl"] = ttl
        if fault_checkpoint is not None:
            arguments["fault"] = fault_checkpoint
        if not self._tick(now) or not self._tick(token):
            self._effect(command, arguments, 2, None)
            return 2
        if command == "renew":
            assert ttl is not None
            new_deadline = self._deadline(now, ttl)
            if new_deadline is None or not self._current(worker, token, now):
                self._effect(command, arguments, 2, None)
                return 2
            self.job["deadline"] = max(self.job["deadline"], new_deadline)
            self._effect(command, arguments, 0, False)
            return 0
        if self.job["status"] in {"completed", "failed"}:
            if (
                self.job["terminal_op"] == command
                and self.job["worker_id"] == worker
                and self.job["fence_token"] == token
            ):
                self._effect(command, arguments, 0, True)
                return 0
            self._effect(command, arguments, 2, None)
            return 2
        if not self._current(worker, token, now):
            self._effect(command, arguments, 2, None)
            return 2
        if fault_checkpoint is not None:
            if fault_checkpoint != "transition.precommit" or fault_exit_code is None:
                raise ValueError("transition fault schedule checkpoint mismatch")
            self._effect(command, arguments, fault_exit_code, None)
            return fault_exit_code
        self.job["status"] = "completed" if command == "complete" else "failed"
        self.job["terminal_op"] = command
        self._effect(command, arguments, 0, False)
        return 0


def execute_scenario(
    case: dict[str, Any],
    minimum: int,
    maximum: int,
    fault_schedules: dict[str, FaultSchedule],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    values = derive_schedule(case, maximum, fault_schedules)
    model = Model(minimum, maximum)
    model.create(values.job)
    w = values.workers
    t = values.ttls
    n = values.base
    scenario = case["scenario"]

    if scenario == "concurrent-expiry-claim":
        model.claim(w[0], n, t[0])
        expiry = n + t[0]
        start = len(model.effects)
        winner, loser = values.contenders
        model.claim(winner, expiry, t[1])
        model.claim(loser, expiry, t[1])
        model.effects[start:] = [
            concurrent_claim_relation(model.effects[start:], winner, loser)
        ]
        assert model.job is not None
        model.job["worker_id"] = "winner"
    elif scenario in {"different-worker-reclaim", "same-worker-reclaim"}:
        sequence = (
            (w[0], w[1], w[2]) if scenario.startswith("different") else (w[0],) * 3
        )
        now = n
        for worker, ttl in zip(sequence, t, strict=False):
            model.claim(worker, now, ttl)
            now += ttl
    elif scenario == "delayed-stale-actions":
        model.claim(w[0], n, t[0])
        model.claim(w[1], n + t[0], t[1])
        for command in values.action_order:
            model.action(
                command,
                w[0],
                1,
                n + t[0] + 1,
                ttl=t[2] if command == "renew" else None,
            )
    elif scenario == "exact-expiry-rejection":
        model.claim(w[0], n, t[0])
        expiry = n + t[0]
        for command in values.action_order:
            model.action(
                command,
                w[0],
                1,
                expiry,
                ttl=t[1] if command == "renew" else None,
            )
    elif scenario == "invalid-ttl-rejection":
        model.claim(w[0], n, t[0])
        for ttl in values.invalid_ttls:
            model.action("renew", w[0], 1, n + 1, ttl=ttl)
        expiry = n + t[0]
        for ttl in values.invalid_ttls:
            model.claim(w[1], expiry, ttl)
    elif scenario == "invalid-tick-overflow-rejection":
        model.claim(w[0], n, t[0])
        for action in values.invalid_tick_order:
            if action == "negative-renew":
                model.action("renew", w[0], 1, -1, ttl=t[1])
            elif action == "overflow-complete":
                model.action("complete", w[0], 1, maximum + 1)
            else:
                model.claim(w[1], maximum, 1)
    elif scenario == "shorter-renewal":
        long_ttl = max(t[0], t[1]) + 8
        model.claim(w[0], n, long_ttl)
        model.action("renew", w[0], 1, n + 1, ttl=2)
    elif scenario == "tick-domain-boundaries":
        model.claim(w[0], minimum, t[0])
        model.claim(w[1], minimum + t[0], t[1])
        near_max = maximum - t[2]
        model.claim(w[2], near_max, t[2])
        model.action("complete", w[2], 3, maximum)
    elif scenario == "active-restart":
        model.claim(w[0], n, t[0])
        model.inspect()
        model.action("renew", w[0], 1, n + 1, ttl=t[1])
        model.inspect()
    elif scenario == "terminal-restart":
        model.claim(w[0], n, t[0])
        model.action("fail", w[0], 1, n + 1)
        model.inspect()
    elif scenario == "claim-precommit-crash":
        if values.fault is None:
            raise ValueError("claim crash case has no fault schedule")
        model.claim(
            w[0],
            n,
            t[0],
            fault_checkpoint=values.fault.checkpoint_id,
            fault_exit_code=values.fault.exit_code,
        )
        model.claim(w[1], n + 1, t[1])
    elif scenario == "transition-precommit-crash":
        if values.fault is None:
            raise ValueError("transition crash case has no fault schedule")
        model.claim(w[0], n, t[0])
        model.action(
            "complete",
            w[0],
            1,
            n + 1,
            fault_checkpoint=values.fault.checkpoint_id,
            fault_exit_code=values.fault.exit_code,
        )
        model.action("complete", w[0], 1, n + 2)
    elif scenario == "matching-terminal-replay":
        model.claim(w[0], n, t[0])
        model.action("complete", w[0], 1, n + 1)
        model.action("complete", w[0], 1, n + t[0])
    elif scenario == "conflicting-terminal-rejection":
        model.claim(w[0], n, t[0])
        model.action("complete", w[0], 1, n + 1)
        for command, worker_index, token in values.conflicting_terminal_order:
            model.action(command, w[worker_index], token, n + 2)
    else:
        raise ValueError("unknown model scenario")

    assert model.job is not None
    return deepcopy(model.job), model.effects


def expected_hashes(
    case: dict[str, Any],
    minimum: int,
    maximum: int,
    fault_schedules: dict[str, FaultSchedule],
) -> tuple[str, str]:
    state, effects = execute_scenario(case, minimum, maximum, fault_schedules)
    return digest(state), digest(effects)
