#!/bin/sh
set -eu

cat > /workspace/authority.py <<'PY'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tomllib
from pathlib import Path
from typing import Never


class Rejected(Exception):
    pass


class ContractParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise Rejected("invalid arguments")


def clock_domain() -> tuple[int, int]:
    contract = tomllib.loads(Path(__file__).with_name("contract.toml").read_text())
    clock = contract["logical_clock"]
    minimum = clock["minimum"]
    maximum = clock["maximum"]
    if type(minimum) is not int or type(maximum) is not int or minimum != 0:
        raise Rejected("invalid clock contract")
    return minimum, maximum


def tick(value: int, label: str) -> int:
    minimum, maximum = clock_domain()
    if type(value) is not int or value < minimum or value > maximum:
        raise Rejected(f"invalid {label}")
    return value


def deadline(now: int, ttl: int) -> int:
    tick(now, "now")
    maximum = clock_domain()[1]
    if type(ttl) is not int or ttl <= 0 or ttl > maximum - now:
        raise Rejected("invalid ttl")
    return now + ttl


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
            worker_id TEXT,
            fence_token INTEGER NOT NULL CHECK(fence_token >= 0),
            deadline INTEGER,
            terminal_op TEXT CHECK(terminal_op IS NULL OR terminal_op IN ('complete','fail'))
        )
        """
    )
    return connection


def begin(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def get(connection: sqlite3.Connection, job: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT job_id,status,worker_id,fence_token,deadline,terminal_op FROM jobs WHERE job_id=?",
        (job,),
    ).fetchone()
    if row is None:
        raise Rejected("unknown job")
    return row


def state(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in (
        "deadline", "fence_token", "job_id", "status", "terminal_op", "worker_id"
    )}


def current(row: sqlite3.Row, worker: str, token: int, now: int) -> bool:
    return (
        row["status"] == "running"
        and row["worker_id"] == worker
        and row["fence_token"] == token
        and now < row["deadline"]
    )


def execute(args: argparse.Namespace) -> dict[str, object]:
    connection = connect(args.db)
    try:
        if args.command == "inspect":
            return {"idempotent": False, "job": state(get(connection, args.job)), "ok": True}
        begin(connection)
        if args.command == "create":
            connection.execute(
                "INSERT INTO jobs(job_id,status,worker_id,fence_token,deadline,terminal_op) VALUES(?,'pending',NULL,0,NULL,NULL)",
                (args.job,),
            )
            idempotent = False
        elif args.command == "claim":
            new_deadline = deadline(args.now, args.ttl)
            row = get(connection, args.job)
            if row["status"] in {"completed", "failed"}:
                raise Rejected("job is terminal")
            if row["status"] == "running" and args.now < row["deadline"]:
                raise Rejected("job has a current lease")
            token = row["fence_token"] + 1
            if token > clock_domain()[1]:
                raise Rejected("fence token exhausted")
            connection.execute(
                "UPDATE jobs SET status='running',worker_id=?,fence_token=?,deadline=?,terminal_op=NULL WHERE job_id=?",
                (args.worker, token, new_deadline, args.job),
            )
            if args.fault == "claim.precommit":
                os._exit(86)
            idempotent = False
        elif args.command == "renew":
            now = tick(args.now, "now")
            new_deadline = deadline(now, args.ttl)
            token = tick(args.token, "token")
            row = get(connection, args.job)
            if not current(row, args.worker, token, now):
                raise Rejected("lease rejected")
            connection.execute(
                "UPDATE jobs SET deadline=? WHERE job_id=?",
                (max(row["deadline"], new_deadline), args.job),
            )
            idempotent = False
        else:
            now = tick(args.now, "now")
            token = tick(args.token, "token")
            row = get(connection, args.job)
            if row["status"] in {"completed", "failed"}:
                if (
                    row["terminal_op"] == args.command
                    and row["worker_id"] == args.worker
                    and row["fence_token"] == token
                ):
                    connection.rollback()
                    return {"idempotent": True, "job": state(row), "ok": True}
                raise Rejected("terminal operation conflicts")
            if not current(row, args.worker, token, now):
                raise Rejected("lease rejected")
            connection.execute(
                "UPDATE jobs SET status=?,terminal_op=? WHERE job_id=?",
                ("completed" if args.command == "complete" else "failed", args.command, args.job),
            )
            if args.fault == "transition.precommit":
                os._exit(86)
            idempotent = False
        connection.commit()
        result = {"idempotent": idempotent, "job": state(get(connection, args.job)), "ok": True}
        return result
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    result = ContractParser()
    result.add_argument("--db", type=Path, required=True)
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--job", required=True)
    claim = commands.add_parser("claim")
    claim.add_argument("--job", required=True)
    claim.add_argument("--worker", required=True)
    claim.add_argument("--now", type=int, required=True)
    claim.add_argument("--ttl", type=int, required=True)
    claim.add_argument("--fault", choices=["claim.precommit"])
    renew = commands.add_parser("renew")
    renew.add_argument("--job", required=True)
    renew.add_argument("--worker", required=True)
    renew.add_argument("--token", type=int, required=True)
    renew.add_argument("--now", type=int, required=True)
    renew.add_argument("--ttl", type=int, required=True)
    for name in ("complete", "fail"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
        command.add_argument("--worker", required=True)
        command.add_argument("--token", type=int, required=True)
        command.add_argument("--now", type=int, required=True)
        command.add_argument("--fault", choices=["transition.precommit"])
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--job", required=True)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        result = execute(args)
        print(json.dumps(result, allow_nan=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (Rejected, sqlite3.Error, OSError, ValueError) as exc:
        print(
            json.dumps({"error": str(exc), "ok": False}, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
PY
chmod 0644 /workspace/authority.py
python /workspace/test_public.py
