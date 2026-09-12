"""Harbor 0.22 Compose exec with values in the client environment, not argv.

Harbor retains command construction, lifecycle, streams and timeout handling.
Only its exec option prefix is rewritten; shell commands are opaque. The
ContextVar joins two protected hooks without changing process-global os.environ
or leaking per-exec values into concurrent trials or later Compose operations.
"""

from __future__ import annotations

from contextvars import ContextVar
from importlib.metadata import version
from typing import Any

from harbor.environments.base import ExecResult, OutputCallback
from harbor.environments.docker.docker import DockerEnvironment

DOCKER_ENVIRONMENT_IMPORT_PATH = (
    "tetrabench.docker_environment:TetrabenchDockerEnvironment"
)

_client_env: ContextVar[tuple[DockerEnvironment, dict[str, str]] | None] = ContextVar(
    "tetrabench_docker_exec_environment", default=None
)


class TetrabenchDockerEnvironment(DockerEnvironment):
    """Version-matched transport adapter; not a separate Docker run engine."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if version("harbor") != "0.22.0":
            raise RuntimeError("Harbor 0.22.0 is required for Docker exec forwarding")
        super().__init__(*args, **kwargs)

    def _compose_env_vars(self, include_os_env: bool = True) -> dict[str, str]:
        env = super()._compose_env_vars(include_os_env=include_os_env)
        scope = _client_env.get()
        if scope is not None and scope[0] is self:
            env.update(scope[1])
        return env

    async def _run_docker_compose_command(
        self,
        command: list[str],
        check: bool = True,
        timeout_sec: int | None = None,
        stdin_data: bytes | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        command = list(command)
        forwarded: dict[str, str] = {}
        if command and command[0] == "exec":
            index = 1
            # Harbor 0.22 _compose_exec emits only these paired options before
            # the service. Never inspect command/script arguments after it.
            while index < len(command) and command[index] in {"-w", "-u", "-e"}:
                if index + 1 >= len(command):
                    raise ValueError("incomplete Harbor Docker exec option")
                if command[index] == "-e":
                    key, separator, value = command[index + 1].partition("=")
                    if not key or "\x00" in key:
                        raise ValueError("invalid Harbor Docker exec environment name")
                    if separator:
                        forwarded[key] = value
                        command[index + 1] = key
                index += 2
        token = _client_env.set((self, forwarded))
        try:
            return await super()._run_docker_compose_command(
                command,
                check=check,
                timeout_sec=timeout_sec,
                stdin_data=stdin_data,
                on_output=on_output,
            )
        finally:
            _client_env.reset(token)
