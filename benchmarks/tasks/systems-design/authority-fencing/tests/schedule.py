#!/usr/bin/env python3
"""Trusted seed expansion and fault-schedule resolution for hidden cases."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FaultSchedule:
    id: str
    checkpoint_id: str
    exit_code: int


@dataclass(frozen=True)
class Schedule:
    job: str
    workers: tuple[str, ...]
    base: int
    ttls: tuple[int, ...]
    contenders: tuple[str, str]
    action_order: tuple[str, ...]
    invalid_ttls: tuple[int, int]
    invalid_tick_order: tuple[str, ...]
    conflicting_terminal_order: tuple[tuple[str, int, int], ...]
    fault: FaultSchedule | None


def derive_schedule(
    case: dict[str, Any],
    maximum: int,
    fault_schedules: dict[str, FaultSchedule],
) -> Schedule:
    """Expand one seed exactly once for both model and runner orchestration."""
    source = random.Random(case["seed"])
    job = f"job-{source.getrandbits(56):014x}"
    workers = tuple(f"worker-{source.getrandbits(56):014x}" for _ in range(5))
    base = source.randrange(1000, maximum - 10000)
    ttls = tuple(source.randrange(2, 32) for _ in range(8))

    contenders = [workers[1], workers[2]]
    source.shuffle(contenders)
    actions = ["renew", "complete", "fail"]
    source.shuffle(actions)
    invalid_ttls = [0, -ttls[1]]
    source.shuffle(invalid_ttls)
    invalid_tick_order = ["negative-renew", "overflow-complete", "overflow-claim"]
    source.shuffle(invalid_tick_order)
    conflicting_terminal_order = [
        ("fail", 0, 1),
        ("complete", 0, 2),
        ("complete", 1, 1),
    ]
    source.shuffle(conflicting_terminal_order)

    fault_id = case.get("fault_schedule_id")
    fault = fault_schedules[fault_id] if fault_id is not None else None
    return Schedule(
        job=job,
        workers=workers,
        base=base,
        ttls=ttls,
        contenders=(contenders[0], contenders[1]),
        action_order=tuple(actions),
        invalid_ttls=(invalid_ttls[0], invalid_ttls[1]),
        invalid_tick_order=tuple(invalid_tick_order),
        conflicting_terminal_order=tuple(conflicting_terminal_order),
        fault=fault,
    )
