"""Bounded literal-only auth retention guard. Not a workload-secret detector.

Only credential literals known to this run (initial and refreshed native state)
are covered. Encoded/encrypted transformations and arbitrary workload secrets are
outside this contract. Unknown refresh state quarantines all native job output.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from tetrabench.artifact_policy import ArtifactLimits
from tetrabench.auth_sessions import AuthError, private_directory, private_json
from tetrabench.run_reference import write_private_record

MARKER = "native-auth-retention.json"
Reason = Literal["known-auth-literal", "refresh-state-unknown", "scan-incomplete"]
_PROOF_NAMES = {"config.json", "lock.json", "manifest.json", "result.json"}


class AuthRetentionError(AuthError):
    pass


def outputs_blocked(attempt_root: Path) -> bool:
    """Presence, including an invalid marker or symlink, is fail-closed."""
    marker = attempt_root / MARKER
    return marker.exists() or marker.is_symlink()


@dataclass(frozen=True)
class ScanResult:
    contains_literal: bool
    unsafe_to_scrub: bool


class KnownAuthRetention:
    def __init__(self, *, limits: ArtifactLimits | None = None):
        self.values: set[str] = set()
        self.complete = True
        self.limits = limits or ArtifactLimits()
        self.quarantine_path: Path | None = None

    def __repr__(self) -> str:
        return "<KnownAuthRetention private>"

    def add(self, value: str) -> None:
        if not value:
            return
        if len(self.values) >= 256 and value not in self.values:
            self.complete = False
            return
        if len(value.encode()) > 128 * 1024:
            self.complete = False
            return
        self.values.add(value)
        if sum(len(item.encode()) for item in self.values) > 4 * 1024 * 1024:
            self.complete = False

    def native(self, harness: str, data: bytes) -> None:
        try:
            document = private_json(data)
            if harness == "codex":
                token = document.get("tokens", {})
                fields = ("access_token", "refresh_token", "id_token")
            else:
                token = document.get(
                    "openai" if harness == "opencode" else "openai-codex", {}
                )
                fields = ("access", "refresh")
            if not isinstance(token, dict):
                raise ValueError
            for field in fields:
                value = token.get(field)
                if isinstance(value, str):
                    self.add(value)
        except (AuthError, ValueError, TypeError):
            self.complete = False

    def _inventory(self, root: Path, deadline: float) -> list[Path]:
        pending = [(root, 0)]
        files: list[Path] = []
        entries = total = 0
        while pending:
            path, depth = pending.pop()
            entries += 1
            if entries > 50_000 or depth > 64 or time.monotonic() > deadline:
                raise AuthRetentionError("auth retention scan exceeded its bounds")
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as children:
                    for child in children:
                        entries += 1
                        if entries > 50_000:
                            raise AuthRetentionError(
                                "auth retention scan exceeded its bounds"
                            )
                        pending.append((Path(child.path), depth + 1))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                files.append(path)
                if (
                    len(files) > self.limits.max_files
                    or info.st_size > self.limits.max_file_bytes
                    or total > self.limits.max_total_bytes
                ):
                    raise AuthRetentionError("auth retention scan exceeded its bounds")
            else:
                raise AuthRetentionError(
                    "auth retention output contains an unsafe file"
                )
        return files

    def scan(self, root: Path) -> ScanResult:
        deadline = time.monotonic() + 30
        files = self._inventory(root, deadline)
        needles = tuple(value.encode() for value in self.values)
        overlap = max((len(value) for value in needles), default=1) - 1
        found = unsafe = False
        for path in files:
            if any(
                needle in str(path.relative_to(root)).encode() for needle in needles
            ):
                return ScanResult(True, True)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise AuthRetentionError("auth retention file changed during scan")
                tail = b""
                matched = False
                text = True
                decoder = codecs.getincrementaldecoder("utf-8")()
                total = 0
                while chunk := stream.read(64 * 1024):
                    total += len(chunk)
                    if (
                        total > self.limits.max_file_bytes
                        or time.monotonic() > deadline
                    ):
                        raise AuthRetentionError(
                            "auth retention scan exceeded its bounds"
                        )
                    window = tail + chunk
                    matched |= any(needle in window for needle in needles)
                    tail = window[-overlap:] if overlap else b""
                    if b"\0" in chunk:
                        text = False
                    if text:
                        try:
                            decoder.decode(chunk)
                        except UnicodeDecodeError:
                            text = False
                if text:
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        text = False
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or total != before.st_size:
                    raise AuthRetentionError("auth retention file changed during scan")
            found |= matched
            opaque = path.suffix in {
                ".db",
                ".sqlite",
                ".sqlite3",
            } or path.name.endswith(("-wal", "-shm"))
            unsafe |= matched and (not text or opaque or path.name in _PROOF_NAMES)
        return ScanResult(found, unsafe)

    def _harbor_scrub(self, root: Path) -> None:
        """Pinned scrubber view, never injected into agent/child environments."""
        from harbor.trial.trial import Trial

        if version("harbor") != "0.22.0":
            raise AuthRetentionError("native auth scrubber version is unproven")
        view = SimpleNamespace(
            agent=SimpleNamespace(
                extra_env={
                    f"AUTH_SECRET_{index}": value
                    for index, value in enumerate(self.values)
                }
            ),
            task=SimpleNamespace(
                config=SimpleNamespace(verifier=SimpleNamespace(env={}))
            ),
            config=SimpleNamespace(verifier=SimpleNamespace(env={})),
            user_agent=None,
            paths=SimpleNamespace(trial_dir=root),
            logger=logging.Logger("private-auth-scrubber", level=logging.CRITICAL + 1),
        )
        # Harbor exports no standalone scrubber; this version-matched metadata
        # view supplies exactly the fields its implementation consumes.
        scrub: Any = Trial._scrub_jobs_dir
        scrub(view)

    def _block(
        self, root: Path, attempt_root: Path, private_parent: Path, reason: Reason
    ) -> None:
        marker = json.dumps(
            {"schema_version": 1, "state": "publication-blocked", "reason": reason},
            separators=(",", ":"),
        ).encode()
        # The publisher consults this before any failure artifact upload. Write
        # it first: a cross-device move or interrupted copy must not leak output.
        write_private_record(attempt_root / MARKER, marker)
        destination = private_directory(
            private_parent / ("quarantine-" + uuid.uuid4().hex), create=True
        )
        self.quarantine_path = destination / "native-output"
        try:
            shutil.move(str(root), self.quarantine_path)
            # A late native download cannot recreate the directory at this name.
            write_private_record(root, marker)
        except OSError:
            # Original bytes may remain locally, but are never in the published
            # inventory. No digest or rejected filename enters the diagnostic.
            pass
        raise AuthRetentionError("native auth output blocked from publication")

    def enforce(
        self,
        root: Path,
        *,
        attempt_root: Path,
        private_parent: Path,
        refresh_unknown: bool,
    ) -> None:
        if not root.exists() and not root.is_symlink():
            return
        if outputs_blocked(attempt_root):
            raise AuthRetentionError(
                "native auth output publication is already blocked"
            )
        reason: Reason | None = None
        try:
            if refresh_unknown:
                reason = "refresh-state-unknown"
            elif not self.complete:
                reason = "scan-incomplete"
            else:
                result = self.scan(root)
                if result.unsafe_to_scrub:
                    reason = "known-auth-literal"
                elif result.contains_literal:
                    self._harbor_scrub(root)
                    if self.scan(root).contains_literal:
                        reason = "known-auth-literal"
        except (OSError, ValueError, AuthRetentionError):
            reason = "scan-incomplete"
        if reason is not None:
            self._block(root, attempt_root, private_parent, reason)
