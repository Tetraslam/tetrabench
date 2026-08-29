#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

OID_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{15,63}$")
TRANSITION_KEYS = {
    "base",
    "head",
    "head_oid",
    "request_id",
    "schema_version",
    "type",
}
MAX_REQUEST_BYTES = 4096


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(data: bytes, *, newline: bool) -> Any:
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ForgeError("invalid JSON") from exc
    expected = canonical(value) + (b"\n" if newline else b"")
    if data != expected:
        raise ForgeError("JSON is not canonical")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ForgeError(ValueError):
    pass


class ForgeStore:
    def __init__(
        self,
        database: Path,
        initial_state_path: Path,
        capability: str | None = None,
    ):
        self.database = database
        self.initial_state_path = initial_state_path
        self.capability = capability or os.environ.get("FORGE_RUN_CAPABILITY", "")
        if len(self.capability) < 32:
            raise ForgeError("run capability is unavailable")
        database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initial_state(self) -> dict[str, Any]:
        state = strict_json(self.initial_state_path.read_bytes(), newline=True)
        if not isinstance(state, dict) or set(state) != {
            "base_oid",
            "base_ref",
            "pull_request",
            "schema_version",
        }:
            raise ForgeError("invalid immutable initial state")
        if (
            type(state["schema_version"]) is not int
            or state["schema_version"] != 1
            or type(state["base_oid"]) is not str
            or not OID_RE.fullmatch(state["base_oid"])
            or state["base_ref"] != "main"
            or state["pull_request"] is not None
        ):
            raise ForgeError("invalid immutable initial state")
        return state

    def _initialize(self) -> None:
        initial = canonical(self._initial_state())
        capability_hash = digest(self.capability.encode("utf-8")).encode("ascii")
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                  key TEXT PRIMARY KEY,
                  value BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                  sequence INTEGER PRIMARY KEY,
                  request_id TEXT NOT NULL UNIQUE,
                  event_json BLOB NOT NULL,
                  event_hash TEXT NOT NULL UNIQUE
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('initial', ?)",
                (initial,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('capability', ?)",
                (capability_hash,),
            )
            stored = dict(connection.execute("SELECT key, value FROM metadata"))
            if (
                stored["initial"] != initial
                or stored.get("capability") != capability_hash
            ):
                raise ForgeError("immutable forge authority changed")

    @staticmethod
    def _validate_transition(transition: Any, initial: dict[str, Any]) -> None:
        if not isinstance(transition, dict) or set(transition) != TRANSITION_KEYS:
            raise ForgeError("transition schema mismatch")
        if (
            type(transition["schema_version"]) is not int
            or transition["schema_version"] != 1
        ):
            raise ForgeError("unsupported transition schema")
        if transition["type"] not in {"pull_request.opened", "pull_request.submitted"}:
            raise ForgeError("unsupported transition type")
        if transition["base"] != initial["base_ref"]:
            raise ForgeError("base does not match current state")
        if transition["head"] != "feature" or transition["head"] == transition["base"]:
            raise ForgeError("invalid head")
        if type(transition["head_oid"]) is not str or not OID_RE.fullmatch(
            transition["head_oid"]
        ):
            raise ForgeError("head_oid must be 40 lowercase hex characters")
        if transition["head_oid"] == initial["base_oid"]:
            raise ForgeError("head_oid must advance current state")
        if type(transition["request_id"]) is not str or not REQUEST_ID_RE.fullmatch(
            transition["request_id"]
        ):
            raise ForgeError("invalid unique request_id")

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        transition: dict[str, Any],
        previous: str,
        sequence: int,
    ) -> dict[str, Any]:
        core = {
            "prev_hash": previous,
            "sequence": sequence,
            "transition": transition,
        }
        event = {**core, "event_hash": digest(canonical(core))}
        try:
            connection.execute(
                "INSERT INTO events(sequence, request_id, event_json, event_hash) "
                "VALUES (?, ?, ?, ?)",
                (
                    sequence,
                    transition["request_id"],
                    canonical(event),
                    event["event_hash"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ForgeError("request_id replayed") from exc
        return event

    def apply_transition(self, transition: Any, capability: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM metadata WHERE key='terminal'"
            ).fetchone():
                raise ForgeError("forge is terminal and sealed")
            expected_capability = connection.execute(
                "SELECT value FROM metadata WHERE key='capability'"
            ).fetchone()
            if expected_capability is None or expected_capability["value"] != digest(
                capability.encode("utf-8")
            ).encode("ascii"):
                raise ForgeError("invalid or revoked run capability")
            initial = strict_json(
                connection.execute(
                    "SELECT value FROM metadata WHERE key='initial'"
                ).fetchone()[0],
                newline=False,
            )
            self._validate_transition(transition, initial)
            rows = list(
                connection.execute(
                    "SELECT event_json, event_hash FROM events ORDER BY sequence"
                )
            )
            events = [strict_json(row["event_json"], newline=False) for row in rows]
            previous = digest(canonical(initial))
            for sequence, (row, event) in enumerate(zip(rows, events, strict=True), 1):
                if not isinstance(event, dict) or set(event) != {
                    "event_hash",
                    "prev_hash",
                    "sequence",
                    "transition",
                }:
                    raise ForgeError("stored event schema mismatch")
                self._validate_transition(event["transition"], initial)
                core = {
                    "prev_hash": event["prev_hash"],
                    "sequence": event["sequence"],
                    "transition": event["transition"],
                }
                if (
                    type(event["sequence"]) is not int
                    or event["sequence"] != sequence
                    or event["prev_hash"] != previous
                    or event["event_hash"] != digest(canonical(core))
                    or row["event_hash"] != event["event_hash"]
                ):
                    raise ForgeError("stored event chain mismatch")
                previous = event["event_hash"]
            expected_type = (
                "pull_request.opened" if not events else "pull_request.submitted"
            )
            if transition["type"] != expected_type:
                raise ForgeError("transition does not match current state")
            if events and any(
                transition[field] != events[0]["transition"][field]
                for field in ("base", "head", "head_oid")
            ):
                raise ForgeError("final transition changed pull request state")
            event = self._append_event(
                connection, transition, previous, len(events) + 1
            )
            if transition["type"] == "pull_request.submitted":
                complete = [*events, event]
                snapshot, files, export_record = self._sealed_export(initial, complete)
                if snapshot["event_count"] != 2:
                    raise ForgeError("complete state is invalid")
                connection.execute("DELETE FROM metadata WHERE key='capability'")
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('terminal', ?)",
                    (b"sealed",),
                )
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('export', ?)",
                    (export_record,),
                )
                for name, data in files.items():
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)",
                        (f"export:{name}", data),
                    )
            connection.commit()
            return event

    @staticmethod
    def _sealed_export(
        initial: dict[str, Any], events: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, bytes], bytes]:
        snapshot = {
            "current": {"pull_request": events[-1]["transition"]},
            "event_count": len(events),
            "head_hash": events[-1]["event_hash"],
            "initial": initial,
            "schema_version": 1,
            "sealed": True,
            "terminal_state": "pr_submitted",
        }
        snapshot_bytes = canonical(snapshot) + b"\n"
        events_bytes = b"".join(canonical(event) + b"\n" for event in events)
        manifest = {
            "files": {
                "events.jsonl": digest(events_bytes),
                "snapshot.json": digest(snapshot_bytes),
            },
            "schema_version": 1,
        }
        files = {
            "events.jsonl": events_bytes,
            "manifest.json": canonical(manifest) + b"\n",
            "snapshot.json": snapshot_bytes,
        }
        return snapshot, files, canonical(manifest)

    def export_sealed(self, export_path: Path) -> dict[str, Any]:
        with self._connect() as connection:
            terminal = connection.execute(
                "SELECT value FROM metadata WHERE key='terminal'"
            ).fetchone()
            if terminal is None or terminal["value"] != b"sealed":
                raise ForgeError("forge has not finalized")
            export_record = connection.execute(
                "SELECT value FROM metadata WHERE key='export'"
            ).fetchone()
            if export_record is None:
                raise ForgeError("sealed export record is missing")
            manifest = strict_json(export_record["value"], newline=False)
            rows = connection.execute(
                "SELECT key, value FROM metadata WHERE key LIKE 'export:%'"
            )
            files = {row["key"].removeprefix("export:"): row["value"] for row in rows}
        if set(files) != {"events.jsonl", "manifest.json", "snapshot.json"}:
            raise ForgeError("sealed export files are incomplete")
        if strict_json(files["manifest.json"], newline=True) != manifest:
            raise ForgeError("sealed export manifest changed")
        if manifest != {
            "files": {
                "events.jsonl": digest(files["events.jsonl"]),
                "snapshot.json": digest(files["snapshot.json"]),
            },
            "schema_version": 1,
        }:
            raise ForgeError("sealed export bytes changed")
        strict_json(files["snapshot.json"], newline=True)
        for line in files["events.jsonl"].splitlines(keepends=True):
            strict_json(line, newline=True)
        self._publish_export(export_path, files)
        return manifest

    @staticmethod
    def _publish_export(export_path: Path, files: dict[str, bytes]) -> None:
        if export_path.exists():
            if export_path.is_symlink() or not export_path.is_dir():
                raise ForgeError("existing export is unsafe")
            actual = {
                path.name: path.read_bytes()
                for path in export_path.iterdir()
                if path.is_file() and not path.is_symlink()
            }
            if actual != files or len(actual) != len(list(export_path.iterdir())):
                raise ForgeError("existing export differs from sealed state")
            return
        export_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".export-", dir=export_path.parent))
        try:
            for name, data in files.items():
                target = temporary / name
                descriptor = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            directory_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(temporary, export_path)
            parent_fd = os.open(export_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            if export_path.exists():
                shutil.rmtree(export_path)
                parent_fd = os.open(export_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


class Handler(http.server.BaseHTTPRequestHandler):
    store: ForgeStore

    def _reply(self, status: int, value: dict[str, Any]) -> None:
        body = canonical(value) + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject_method(self) -> None:
        self._reply(405, {"error": "method not allowed"})

    def do_GET(self) -> None:
        if self.path == "/health" and not self.headers.get("Content-Length"):
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/transitions":
            self._reply(404, {"error": "not found"})
            return
        try:
            if self.headers.get("Transfer-Encoding") is not None:
                raise ForgeError("transfer encoding is forbidden")
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ForgeError("exactly one Content-Length is required")
            length = int(lengths[0])
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ForgeError("invalid request size")
            content_types = self.headers.get_all("Content-Type") or []
            if content_types != ["application/json"]:
                raise ForgeError("Content-Type must be application/json")
            capability = self.headers.get("X-Forge-Capability", "")
            value = strict_json(self.rfile.read(length), newline=False)
            event = self.store.apply_transition(value, capability)
        except ForgeError as exc:
            status = 409 if "terminal and sealed" in str(exc) else 400
            self._reply(status, {"error": str(exc)})
            return
        self._reply(201, event)

    do_DELETE = _reject_method
    do_HEAD = _reject_method
    do_OPTIONS = _reject_method
    do_PATCH = _reject_method
    do_PUT = _reject_method

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", type=Path, default=Path("/forge/state/forge.sqlite3")
    )
    parser.add_argument(
        "--initial-state",
        type=Path,
        default=Path("/opt/forge/initial_state.json"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    export = subparsers.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    store = ForgeStore(args.database, args.initial_state)
    if args.command == "export":
        print(canonical(store.export_sealed(args.output)).decode())
        return 0
    Handler.store = store
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
