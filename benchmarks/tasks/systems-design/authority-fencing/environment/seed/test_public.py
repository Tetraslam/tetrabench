from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
PROGRAM = ROOT / "authority.py"


class PublicCases(unittest.TestCase):
    def run_cli(self, db: Path, *arguments: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, str(PROGRAM), "--db", str(db), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.stderr, "")
        value = json.loads(result.stdout)
        self.assertEqual(
            result.stdout,
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        )
        self.assertEqual(set(value), {"idempotent", "job", "ok"})
        self.assertIs(value["ok"], True)
        return value

    def reject_cli(self, db: Path, *arguments: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, str(PROGRAM), "--db", str(db), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        value = json.loads(result.stderr)
        self.assertEqual(
            result.stderr,
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        )
        self.assertEqual(set(value), {"error", "ok"})
        self.assertIs(value["ok"], False)
        return value

    def test_happy_path_and_fresh_process_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.sqlite3"
            self.run_cli(db, "create", "--job", "public-job")
            claimed = self.run_cli(
                db,
                "claim",
                "--job",
                "public-job",
                "--worker",
                "public-worker",
                "--now",
                "10",
                "--ttl",
                "5",
            )
            self.assertEqual(claimed["job"]["fence_token"], 1)
            self.assertIs(claimed["idempotent"], False)
            renewed = self.run_cli(
                db,
                "renew",
                "--job",
                "public-job",
                "--worker",
                "public-worker",
                "--token",
                "1",
                "--now",
                "11",
                "--ttl",
                "10",
            )
            self.assertEqual(renewed["job"]["deadline"], 21)
            completed = self.run_cli(
                db,
                "complete",
                "--job",
                "public-job",
                "--worker",
                "public-worker",
                "--token",
                "1",
                "--now",
                "12",
            )
            self.assertEqual(completed["job"]["status"], "completed")
            inspected = self.run_cli(db, "inspect", "--job", "public-job")
            self.assertEqual(inspected["job"], completed["job"])
            self.assertIs(inspected["idempotent"], False)
            replayed = self.run_cli(
                db,
                "complete",
                "--job",
                "public-job",
                "--worker",
                "public-worker",
                "--token",
                "1",
                "--now",
                "21",
            )
            self.assertEqual(replayed["job"], completed["job"])
            self.assertIs(replayed["idempotent"], True)
            before = db.read_bytes()
            self.reject_cli(
                db,
                "fail",
                "--job",
                "public-job",
                "--worker",
                "public-worker",
                "--token",
                "1",
                "--now",
                "21",
            )
            self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
