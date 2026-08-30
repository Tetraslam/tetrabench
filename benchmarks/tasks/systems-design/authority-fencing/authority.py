#!/usr/bin/env python3
"""Lease queue skeleton. Complete the authority and failure-path logic."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Never


class Rejected(Exception):
    pass


class ContractParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise Rejected("invalid arguments")


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            worker_id TEXT,
            fence_token INTEGER NOT NULL,
            deadline INTEGER,
            terminal_op TEXT
        )
        """
    )
    return connection


def state(row: sqlite3.Row) -> dict[str, object]:
    return {
        "deadline": row["deadline"],
        "fence_token": row["fence_token"],
        "job_id": row["job_id"],
        "status": row["status"],
        "terminal_op": row["terminal_op"],
        "worker_id": row["worker_id"],
    }


def row_for(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise Rejected("unknown job")
    return row


def execute(args: argparse.Namespace) -> dict[str, object]:
    with connect(args.db) as connection:
        if args.command == "create":
            connection.execute(
                "INSERT INTO jobs VALUES (?, 'pending', NULL, 0, NULL, NULL)",
                (args.job,),
            )
        elif args.command == "claim":
            row = row_for(connection, args.job)
            if row["status"] != "pending":
                raise Rejected("job is not claimable")
            connection.execute(
                "UPDATE jobs SET status='running', worker_id=?, fence_token=1, "
                "deadline=? WHERE job_id=?",
                (args.worker, args.now + args.ttl, args.job),
            )
        elif args.command == "renew":
            row = row_for(connection, args.job)
            if row["status"] != "running" or row["worker_id"] != args.worker:
                raise Rejected("lease rejected")
            connection.execute(
                "UPDATE jobs SET deadline=? WHERE job_id=?",
                (args.now + args.ttl, args.job),
            )
        elif args.command in {"complete", "fail"}:
            row = row_for(connection, args.job)
            if row["status"] in {"completed", "failed"}:
                if (
                    row["terminal_op"] == args.command
                    and row["worker_id"] == args.worker
                    and row["fence_token"] == args.token
                ):
                    return {
                        "idempotent": True,
                        "job": state(row),
                        "ok": True,
                    }
                raise Rejected("terminal operation conflicts")
            if row["status"] != "running" or row["worker_id"] != args.worker:
                raise Rejected("lease rejected")
            connection.execute(
                "UPDATE jobs SET status=?, terminal_op=? WHERE job_id=?",
                (
                    "completed" if args.command == "complete" else "failed",
                    args.command,
                    args.job,
                ),
            )
        return {
            "idempotent": False,
            "job": state(row_for(connection, args.job)),
            "ok": True,
        }


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
    for name in ("renew", "complete", "fail"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
        command.add_argument("--worker", required=True)
        command.add_argument("--token", type=int, required=True)
        command.add_argument("--now", type=int, required=True)
        if name == "renew":
            command.add_argument("--ttl", type=int, required=True)
        else:
            command.add_argument("--fault", choices=["transition.precommit"])
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--job", required=True)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        print(json.dumps(execute(args), sort_keys=True, separators=(",", ":")))
        return 0
    except (Rejected, sqlite3.IntegrityError, ValueError) as exc:
        print(
            json.dumps(
                {"error": str(exc), "ok": False}, sort_keys=True, separators=(",", ":")
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
