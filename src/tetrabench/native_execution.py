"""Owned native executable paths, independent of auth's XDG directories."""

from __future__ import annotations

import os
from collections.abc import Mapping

NATIVE_SHELL_PREFIX = (
    'export NVM_DIR="$HOME/.nvm"; '
    'export PATH="$HOME/.local/bin:$PATH"; '
    'if [ -s "$NVM_DIR/nvm.sh" ]; then . "$NVM_DIR/nvm.sh"; fi; '
)


def native_shell_command(command: str) -> str:
    """Expand paths inside the target user's shell, never on the controller."""
    return (
        command
        if command.startswith(NATIVE_SHELL_PREFIX)
        else NATIVE_SHELL_PREFIX + command
    )


def native_process_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Normalize a known local HOME for argv-only native auth processes."""
    result = dict(environment)
    home = result.get("HOME")
    if home:
        if not home.startswith("/") or "\x00" in home:
            raise ValueError("native process HOME must be an absolute path")
        local_bin = home.rstrip("/") + "/.local/bin"
        result["NVM_DIR"] = home.rstrip("/") + "/.nvm"
        result["PATH"] = ":".join(
            dict.fromkeys([local_bin, *result.get("PATH", os.defpath).split(":")])
        )
    return result
