"""Thin Harbor adapters for sealed settings, model policy and observed versions."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from harbor.agents.installed.base import (
    BaseInstalledAgent,
    CliFlag,
    NonZeroAgentExitCodeError,
    with_prompt_template,
)
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.codex import Codex
from harbor.agents.installed.opencode import OpenCode
from harbor.agents.installed.pi import Pi

from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.harness_config import ResolvedHarness
from tetrabench.harnesses import get_harness, parse_native


class HarnessVersionError(RuntimeError):
    """Installed executable identity did not prove the requested pin."""


class ControlledAgent(BaseInstalledAgent):
    """Shared lifecycle checks; native agents continue to own execution."""

    def __init__(
        self, logs_dir: Path, *, harness: dict[str, Any], version: str, **kwargs: Any
    ):
        self.harness = ResolvedHarness.model_validate(harness)
        if (
            version != self.harness.version
            or kwargs.get("model_name") != self.harness.model
        ):
            raise ValueError("Harbor adapter and sealed harness identity disagree")
        # Only Harbor's lifecycle arguments may accompany the sealed specification.
        allowed = {
            "logs_dir",
            "model_name",
            "extra_env",
            "logger",
            "mcp_servers",
            "skills_dir",
            "environment_logs_dir",
            "session_id",
            "context_id",
            "load_trajectory",
        }
        if set(kwargs) - allowed:
            raise ValueError("unsupported controlled adapter argument")
        self._observed_version: str | None = None
        self._effective_configs: dict[str, str] = {}
        native = parse_native(self.harness.native_config)
        options = dict(self.harness.options)
        if self.harness.name == "opencode":
            if self.harness.ancillary_models == "primary":
                native["small_model"] = self.harness.model
            kwargs["opencode_config"] = native
        elif self.harness.name in {"codex", "claude-code"}:
            kwargs["config"] = native
            fallback = options.get("fallback_model")
            if self.harness.name == "claude-code" and isinstance(fallback, str):
                options["fallback_model"] = fallback.split("/", 1)[-1]
        self._pi_native = native if self.harness.name == "pi" else {}
        super().__init__(logs_dir=logs_dir, version=version, **kwargs, **options)

    def _env_sources(self) -> tuple[Mapping[str, str], ...]:
        # Host fallback would silently select interactive auth, routes, or flags.
        return (
            getattr(self, "_resolved_env_vars", {}),
            getattr(self, "_extra_env", {}),
        )

    def build_cli_flags(self) -> str:
        """Render descriptor tokens before quoting values, never a shell fragment."""
        tokens = []
        for flag in self.CLI_FLAGS:
            value = self._resolved_flags.get(flag.kwarg)
            if value is None:
                continue
            if flag.format is not None:
                # Only the pinned descriptor supplies syntax. Splitting after
                # substitution would reinterpret spaces or shell syntax in values.
                formatted = [
                    token.format(value=value) for token in shlex.split(flag.format)
                ]
                if (
                    self.harness.name == "codex"
                    and len(formatted) == 2
                    and formatted[0] == "-c"
                ):
                    tokens.append("--config=" + formatted[1])
                else:
                    tokens.extend(formatted)
            elif flag.type == "bool":
                if value:
                    tokens.append(flag.cli)
            else:
                if self.harness.name == "pi":
                    # Pi 0.74's manual parser treats --thinking=high as an
                    # unknown extension flag. Its validated enum cannot lead '-'.
                    if str(value).startswith("-"):
                        raise ValueError("Pi option values cannot start with '-'")
                    tokens.extend((flag.cli, str(value)))
                else:
                    # Quoting a separate '--version' does not stop CLI option
                    # parsing. Bind the value first, then quote the whole token.
                    tokens.append(f"{flag.cli}={value}")
        return shlex.join(tokens)

    def _safe_text(self, text: str) -> str:
        try:
            value = json.loads(text)
            is_json = True
        except ValueError:
            value = tomllib.loads(text)
            is_json = False
        references = {
            value: f"${{{key}}}" for key, value in self._extra_env.items() if value
        }

        def scrub(item):
            if isinstance(item, str):
                return references.get(item, item)
            if isinstance(item, list):
                return [scrub(child) for child in item]
            if isinstance(item, dict):
                return {key: scrub(child) for key, child in item.items()}
            return item

        if is_json:
            return json.dumps(scrub(value), allow_nan=False)
        import toml

        return toml.dumps(scrub(value))

    def _write_provenance(self, status: str) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        package = get_harness(self.harness.name).package
        document = {
            "schema_version": 1,
            "harness": self.harness.name,
            "package": package,
            "requested_version": self.harness.version,
            "observed_version": self._observed_version,
            "version_status": status,
            "requested_model": self.harness.model,
            "ancillary_models": self.harness.ancillary_models,
            "credential_variables": sorted(self.harness.env),
            "credential_references": sorted(set(self.harness.env.values())),
            "options": self.harness.options,
            "effective_native_configs": self._effective_configs,
            "limitations": [
                "Known native routing only; no universal all-call enforcement",
                "Costs are not a reconciled provider invoice",
            ],
        }
        path = self.logs_dir / "tetrabench-harness.json"
        path.write_bytes(dumps_canonical_json(document))
        path.chmod(0o600)

    async def setup(self, environment: Any) -> None:
        self._write_provenance("unverified")
        await super().setup(environment)
        command = self.get_version_command()
        if command is None:
            raise HarnessVersionError("harness has no executable version probe")
        try:
            result = await environment.exec(command=command)
            parsed = (
                self.parse_version(result.stdout or "")
                if result.return_code == 0
                else ""
            )
        except Exception:
            self._write_provenance("unverified")
            raise HarnessVersionError(
                "harness executable version probe failed"
            ) from None
        # Never retain arbitrary executable output, even on a failed probe.
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", parsed):
            self._observed_version = parsed
        status = (
            "matched" if self._observed_version == self.harness.version else "mismatch"
        )
        self._write_provenance(status)
        if status != "matched":
            raise HarnessVersionError(
                "installed harness version does not match requested pin"
            )

    async def _upload_config_text(
        self, environment: Any, *, content: str, remote_path: str, filename: str
    ) -> None:
        if filename in {"config.toml", "settings.json", "models.json"}:
            self._effective_configs[filename] = self._safe_text(content)
            self._write_provenance(
                "matched" if self._observed_version else "unverified"
            )
        await super()._upload_config_text(
            environment, content=content, remote_path=remote_path, filename=filename
        )

    async def _exec(
        self,
        environment: Any,
        command: str,
        user: str | int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        env = dict(env or {})
        if self.harness.name == "claude-code":
            # Native run() reads these host variables directly instead of _get_env.
            for key in (
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
            ):
                env.pop(key, None)
            if self.harness.ancillary_models == "primary":
                model = self.harness.model.split("/", 1)[-1]
                env.update(
                    {
                        f"ANTHROPIC_DEFAULT_{tier}_MODEL": model
                        for tier in ("SONNET", "OPUS", "HAIKU")
                    }
                )
                env["CLAUDE_CODE_SUBAGENT_MODEL"] = model
        elif self.harness.name == "pi" and self._pi_native:
            # Harbor 0.22's sandbox-local path must match its model-file injection.
            env["PI_CODING_AGENT_DIR"] = "/tmp/harbor-pi-agent"  # nosec B108
        return await super()._exec(
            environment, command, user=user, env=env, cwd=cwd, timeout_sec=timeout_sec
        )


class ControlledOpenCode(ControlledAgent, OpenCode):
    CLI_FLAGS: ClassVar = [
        *OpenCode.CLI_FLAGS,
        CliFlag("title", cli="--title", type="str"),
    ]

    def _build_register_config_command(self) -> str | None:
        command = super()._build_register_config_command()
        if command:
            tokens = shlex.split(command)
            self._effective_configs["opencode.json"] = self._safe_text(
                tokens[tokens.index("echo") + 1]
            )
            self._write_provenance(
                "matched" if self._observed_version else "unverified"
            )
        return command

    @with_prompt_template
    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        # Harbor 0.22's --dangerously-skip-permissions is not accepted by
        # OpenCode 1.18.29. Keep its native setup/stream handling with --auto.
        self._instruction = instruction
        env = dict(self.model_connection.env)
        env.update(
            OPENCODE_FAKE_VCS="git",
            XDG_DATA_HOME="/logs/agent/opencode/xdg-data",
            XDG_STATE_HOME="/logs/agent/opencode/xdg-state",
        )
        for command in (
            self._build_register_skills_command(),
            self._build_register_config_command(),
        ):
            if command:
                await self.exec_as_agent(environment, command=command, env=env)
        prefix = shlex.join(
            ["opencode", f"--model={self.model_name}", "run", "--format=json"]
        )
        resume = "--continue " if self._resume else ""
        await self.exec_as_agent(
            environment,
            command=(
                "[ -f ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh; "
                f"{prefix} {resume}{self.build_cli_flags()} --thinking --auto -- "
                f"{shlex.quote(instruction)} 2>&1 </dev/null | "
                "stdbuf -oL tee /logs/agent/opencode.txt"
            ),
            env=env,
        )
        if messages := self._error_messages():
            raise NonZeroAgentExitCodeError(
                "OpenCode emitted error event(s): " + "; ".join(messages[:3])
            )


class ControlledCodex(ControlledAgent, Codex):
    pass


class ControlledClaudeCode(ControlledAgent, ClaudeCode):
    pass


class ControlledPi(ControlledAgent, Pi):
    def _build_custom_models_json(
        self, access: Any, model_id: str
    ) -> dict[str, Any] | None:
        models = super()._build_custom_models_json(access, model_id)
        if models is not None:
            # Pi 0.74 resolves process.env[config] || config, not $ interpolation.
            for provider in models["providers"].values():
                provider["apiKey"] = self._api_key_env_name(access)
        return models

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        if self._pi_native:
            await self.exec_as_agent(
                environment,
                command=(
                    "mkdir -p /tmp/harbor-pi-agent && chmod 700 /tmp/harbor-pi-agent"
                ),
            )
            for kind, value in self._pi_native.items():
                # Same pinned sandbox directory used by Harbor's Pi adapter.
                await self._upload_config_text(
                    environment,
                    content=json.dumps(value, allow_nan=False),
                    remote_path=f"/tmp/harbor-pi-agent/{kind}.json",  # nosec B108
                    filename=f"{kind}.json",
                )
        await super().run(instruction, environment, context)
