"""Shared controller and receiver artifact limits."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_ARTIFACT_FILES = 10_000
DEFAULT_MAX_ARTIFACT_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_TOTAL_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArtifactLimits:
    max_files: int = DEFAULT_MAX_ARTIFACT_FILES
    max_file_bytes: int = DEFAULT_MAX_ARTIFACT_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_ARTIFACT_TOTAL_BYTES

    def __post_init__(self) -> None:
        if min(self.max_files, self.max_file_bytes, self.max_total_bytes) <= 0:
            raise ValueError("artifact limits must be positive")
