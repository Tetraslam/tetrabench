"""Harbor 0.22 DinD exec forwarding without credential values in commands.

The pinned strategy has no exec-env builder hook. Its protected Compose/VM
primitives do allow a per-call adapter: native DinDComposeOps builds the service
command without env values, Docker receives name-only flags, and Modal's native
SDK Secret environment carries the values. Lifecycle and non-exec operations
remain inherited. The adapter writes no startup env files and adds no
process-global state.
"""

from __future__ import annotations

import re

from harbor.environments.base import ExecResult
from harbor.environments.dind_compose import DinDComposeOps
from harbor.environments.modal import _ModalDinD


class _EnvNameComposeExec(DinDComposeOps):
    def __init__(self, strategy: _ModalDinD, env: dict[str, str] | None) -> None:
        self._strategy = strategy
        self._exec_env = dict(env or {})
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in self._exec_env):
            raise ValueError("invalid Modal Compose exec environment name")

    async def _compose_exec(
        self, subcommand: list[str], timeout_sec: int | None = None
    ) -> ExecResult:
        if subcommand[0] != "exec":
            raise RuntimeError("Modal Compose env adapter only supports exec")
        strategy = self._strategy
        infra = strategy._infra_env_vars()
        host_env = strategy._compose_env_vars()
        host_env.update(self._exec_env)
        flags = [part for name in self._exec_env for part in ("-e", name)]
        # _sdk_exec merges the main environment's scoped overlays again. Keep
        # explicit sidecar values authoritative when they use the same names.
        with strategy._env.scoped_exec_env(self._exec_env):
            host_env = strategy._env._merge_env(host_env) or {}
            # Validate the SDK's final merge, including outer main scopes.
            if any(host_env.get(k) != v for k, v in infra.items()):
                raise ValueError(
                    "Modal Compose exec environment conflicts with Harbor infra"
                )
            return await strategy._vm_exec(
                strategy._compose_cmd(["exec", *flags, *subcommand[1:]]),
                env=host_env,
                timeout_sec=timeout_sec,
            )


class EnvNameModalDinD(_ModalDinD):
    """Replace only exec transport; Harbor owns the complete DinD lifecycle."""

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
        *,
        service: str | None = None,
    ) -> ExecResult:
        return await _EnvNameComposeExec(self, env).exec(
            command,
            cwd=cwd,
            timeout_sec=timeout_sec,
            user=user,
            service=service,
        )
