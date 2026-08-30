#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import selectors
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

from model import (
    canonical,
    case_input_digest,
    concurrent_claim_relation,
    digest,
    expected_hashes,
)
from schedule import FaultSchedule, derive_schedule

PYTHON = sys.executable
SETPRIV = "/usr/bin/setpriv"
RUNNER_UID = 65532
RUNNER_GID = 65532
ALLOWED = {"README.md", "authority.py", "contract.toml", "test_public.py"}
OUTPUT_LIMIT = 64 * 1024
GATES = (
    "single-authority",
    "monotonic-fence",
    "stale-rejection",
    "restart-durability",
    "transaction-rollback",
    "terminal-idempotence",
)


def read_toml(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    value = tomllib.loads(data.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise ValueError("TOML root must be a table")
    return value


def validate_tree(root: Path) -> None:
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("missing or unsafe submission root")
    names: set[str] = set()
    total = 0
    for entry_count, entry in enumerate(os.scandir(root), start=1):
        if entry_count > len(ALLOWED):
            raise ValueError("submission contains too many root entries")
        if entry.name not in ALLOWED:
            raise ValueError("submission contains an unknown root entry")
        names.add(entry.name)
        item = root / entry.name
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("submission contains link, directory, or special file")
        if metadata.st_nlink != 1 or metadata.st_size > 1024 * 1024:
            raise ValueError("submission file budget exceeded")
        total += metadata.st_size
    if names != ALLOWED or total > 2 * 1024 * 1024:
        raise ValueError("submission paths or total budget mismatch")


def validate_manifests(
    contract_path: Path, cases_path: Path, workspace: Path
) -> tuple[list[dict[str, Any]], int, int, dict[str, FaultSchedule]]:
    if (workspace / "contract.toml").read_bytes() != contract_path.read_bytes():
        raise ValueError("submitted contract differs from immutable contract")
    contract = read_toml(contract_path)
    cases = read_toml(cases_path)
    if (
        contract.get("schema_version") != 1
        or contract.get("task_id") != "systems-design/authority-fencing"
    ):
        raise ValueError("contract identity mismatch")
    if cases.get("schema_version") != 2 or cases.get("task_id") != contract["task_id"]:
        raise ValueError("case identity mismatch")
    if contract.get("allowed_submission_paths") != sorted(ALLOWED):
        raise ValueError("allowed path contract mismatch")
    initial_files = contract.get("initial_workspace_files")
    if initial_files != ["README.md", "authority.py", "test_public.py"]:
        raise ValueError("initial workspace file scope mismatch")
    initial_manifest = contract.get("initial_workspace_manifest")
    if (
        not isinstance(initial_manifest, list)
        or [item.get("path") for item in initial_manifest] != initial_files
        or any(
            set(item) != {"path", "sha256"}
            or type(item["sha256"]) is not str
            or len(item["sha256"]) != 64
            for item in initial_manifest
        )
    ):
        raise ValueError("initial workspace manifest mismatch")
    if digest(initial_manifest) != contract.get("initial_workspace_sha256"):
        raise ValueError("initial workspace digest mismatch")
    hidden_digest = case_input_digest(cases)
    if hidden_digest != cases.get(
        "input_manifest_sha256"
    ) or hidden_digest != contract.get("hidden_case_input_sha256"):
        raise ValueError("hidden case input digest mismatch")
    commands = {item.get("id") for item in contract.get("cli", {}).get("commands", [])}
    if commands != {"create", "claim", "renew", "complete", "fail", "inspect"}:
        raise ValueError("CLI command contract mismatch")
    fault_entries = contract.get("faults", [])
    if not isinstance(fault_entries, list) or any(
        type(item) is not dict
        or set(item) != {"id", "exit_code", "effect"}
        or type(item["id"]) is not str
        or type(item["exit_code"]) is not int
        or type(item["effect"]) is not str
        for item in fault_entries
    ):
        raise ValueError("fault contract schema mismatch")
    fault_exit_codes = {item["id"]: item["exit_code"] for item in fault_entries}
    declared_fault_exit = (
        contract.get("cli", {}).get("output", {}).get("fault_exit_code")
    )
    if (
        len(fault_exit_codes) != len(fault_entries)
        or set(fault_exit_codes) != {"claim.precommit", "transition.precommit"}
        or type(declared_fault_exit) is not int
        or set(fault_exit_codes.values()) != {declared_fault_exit}
    ):
        raise ValueError("fault contract mismatch")
    gate_map = {
        item.get("id"): set(item.get("case_ids", []))
        for item in contract.get("gates", [])
    }
    if set(gate_map) != set(GATES):
        raise ValueError("gate contract mismatch")
    fixed = cases.get("cases", [])
    if not isinstance(fixed, list) or not 1 <= len(fixed) <= 16:
        raise ValueError("case count mismatch")
    case_ids = {item.get("id") for item in fixed}
    if len(case_ids) != len(fixed) or set().union(*gate_map.values()) != case_ids:
        raise ValueError("case reference mismatch")
    schedules = cases.get("fault_schedules", [])
    if not isinstance(schedules, list) or len(schedules) > 8:
        raise ValueError("fault schedule count mismatch")
    if any(
        type(item) is not dict
        or set(item) != {"id", "checkpoint_id", "exit_code"}
        or type(item["id"]) is not str
        or not item["id"]
        or type(item["checkpoint_id"]) is not str
        or type(item["exit_code"]) is not int
        for item in schedules
    ):
        raise ValueError("fault schedule schema mismatch")
    schedule_ids = {item["id"] for item in schedules}
    if len(schedule_ids) != len(schedules):
        raise ValueError("fault schedule reference mismatch")
    fault_schedules = {
        item["id"]: FaultSchedule(
            id=item["id"],
            checkpoint_id=item["checkpoint_id"],
            exit_code=item["exit_code"],
        )
        for item in schedules
    }
    if any(
        item.checkpoint_id not in fault_exit_codes
        or item.exit_code != fault_exit_codes.get(item.checkpoint_id)
        for item in fault_schedules.values()
    ):
        raise ValueError("fault schedule differs from public fault contract")
    for item in fixed:
        if set(item) not in (
            {
                "id",
                "scenario",
                "seed",
                "gate_ids",
                "expected_state_sha256",
                "expected_effect_sha256",
            },
            {
                "id",
                "scenario",
                "seed",
                "gate_ids",
                "fault_schedule_id",
                "expected_state_sha256",
                "expected_effect_sha256",
            },
        ):
            raise ValueError("case schema mismatch")
        if type(item["seed"]) is not int or item["seed"] < 0:
            raise ValueError("case seed mismatch")
        if item["scenario"] != item["id"]:
            raise ValueError("case scenario mismatch")
        if set(item["gate_ids"]) - set(GATES):
            raise ValueError("case gate reference mismatch")
        if (
            "fault_schedule_id" in item
            and item["fault_schedule_id"] not in schedule_ids
        ):
            raise ValueError("case fault reference mismatch")
        is_fault_case = item["scenario"] in {
            "claim-precommit-crash",
            "transition-precommit-crash",
        }
        if ("fault_schedule_id" in item) is not is_fault_case:
            raise ValueError("case fault schedule presence mismatch")
        for key in ("expected_state_sha256", "expected_effect_sha256"):
            value = item[key]
            if type(value) is not str or len(value) != 64 or value == "PLACEHOLDER":
                raise ValueError("case hash is not frozen")
        clock = contract.get("logical_clock", {})
        modeled_state, modeled_effect = expected_hashes(
            item, clock.get("minimum"), clock.get("maximum"), fault_schedules
        )
        if (
            item["expected_state_sha256"] != modeled_state
            or item["expected_effect_sha256"] != modeled_effect
        ):
            raise ValueError("case expectation differs from verifier model")
    for gate, ids in gate_map.items():
        if ids != {item["id"] for item in fixed if gate in item["gate_ids"]}:
            raise ValueError("gate-to-case mapping mismatch")
    clock = contract["logical_clock"]
    minimum = clock.get("minimum")
    maximum = clock.get("maximum")
    if type(minimum) is not int or type(maximum) is not int or minimum != 0:
        raise ValueError("logical clock domain mismatch")
    return fixed, minimum, maximum, fault_schedules


def limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))


def runner_processes() -> list[int]:
    found: list[int] = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            lines = (item / "status").read_text().splitlines()
            uid = next(line for line in lines if line.startswith("Uid:"))
            if int(uid.split()[1]) == RUNNER_UID:
                found.append(int(item.name))
        except (FileNotFoundError, PermissionError, StopIteration, ValueError):
            pass
    return sorted(found)


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def bounded_process_output(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    output_limit: int = OUTPUT_LIMIT,
    drain_timeout: float = 0.5,
) -> tuple[bytes, bytes]:
    """Collect both child pipes without retaining more than output_limit bytes."""
    if process.stdout is None or process.stderr is None:
        raise ValueError("submitted CLI pipes unavailable")
    streams = {
        process.stdout.fileno(): bytearray(),
        process.stderr.fileno(): bytearray(),
    }
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    failure: str | None = None
    killed = False
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            now = time.monotonic()
            if failure is None and now >= deadline:
                failure = "submitted CLI timed out"
            if process.poll() is not None and not killed:
                killed = True
                kill_process_group(process)
                deadline = min(deadline, now + drain_timeout)
            if failure is not None and not killed:
                killed = True
                kill_process_group(process)
                deadline = now + drain_timeout
            if killed and now >= deadline:
                break
            wait = max(0.0, min(0.05, deadline - now))
            for key, _events in selector.select(wait):
                stream = key.fileobj
                try:
                    chunk = os.read(key.fd, 16 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                retained = sum(len(value) for value in streams.values())
                remaining = max(0, output_limit - retained)
                if remaining:
                    streams[key.fd].extend(chunk[:remaining])
                if len(chunk) > remaining and failure is None:
                    failure = "submitted CLI output exceeded limit"
        if failure is not None:
            raise ValueError(failure)
        return bytes(streams[process.stdout.fileno()]), bytes(
            streams[process.stderr.fileno()]
        )
    finally:
        kill_process_group(process)
        selector.close()
        process.stdout.close()
        process.stderr.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            kill_process_group(process)
            process.wait(timeout=2)


class Harness:
    def __init__(self, program: Path, root: Path):
        self.program = program
        self.root = root
        self.effects: list[dict[str, Any]] = []
        self.effects_lock = threading.Lock()

    def invoke(
        self,
        db: Path,
        *arguments: str,
        expected: int | None = 0,
        expected_idempotent: bool = False,
        trace_arguments: dict[str, Any],
    ) -> tuple[int, dict[str, Any] | None]:
        command = [
            SETPRIV,
            f"--reuid={RUNNER_UID}",
            f"--regid={RUNNER_GID}",
            "--clear-groups",
            "--no-new-privs",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            PYTHON,
            str(self.program),
            "--db",
            str(db),
            *arguments,
        ]
        process = subprocess.Popen(
            command,
            cwd=self.root,
            env={
                "HOME": str(self.root),
                "LANG": "C.UTF-8",
                "PATH": "",
                "TMPDIR": str(self.root),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=limits,
        )
        stdout, stderr = bounded_process_output(process, timeout=10)
        if expected is not None and process.returncode != expected:
            raise ValueError(
                f"unexpected CLI exit: {arguments[0]}:{process.returncode}"
            )
        value: dict[str, Any] | None = None
        output = stdout if process.returncode == 0 else stderr
        if process.returncode in {0, 2}:
            if process.returncode == 0 and stderr:
                raise ValueError("successful CLI wrote stderr")
            if process.returncode == 2 and stdout:
                raise ValueError("rejected CLI wrote stdout")
            try:
                parsed = json.loads(output.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("submitted CLI emitted invalid JSON") from exc
            if type(parsed) is not dict or output != canonical(parsed) + b"\n":
                raise ValueError("submitted CLI output is noncanonical")
            value = parsed
            if process.returncode == 0:
                direct = read_state(db)
                if (
                    set(value) != {"idempotent", "job", "ok"}
                    or type(value["idempotent"]) is not bool
                    or value["idempotent"] is not expected_idempotent
                    or value.get("ok") is not True
                    or value.get("job") != direct
                ):
                    raise ValueError("CLI output does not match durable state")
            elif (
                set(value) != {"error", "ok"}
                or type(value.get("error")) is not str
                or not value["error"]
                or value.get("ok") is not False
            ):
                raise ValueError("rejection output mismatch")
        elif stdout or stderr:
            raise ValueError("faulting CLI emitted output")
        with self.effects_lock:
            self.effects.append(
                {
                    "arguments": trace_arguments,
                    "exit_code": process.returncode,
                    "idempotent": value.get("idempotent")
                    if process.returncode == 0 and value is not None
                    else None,
                    "operation": arguments[0],
                }
            )
        return process.returncode, value


def read_state(db: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        tables = connection.execute(
            "SELECT name,type FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if [(row["name"], row["type"]) for row in tables] != [("jobs", "table")]:
            raise ValueError("durable schema mismatch")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(jobs)")]
        if columns != [
            "job_id",
            "status",
            "worker_id",
            "fence_token",
            "deadline",
            "terminal_op",
        ]:
            raise ValueError("job columns mismatch")
        rows = connection.execute(
            "SELECT job_id,status,worker_id,fence_token,deadline,terminal_op FROM jobs"
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("expected exactly one durable job")
        row = rows[0]
        return {
            "deadline": row["deadline"],
            "fence_token": row["fence_token"],
            "job_id": row["job_id"],
            "status": row["status"],
            "terminal_op": row["terminal_op"],
            "worker_id": row["worker_id"],
        }
    finally:
        connection.close()


def db_digest(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def verify_concurrent_claim_outcome(
    results: list[tuple[str, int]],
    state: dict[str, Any],
    contenders: tuple[str, str],
) -> dict[str, Any]:
    workers = [worker for worker, _code in results]
    if len(results) != 2 or len(set(workers)) != 2 or set(workers) != set(contenders):
        raise ValueError("concurrent claim identity differs from seeded schedule")
    winners = [worker for worker, code in results if code == 0]
    losers = [worker for worker, code in results if code == 2]
    if len(winners) != 1 or len(losers) != 1:
        raise ValueError("concurrent claim did not produce one winner")
    winner, loser = winners[0], losers[0]
    token = state.get("fence_token")
    durable_owner_token_match = state.get("worker_id") == winner and token == 2
    if not durable_owner_token_match:
        raise ValueError("concurrent claim durable winner mismatch")
    return {
        "durable_owner_token_match": durable_owner_token_match,
        "loser_worker_id": loser,
        "winner_worker_id": winner,
        "winning_fence_token": token,
    }


def create(h: Harness, db: Path, job: str) -> None:
    h.invoke(db, "create", "--job", job, trace_arguments={"job": job})


def inspect(h: Harness, db: Path, job: str) -> None:
    h.invoke(db, "inspect", "--job", job, trace_arguments={"job": job})


def claim(
    h: Harness,
    db: Path,
    job: str,
    worker: str,
    now: int,
    ttl: int,
    *extra: str,
    expected: int | None = 0,
    trace_worker: str | None = None,
) -> tuple[int, dict[str, Any] | None]:
    trace_arguments: dict[str, Any] = {
        "job": job,
        "now": now,
        "ttl": ttl,
        "worker": trace_worker or worker,
    }
    if extra:
        if len(extra) != 2 or extra[0] != "--fault":
            raise ValueError("unsupported claim trace arguments")
        trace_arguments["fault"] = extra[1]
    return h.invoke(
        db,
        "claim",
        "--job",
        job,
        "--worker",
        worker,
        "--now",
        str(now),
        "--ttl",
        str(ttl),
        *extra,
        expected=expected,
        trace_arguments=trace_arguments,
    )


def mutate(
    h: Harness,
    db: Path,
    op: str,
    job: str,
    worker: str,
    token: int,
    now: int,
    *extra: str,
    ttl: int | None = None,
    expected: int | None = 0,
    expected_idempotent: bool = False,
) -> tuple[int, dict[str, Any] | None]:
    arguments = [
        op,
        "--job",
        job,
        "--worker",
        worker,
        "--token",
        str(token),
        "--now",
        str(now),
    ]
    if ttl is not None:
        arguments.extend(("--ttl", str(ttl)))
    arguments.extend(extra)
    trace_arguments: dict[str, Any] = {
        "job": job,
        "now": now,
        "token": token,
        "worker": worker,
    }
    if ttl is not None:
        trace_arguments["ttl"] = ttl
    if extra:
        if len(extra) != 2 or extra[0] != "--fault":
            raise ValueError("unsupported mutation trace arguments")
        trace_arguments["fault"] = extra[1]
    return h.invoke(
        db,
        *arguments,
        expected=expected,
        expected_idempotent=expected_idempotent,
        trace_arguments=trace_arguments,
    )


def run_case(
    case: dict[str, Any],
    program: Path,
    root: Path,
    minimum: int,
    maximum: int,
    fault_schedules: dict[str, FaultSchedule],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    values = derive_schedule(case, maximum, fault_schedules)
    job = values.job
    workers = values.workers
    ttls = values.ttls
    now = values.base
    db = root / "state.sqlite3"
    h = Harness(program, root)
    create(h, db, job)
    scenario = case["scenario"]
    concurrent_claim: dict[str, Any] | None = None

    def reject_stable(call: Any) -> None:
        before_bytes = db.read_bytes()
        before_state = read_state(db)
        call()
        if db.read_bytes() != before_bytes or read_state(db) != before_state:
            raise ValueError("rejected action changed durable state")

    if scenario == "concurrent-expiry-claim":
        claim(h, db, job, workers[0], now, ttls[0])
        expiry = now + ttls[0]
        effect_start = len(h.effects)
        results: list[tuple[str, int]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def contender(worker: str) -> None:
            barrier.wait()
            try:
                code, _ = claim(
                    h,
                    db,
                    job,
                    worker,
                    expiry,
                    ttls[1],
                    expected=None,
                )
            except Exception:
                code = -1
            with lock:
                results.append((worker, code))

        threads = [
            threading.Thread(target=contender, args=(worker,))
            for worker in values.contenders
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        if any(thread.is_alive() for thread in threads):
            raise ValueError("concurrent claim did not produce one winner")
        concurrent_claim = verify_concurrent_claim_outcome(
            results, read_state(db), values.contenders
        )
        winner = concurrent_claim["winner_worker_id"]
        loser = concurrent_claim["loser_worker_id"]
        with h.effects_lock:
            h.effects[effect_start:] = [
                concurrent_claim_relation(h.effects[effect_start:], winner, loser)
            ]
    elif scenario in {"different-worker-reclaim", "same-worker-reclaim"}:
        sequence = (
            [workers[0], workers[1], workers[2]]
            if scenario.startswith("different")
            else [workers[0]] * 3
        )
        for worker, ttl in zip(sequence, ttls, strict=False):
            claim(h, db, job, worker, now, ttl)
            now += ttl
        if read_state(db)["fence_token"] != 3:
            raise ValueError("fence token did not increase on every reclaim")
    elif scenario == "delayed-stale-actions":
        claim(h, db, job, workers[0], now, ttls[0])
        expiry = now + ttls[0]
        claim(h, db, job, workers[1], expiry, ttls[1])
        for op in values.action_order:
            reject_stable(
                lambda op=op: mutate(
                    h,
                    db,
                    op,
                    job,
                    workers[0],
                    1,
                    expiry + 1,
                    ttl=ttls[2] if op == "renew" else None,
                    expected=2,
                )
            )
    elif scenario == "exact-expiry-rejection":
        claim(h, db, job, workers[0], now, ttls[0])
        expiry = now + ttls[0]
        for op in values.action_order:
            reject_stable(
                lambda op=op: mutate(
                    h,
                    db,
                    op,
                    job,
                    workers[0],
                    1,
                    expiry,
                    ttl=ttls[1] if op == "renew" else None,
                    expected=2,
                )
            )
    elif scenario == "invalid-ttl-rejection":
        claim(h, db, job, workers[0], now, ttls[0])
        for ttl in values.invalid_ttls:
            reject_stable(
                lambda ttl=ttl: mutate(
                    h,
                    db,
                    "renew",
                    job,
                    workers[0],
                    1,
                    now + 1,
                    ttl=ttl,
                    expected=2,
                )
            )
        expiry = now + ttls[0]
        for ttl in values.invalid_ttls:
            reject_stable(
                lambda ttl=ttl: claim(h, db, job, workers[1], expiry, ttl, expected=2)
            )
    elif scenario == "invalid-tick-overflow-rejection":
        claim(h, db, job, workers[0], now, ttls[0])
        for action in values.invalid_tick_order:
            if action == "negative-renew":

                def call() -> None:
                    mutate(
                        h,
                        db,
                        "renew",
                        job,
                        workers[0],
                        1,
                        -1,
                        ttl=ttls[1],
                        expected=2,
                    )

            elif action == "overflow-complete":

                def call() -> None:
                    mutate(
                        h,
                        db,
                        "complete",
                        job,
                        workers[0],
                        1,
                        maximum + 1,
                        expected=2,
                    )

            else:

                def call() -> None:
                    claim(h, db, job, workers[1], maximum, 1, expected=2)

            reject_stable(call)
    elif scenario == "shorter-renewal":
        long_ttl = max(ttls[0], ttls[1]) + 8
        claim(h, db, job, workers[0], now, long_ttl)
        before_deadline = read_state(db)["deadline"]
        mutate(h, db, "renew", job, workers[0], 1, now + 1, ttl=2)
        if read_state(db)["deadline"] != before_deadline:
            raise ValueError("short renewal decreased deadline")
    elif scenario == "tick-domain-boundaries":
        claim(h, db, job, workers[0], minimum, ttls[0])
        claim(h, db, job, workers[1], minimum + ttls[0], ttls[1])
        near_max = maximum - ttls[2]
        claim(h, db, job, workers[2], near_max, ttls[2])
        reject_stable(
            lambda: mutate(
                h,
                db,
                "complete",
                job,
                workers[2],
                3,
                maximum,
                expected=2,
            )
        )
    elif scenario == "active-restart":
        claim(h, db, job, workers[0], now, ttls[0])
        inspect(h, db, job)
        mutate(h, db, "renew", job, workers[0], 1, now + 1, ttl=ttls[1])
        inspect(h, db, job)
    elif scenario == "terminal-restart":
        claim(h, db, job, workers[0], now, ttls[0])
        mutate(h, db, "fail", job, workers[0], 1, now + 1)
        inspect(h, db, job)
    elif scenario == "claim-precommit-crash":
        if values.fault is None:
            raise ValueError("claim crash case has no fault schedule")
        before = db_digest(db)
        claim(
            h,
            db,
            job,
            workers[0],
            now,
            ttls[0],
            "--fault",
            values.fault.checkpoint_id,
            expected=values.fault.exit_code,
        )
        if db_digest(db) != before:
            raise ValueError("faulting claim did not roll back bytes")
        claim(h, db, job, workers[1], now + 1, ttls[1])
    elif scenario == "transition-precommit-crash":
        if values.fault is None:
            raise ValueError("transition crash case has no fault schedule")
        claim(h, db, job, workers[0], now, ttls[0])
        before = db_digest(db)
        mutate(
            h,
            db,
            "complete",
            job,
            workers[0],
            1,
            now + 1,
            "--fault",
            values.fault.checkpoint_id,
            expected=values.fault.exit_code,
        )
        if db_digest(db) != before:
            raise ValueError("faulting transition did not roll back bytes")
        mutate(h, db, "complete", job, workers[0], 1, now + 2)
    elif scenario == "matching-terminal-replay":
        claim(h, db, job, workers[0], now, ttls[0])
        mutate(h, db, "complete", job, workers[0], 1, now + 1)
        before = db_digest(db)
        _, output = mutate(
            h,
            db,
            "complete",
            job,
            workers[0],
            1,
            now + ttls[0],
            expected_idempotent=True,
        )
        if (
            output is None
            or output.get("idempotent") is not True
            or db_digest(db) != before
        ):
            raise ValueError("matching terminal replay was not byte-stable idempotence")
    elif scenario == "conflicting-terminal-rejection":
        claim(h, db, job, workers[0], now, ttls[0])
        mutate(h, db, "complete", job, workers[0], 1, now + 1)
        for op, worker_index, token in values.conflicting_terminal_order:
            worker = workers[worker_index]
            reject_stable(
                lambda op=op, worker=worker, token=token: mutate(
                    h, db, op, job, worker, token, now + 2, expected=2
                )
            )
    else:
        raise ValueError("unknown hidden case")
    leftovers = runner_processes()
    if leftovers:
        for pid in leftovers:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise ValueError("submitted CLI left a background process")
    return read_state(db), h.effects, concurrent_claim


def network_probe(kind: str, host: str) -> dict[str, Any]:
    if kind == "dns":
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, 443)})
            return {"addresses": addresses, "kind": kind, "resolved": True}
        except OSError as exc:
            return {
                "addresses": [],
                "error": type(exc).__name__,
                "kind": kind,
                "resolved": False,
            }
    try:
        with socket.create_connection((host, 443), timeout=1) as connection:
            connection.sendall(b"\x00")
            connection.recv(1)
    except OSError as exc:
        return {"blocked": True, "error": type(exc).__name__, "kind": kind}
    raise ValueError(f"verifier network probe unexpectedly succeeded: {kind}")


def runtime_evidence() -> dict[str, Any]:
    cgroup = Path("/sys/fs/cgroup")
    return {
        "cgroup": {
            name: (cgroup / name).read_text().strip()
            if (cgroup / name).is_file()
            else "unavailable"
            for name in ("cpu.max", "memory.max", "pids.max")
        },
        "mounts": {
            "artifacts": Path("/artifacts").is_dir(),
            "docker_socket": Path("/var/run/docker.sock").exists(),
            "tests": Path("/tests").is_dir(),
            "workspace": Path("/workspace").is_dir(),
        },
        "network_probes": [
            network_probe("dns", "example.com"),
            network_probe("direct-ip-tcp", "1.1.1.1"),
            network_probe("hostname-tcp", "example.com"),
        ],
        "orchestrator": {"gid": os.getgid(), "uid": os.getuid()},
        "runner": {"gid": RUNNER_GID, "uid": RUNNER_UID},
        "runner_processes_before": runner_processes(),
        "socket_files": [
            str(path) for path in (Path("/var/run/docker.sock"),) if path.exists()
        ],
    }


def verify(workspace: Path, contract: Path, cases: Path) -> dict[str, Any]:
    validate_tree(workspace)
    fixed, minimum, maximum, fault_schedules = validate_manifests(
        contract, cases, workspace
    )
    if not Path(SETPRIV).is_file() or runner_processes():
        raise ValueError("unprivileged runner boundary unavailable")
    runtime = runtime_evidence()
    if runtime["mounts"]["docker_socket"]:
        raise ValueError("verifier has Docker socket access")
    results: list[dict[str, Any]] = []
    concurrent_claim: dict[str, Any] | None = None
    gate_cases: dict[str, list[bool]] = {gate: [] for gate in GATES}
    with tempfile.TemporaryDirectory(prefix="authority-verifier-") as directory:
        scratch = Path(directory)
        os.chmod(scratch, 0o711)
        for item in fixed:
            case_root = scratch / item["id"]
            case_root.mkdir(mode=0o700)
            os.chown(case_root, RUNNER_UID, RUNNER_GID)
            program_root = case_root / "program"
            program_root.mkdir(mode=0o700)
            shutil.copyfile(workspace / "authority.py", program_root / "authority.py")
            shutil.copyfile(contract, program_root / "contract.toml")
            os.chown(program_root, RUNNER_UID, RUNNER_GID)
            os.chown(program_root / "authority.py", RUNNER_UID, RUNNER_GID)
            os.chown(program_root / "contract.toml", RUNNER_UID, RUNNER_GID)
            os.chmod(program_root, 0o700)
            os.chmod(program_root / "authority.py", 0o400)
            os.chmod(program_root / "contract.toml", 0o400)
            passed = False
            state_hash = "0" * 64
            effect_hash = "0" * 64
            error = ""
            try:
                state, effects, case_concurrent_claim = run_case(
                    item,
                    program_root / "authority.py",
                    case_root,
                    minimum,
                    maximum,
                    fault_schedules,
                )
                if item["id"] == "concurrent-expiry-claim":
                    if case_concurrent_claim is None or concurrent_claim is not None:
                        raise ValueError("concurrent claim evidence mismatch")
                    concurrent_claim = case_concurrent_claim
                    state = {**state, "worker_id": "winner"}
                elif case_concurrent_claim is not None:
                    raise ValueError("unexpected concurrent claim evidence")
                state_hash = digest(state)
                effect_hash = digest(effects)
                passed = (
                    state_hash == item["expected_state_sha256"]
                    and effect_hash == item["expected_effect_sha256"]
                )
                if not passed:
                    error = "expected hash mismatch"
            except Exception as exc:
                error = type(exc).__name__
            leftovers = runner_processes()
            if leftovers:
                for pid in leftovers:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                passed = False
                error = "background-process"
            results.append(
                {
                    "effect_hash_match": int(
                        effect_hash == item["expected_effect_sha256"]
                    ),
                    "error": error,
                    "id": item["id"],
                    "passed": int(passed),
                    "state_hash_match": int(
                        state_hash == item["expected_state_sha256"]
                    ),
                }
            )
            for gate in item["gate_ids"]:
                gate_cases[gate].append(passed)
    gates = {
        gate: int(bool(values) and all(values)) for gate, values in gate_cases.items()
    }
    ok = all(gates.values()) and not runner_processes()
    return {
        "cases": results,
        "concurrent_claim": concurrent_claim,
        "gates": gates,
        "mandatory_gate_count": len(GATES),
        "mandatory_gate_pass_count": sum(gates.values()),
        "ok": ok,
        "runtime": runtime,
        "schema_version": 1,
        "task_id": "systems-design/authority-fencing",
    }


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--reward", type=Path, required=True)
    args = parser.parse_args()
    args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(args.diagnostics.parent, 0o700)
    try:
        diagnostics = verify(args.workspace, args.contract, args.cases)
    except Exception as exc:
        diagnostics = {
            "error": type(exc).__name__,
            "gates": {gate: 0 for gate in GATES},
            "mandatory_gate_count": len(GATES),
            "mandatory_gate_pass_count": 0,
            "ok": False,
            "schema_version": 1,
            "task_id": "systems-design/authority-fencing",
        }
    reward = 1 if diagnostics["ok"] else 0
    atomic_write(args.diagnostics, canonical(diagnostics) + b"\n")
    atomic_write(args.reward, canonical({"reward": reward}) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
