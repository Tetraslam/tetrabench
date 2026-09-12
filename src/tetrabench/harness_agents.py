"""Thin Harbor adapters for sealed settings, model policy and observed versions."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, ClassVar, Protocol

from harbor.agents.installed.base import (
    BaseInstalledAgent,
    CliFlag,
    EnvVar,
    NonZeroAgentExitCodeError,
    with_prompt_template,
)
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.codex import Codex
from harbor.agents.installed.opencode import OpenCode
from harbor.agents.installed.pi import Pi

from tetrabench.canonical_json import dumps_canonical_json, sha256_hex
from tetrabench.harness_config import ResolvedHarness
from tetrabench.harnesses import (
    STABLE_VERSIONS,
    get_harness,
    native_configuration_layers,
    parse_native,
    validate_explicit_auth_configuration,
)
from tetrabench.native_execution import NATIVE_SHELL_PREFIX, native_shell_command
from tetrabench.resources import AGENT_RESOURCE_ROOT


class HarnessVersionError(RuntimeError):
    """Installed executable identity did not prove the requested pin."""


class NativeCredentialCapsule(Protocol):
    """Private per-trial capability. Capture never implies consumer termination."""

    async def prepare(self, agent: ControlledAgent, environment: Any) -> None: ...
    def exec_environment(
        self, agent: ControlledAgent, env: dict[str, str]
    ) -> dict[str, str]: ...
    async def capture(self, agent: ControlledAgent, environment: Any) -> None: ...


_credential_factory: ContextVar[
    Callable[[ControlledAgent], NativeCredentialCapsule] | None
] = ContextVar("tetrabench_native_credential_factory", default=None)


@contextmanager
def native_credential_hooks(
    factory: Callable[[ControlledAgent], NativeCredentialCapsule],
) -> Iterator[None]:
    """Install a factory in this execution context, inherited by async trials."""
    token = _credential_factory.set(factory)
    try:
        yield
    finally:
        _credential_factory.reset(token)


def native_credential_hooks_available() -> bool:
    return _credential_factory.get() is not None


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
        self._uploaded_native_files: dict[str, str] = {}
        self._capability_verification: dict[str, Any] | None = None
        self._guard_directory: str | None = None
        self._native_producer_nonce: str | None = None
        self._native_producer_status_path: str | None = None
        self._pi_config_root: str | None = None
        self._resources_uploaded = False
        self._credential_capsule: NativeCredentialCapsule | None = None
        self._runtime_auth_hook: Any = None
        self._credential_environment: dict[str, str] = {}
        self._credential_captured = False
        self._credential_capture_failed = False
        self.native_execution_outcome = "not_started"
        self._installing_native = False
        native = parse_native(self.harness.native_config)
        options = dict(self.harness.options)
        if self.harness.name == "claude-code":
            agents = options.get("agents")
            if isinstance(agents, str) and agents.startswith(AGENT_RESOURCE_ROOT + "/"):
                alias = agents.removeprefix(AGENT_RESOURCE_ROOT + "/")
                options["agents"] = next(
                    item.text
                    for item in self.harness.resources
                    if item.destination == alias
                )
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
        if (
            self.harness.name == "codex"
            and version == STABLE_VERSIONS["codex"]
            and "reasoning_effort" not in options
        ):
            # Harbor defaults to high. At the current pin the native model
            # descriptor/default config, including compaction, owns the choice.
            self._resolved_flags.pop("reasoning_effort", None)

    def _env_sources(self) -> tuple[Mapping[str, str], ...]:
        # Host fallback would silently select interactive auth, routes, or flags.
        return (
            self._credential_environment,
            getattr(self, "_resolved_env_vars", {}),
            getattr(self, "_extra_env", {}),
        )

    async def _prepare_credentials(self, environment: Any) -> None:
        validate_explicit_auth_configuration(self.harness)
        if self.harness.auth is None:
            return
        if self._credential_capsule is None:
            factory = _credential_factory.get()
            if factory is None:
                from tetrabench.runtime_auth import current_runtime_auth

                if self._runtime_auth_hook is None:
                    self._runtime_auth_hook = current_runtime_auth(self.harness)
                    if self._runtime_auth_hook is None:
                        raise ValueError("explicit auth produced no runtime hook")
                    self._runtime_auth_hook.configure_agent(self)
                if self._runtime_auth_hook is None:
                    raise RuntimeError("runtime auth hook lost during configuration")
                await self._runtime_auth_hook.bind(environment)
                self._credential_environment = (
                    self._runtime_auth_hook.execution_environment({})
                )
                return
            self._credential_capsule = factory(self)
        await self._credential_capsule.prepare(self, environment)
        self._credential_environment = self._credential_capsule.exec_environment(
            self, {}
        )

    async def _capture_credentials(self, environment: Any) -> None:
        if self._credential_capsule is None or self._credential_captured:
            return
        try:
            await self._credential_capsule.capture(self, environment)
        except BaseException:
            self._credential_capture_failed = True
            raise
        self._credential_captured = True

    @asynccontextmanager
    async def _native_credentials(self, environment: Any):
        self._credential_captured = False
        self._credential_capture_failed = False
        self.native_execution_outcome = "not_started"
        await self._prepare_credentials(environment)
        if self._runtime_auth_hook is not None:
            self._native_producer_nonce = uuid.uuid4().hex
            self._native_producer_status_path = (
                await self._runtime_auth_hook.prepare_step(
                    environment, producer_nonce=self._native_producer_nonce
                )
            )
            self._credential_environment = (
                self._runtime_auth_hook.execution_environment({})
            )
        try:
            yield
        finally:
            if self._credential_capture_failed:
                raise ValueError(
                    "native credential capture failed; authority remains blocked"
                )
            await self._capture_credentials(environment)
            self._write_provenance(
                "matched" if self._observed_version else "unverified"
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
                    if self.harness.version == STABLE_VERSIONS["codex"]:
                        key = formatted[1].split("=", 1)[0]
                        formatted[1] = key + "=" + json.dumps(value)
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
                    if flag.kwarg == "thinking" and str(value).startswith("-"):
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
            value: f"${{{key}}}"
            for key, value in (self._extra_env | self._credential_environment).items()
            if value
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
            "native_config_evidence": "injected files; native resolution unobserved",
            "discovery": self.harness.discovery or "native",
            "resources": [
                {"destination": item.destination, "sha256": item.sha256}
                for item in self.harness.resources
            ],
            "context_management": self._context_management_evidence(),
            "auth_mode": self.harness.auth.mode if self.harness.auth else "legacy_env",
            "auth_mode_source": "configured_selection",
            "auth_verification": self._auth_evidence(),
            "capability_snapshot_sha256": self.harness.capability_snapshot.sha256
            if self.harness.capability_snapshot
            else None,
            "capability_verification": self._capability_verification,
            "capability_startup_route_verified": bool(
                self._capability_verification
                and "endpoint"
                in self._capability_verification.get("verified_fields", [])
            ),
            "limitations": [
                "Known native routing only; no universal all-call enforcement",
                "Costs are not a reconciled provider invoice",
            ],
        }
        path = self.logs_dir / "tetrabench-harness.json"
        path.write_bytes(dumps_canonical_json(document))
        path.chmod(0o600)

    def _auth_evidence(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "configured_mode": self.harness.auth.mode
            if self.harness.auth
            else "legacy_env",
            "observed_mode": None,
            "source": "unobserved",
            "account_verified": False,
        }
        getter = getattr(self._runtime_auth_hook, "observed_auth_provenance", None)
        if getter is not None:
            evidence = getter()
            if not isinstance(evidence, dict):
                raise ValueError("invalid native auth evidence report")
            observed = evidence.get("observed")
            if not isinstance(observed, dict):
                return report
            mode = observed.get("mode")
            if observed.get("source") == "native_status" and mode in {
                "api_key",
                "chatgpt_oauth",
                "claude_setup_token",
                "none",
                "unknown",
            }:
                report.update(observed_mode=mode, source="native_status")
                report["native_observation"] = {
                    key: observed[key]
                    for key in (
                        "harness",
                        "mode",
                        "source",
                        "expires_at_ms",
                        "expiry_source",
                        "validity",
                    )
                    if key in observed
                }
                report["observations"] = evidence.get("observations", 0)
        return report

    def _context_management_evidence(self) -> dict[str, Any]:
        native = native_configuration_layers(self.harness).effective_config
        controls: dict[str, Any] = {}
        if self.harness.name == "opencode":
            controls = {key: native[key] for key in ("compaction",) if key in native}
        elif self.harness.name == "codex":
            controls = {
                key: value
                for key, value in native.items()
                if "compact" in key or key == "model_context_window"
            }
        elif self.harness.name == "claude-code":
            controls = {
                key: value
                for key, value in self.harness.options.items()
                if key
                in {
                    "autocompact",
                    "disable_auto_compact",
                    "max_output_tokens",
                    "disable_adaptive_thinking",
                    "max_thinking_tokens",
                }
            }
        elif self.harness.name == "pi":
            settings = native.get("settings", {})
            controls = {
                key: settings[key]
                for key in ("compaction", "branchSummary")
                if key in settings
            }
        return {
            "owner": "native_harness",
            "requested_controls": controls,
            "unspecified_controls": "native_version_defaults",
            "codex_native_default": "compaction_trigger_v2"
            if self.harness.name == "codex" and self.harness.version == "0.154.0"
            else None,
            "effective_mechanism": "unobserved",
            "observed_compaction_count": None,
            "opaque_compaction_observed": None,
            "evidence": "configuration_only",
        }

    async def setup(self, environment: Any) -> None:
        await self._prepare_credentials(environment)
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

    def get_version_command(self) -> str | None:
        command = super().get_version_command()
        return native_shell_command(command) if command is not None else None

    async def install(self, environment: Any) -> None:
        self._installing_native = True
        try:
            await super().install(environment)
        finally:
            self._installing_native = False

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
        if filename in {"config.toml", "settings.json", "models.json"}:
            self._uploaded_native_files[remote_path] = sha256_hex(content.encode())

    async def _upload_resources(self, environment: Any) -> None:
        if self._resources_uploaded:
            return
        if self.harness.resources:
            await self.exec_as_agent(
                environment,
                command=f"mkdir -m 700 {shlex.quote(AGENT_RESOURCE_ROOT)}",
            )
        for resource in self.harness.resources:
            path = f"{AGENT_RESOURCE_ROOT}/{resource.destination}"
            await self.exec_as_agent(
                environment, command=f"mkdir -p {shlex.quote(str(Path(path).parent))}"
            )
            await self._upload_config_text(
                environment,
                content=resource.text,
                remote_path=path,
                filename=Path(resource.destination).name,
            )
            await self.exec_as_agent(
                environment, command=f"chmod {resource.mode:o} {shlex.quote(path)}"
            )
        self._resources_uploaded = True

    async def _exec(
        self,
        environment: Any,
        command: str,
        user: str | int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        if (
            self._installing_native
            and self.harness.name == "pi"
            and self.harness.version == "0.85.1"
        ):
            from harbor.agents.installed.node_install import nvm_node_install_snippet

            # Harbor 0.22 has no installer node_major argument. Change only its
            # exact shared snippet, during installation, for Pi's >=24 engine.
            command = command.replace(
                nvm_node_install_snippet(), nvm_node_install_snippet(node_major=24)
            )
        if (
            self._installing_native
            and "raw.githubusercontent.com/nvm-sh/nvm/" in command
        ):
            # nvm's installer refuses an explicitly selected directory unless it
            # already exists. Only installation creates it, not read-only probes.
            command = 'mkdir -p -m 700 "$HOME/.nvm" || exit 1; ' + command
        if (
            self._credential_capsule is not None
            and self.harness.name == "codex"
            and command == 'rm -rf /tmp/codex-secrets "$CODEX_HOME"'
        ):
            await self._capture_credentials(environment)
        env = dict(env or {})
        if self.harness.name == "claude-code":
            # Native run() reads these host variables directly instead of _get_env.
            for key in (
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
            ):
                env.pop(key, None)
            env.update(self._resolved_env_vars)
            if self.harness.ancillary_models == "native":
                for key in (
                    "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                    "CLAUDE_CODE_SUBAGENT_MODEL",
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
        elif self.harness.name == "pi" and (
            self._pi_native or self.harness.discovery == "isolated"
        ):
            # Harbor 0.22's sandbox-local path must match its model-file injection.
            env["PI_CODING_AGENT_DIR"] = "/tmp/harbor-pi-agent"  # nosec B108
        elif (
            self.harness.name == "codex"
            and self.harness.version == STABLE_VERSIONS["codex"]
            and command.startswith(
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; codex exec "
            )
        ):
            old = (
                "--skip-git-repo-check "
                f"--model {self.harness.model.split('/')[-1]} --json "
            )
            if old not in command:
                raise ValueError("pinned Harbor Codex model argv shape changed")
            command = command.replace(
                old,
                "--skip-git-repo-check "
                + shlex.quote("--model=" + self.harness.model.split("/", 1)[1])
                + " --json ",
                1,
            )
        if self._credential_capsule is not None:
            env = self._credential_capsule.exec_environment(self, env)
        native = self._is_native_model_command(command)
        if native:
            self.native_execution_outcome = "unknown"
        try:
            if self._runtime_auth_hook is not None:
                env = self._runtime_auth_hook.execution_environment(env)
            if native and self.harness.capability_snapshot is not None:
                snapshot = self.harness.capability_snapshot.snapshot()
                endpoints = {
                    endpoint.rstrip("/") for endpoint in snapshot.identity.endpoints
                }
                for key, value in env.items():
                    if (
                        endpoints
                        and key.endswith("BASE_URL")
                        and value.rstrip("/") not in endpoints
                    ):
                        raise ValueError(
                            "native endpoint drift from capability snapshot"
                        )
                await self.validate_native_capability(environment, env=env, cwd=cwd)
            if native and self._runtime_auth_hook is not None:
                if (
                    self._native_producer_status_path is None
                    or self._native_producer_nonce is None
                ):
                    raise ValueError("native execution has no fresh producer witness")
                command = native_status_command(
                    command,
                    self.harness.name,
                    self._native_producer_status_path,
                    nonce=self._native_producer_nonce,
                )
                command = native_shell_command(command)
                result = await self._runtime_auth_hook.execute_model(
                    environment,
                    command,
                    user=user,
                    env=env,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                )
                if result.return_code != 0:
                    raise self._classify_exec_error(command, result)
            else:
                # Root dependency/chown operations must not source an agent user's
                # dotfiles. The native installer itself runs as the default user.
                if user is None or str(user) == str(
                    getattr(environment, "default_user", None)
                ):
                    command = native_shell_command(command)
                result = await super()._exec(
                    environment,
                    command,
                    user=user,
                    env=env,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                )
        except NonZeroAgentExitCodeError:
            if native:
                self.native_execution_outcome = "failed"
            raise
        if native:
            self.native_execution_outcome = "completed"
        return result

    async def validate_native_capability(
        self,
        environment: Any,
        *,
        env: dict[str, str],
        cwd: str | None = None,
        metadata_executor: Any = None,
        policy: str | None = None,
    ) -> None:
        """Use the native startup/dispatch verifier only for adopted snapshots."""
        if self.harness.capability_snapshot is None:
            return
        from tetrabench.capabilities import check_public_metadata
        from tetrabench.runtime_reasoning import validate_runtime_capability

        report = await validate_runtime_capability(
            self,
            environment,
            env=env,
            cwd=cwd,
            metadata_executor=metadata_executor,
            policy=policy,
        )
        check_public_metadata(report)
        self._capability_verification = report
        self._write_provenance("matched" if self._observed_version else "unverified")

    def _is_native_model_command(self, command: str) -> bool:
        command = command.removeprefix(NATIVE_SHELL_PREFIX)
        prefixes = {
            "codex": "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; codex exec ",
            "opencode": "[ -f ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh; opencode ",
            "claude-code": (
                'export PATH="$HOME/.local/bin:$PATH"; harbor_claude_code_instruction_'
            ),
        }
        if self.harness.name == "pi":
            return bool(
                re.match(
                    r"^\. ~/\.nvm/nvm\.sh; (?:set \+e; )?(?:PI_CODING_AGENT_DIR=\S+ )?"
                    r"pi --print --mode json ",
                    command,
                )
            )
        return command.startswith(prefixes[self.harness.name])


class ControlledOpenCode(ControlledAgent, OpenCode):
    # Rendered only into the per-trial Harbor sandbox; never opened on this host.
    _ISOLATED_CONFIG_HOME = "/tmp/tetrabench-opencode-config"  # nosec B108

    CLI_FLAGS: ClassVar = [
        *OpenCode.CLI_FLAGS,
        CliFlag("title", cli="--title", type="str"),
        CliFlag("agent", cli="--agent", type="str"),
        CliFlag("pure", cli="--pure", type="bool"),
    ]

    def _build_register_config_command(self) -> str | None:
        command = super()._build_register_config_command()
        if command:
            tokens = shlex.split(command)
            self._effective_configs["opencode.json"] = self._safe_text(
                tokens[tokens.index("echo") + 1]
            )
            description = native_configuration_layers(
                self.harness, base_config=json.loads(tokens[tokens.index("echo") + 1])
            )
            self._effective_configs["opencode.sealed-layers.json"] = self._safe_text(
                json.dumps(description.effective_config)
            )
            self._write_provenance(
                "matched" if self._observed_version else "unverified"
            )
            if self.harness.discovery == "isolated":
                command = command.replace(
                    "~/.config/opencode", self._ISOLATED_CONFIG_HOME + "/opencode"
                )
            if self.harness.auth is not None and self._credential_environment.get(
                "XDG_CONFIG_HOME"
            ):
                target = self._credential_environment["XDG_CONFIG_HOME"] + "/opencode"
                command = command.replace(
                    "~/.config/opencode", shlex.quote(target)
                ).replace(self._ISOLATED_CONFIG_HOME + "/opencode", shlex.quote(target))
        return command

    @with_prompt_template
    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        async with self._native_credentials(environment):
            await self._run_native(instruction, environment, context)

    async def _run_native(
        self, instruction: str, environment: Any, context: Any
    ) -> None:
        # Harbor 0.22's --dangerously-skip-permissions is not accepted by
        # OpenCode 1.18.29. Keep its native setup/stream handling with --auto.
        self._instruction = instruction
        await self._upload_resources(environment)
        env = dict(self.model_connection.env)
        env.update(
            OPENCODE_FAKE_VCS="git",
            XDG_DATA_HOME="/logs/agent/opencode/xdg-data",
            XDG_STATE_HOME="/logs/agent/opencode/xdg-state",
        )
        if self.harness.discovery == "isolated":
            env.update(
                OPENCODE_DISABLE_PROJECT_CONFIG="1",
                OPENCODE_DISABLE_CLAUDE_CODE="1",
                OPENCODE_DISABLE_EXTERNAL_SKILLS="1",
                XDG_CONFIG_HOME=self._ISOLATED_CONFIG_HOME,
            )
        directory = native_configuration_layers(self.harness).config_directory
        if directory is not None:
            env["OPENCODE_CONFIG_DIR"] = directory
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
    def _resolve_auth_json_path(self) -> Path | None:
        if self._runtime_auth_hook is not None:
            return self._runtime_auth_hook.codex_source_path
        return super()._resolve_auth_json_path()

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        async with self._native_credentials(environment):
            await self._upload_resources(environment)
            await super().run(instruction, environment, context)


class ControlledClaudeCode(ControlledAgent, ClaudeCode):
    CLI_FLAGS: ClassVar = [
        *(
            flag
            for flag in ClaudeCode.CLI_FLAGS
            if flag.kwarg not in {"reasoning_effort", "permission_mode"}
        ),
        CliFlag("reasoning_effort", cli="--effort", type="str"),
        CliFlag(
            "permission_mode",
            cli="--permission-mode",
            type="str",
            default="bypassPermissions",
        ),
        *(
            CliFlag(name, cli="--" + name.replace("_", "-"), type="str")
            for name in (
                "tools",
                "autocompact",
                "system_prompt",
                "system_prompt_file",
                "append_system_prompt_file",
                "agent",
                "agents",
                "mcp_config",
                "setting_sources",
            )
        ),
        *(
            CliFlag(name, cli="--" + name.replace("_", "-"), type="bool")
            for name in (
                "strict_mcp_config",
                "disable_slash_commands",
                "no_session_persistence",
                "fork_session",
            )
        ),
    ]
    ENV_VARS: ClassVar = [
        *ClaudeCode.ENV_VARS,
        EnvVar("max_output_tokens", env="CLAUDE_CODE_MAX_OUTPUT_TOKENS", type="int"),
        EnvVar(
            "disable_adaptive_thinking",
            env="CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
            type="bool",
            bool_true="1",
            bool_false="0",
        ),
        EnvVar(
            "disable_auto_compact",
            env="DISABLE_AUTO_COMPACT",
            type="bool",
            bool_true="1",
            bool_false="0",
        ),
    ]

    def build_cli_flags(self) -> str:
        flags = super().build_cli_flags()
        if self.harness.discovery == "isolated":
            flags += " --setting-sources='' --strict-mcp-config"
        return flags

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        async with self._native_credentials(environment):
            await self._upload_resources(environment)
            await super().run(instruction, environment, context)


class ControlledPi(ControlledAgent, Pi):
    SUPPORTS_LOAD_NATIVE_TRAJECTORY = True
    CLI_FLAGS: ClassVar = [
        CliFlag("thinking", cli="--thinking", type="str"),
        *(
            CliFlag(name, cli="--" + name.replace("_", "-"), type="str")
            for name in (
                "tools",
                "exclude_tools",
                "system_prompt",
                "append_system_prompt",
                "extension",
                "skill",
                "prompt_template",
                "name",
                "session_id",
            )
        ),
        *(
            CliFlag(name, cli="--" + name.replace("_", "-"), type="bool")
            for name in (
                "no_tools",
                "no_builtin_tools",
                "offline",
                "no_extensions",
                "no_skills",
                "no_prompt_templates",
                "no_themes",
                "no_context_files",
                "no_session",
            )
        ),
    ]

    def build_cli_flags(self) -> str:
        flags = super().build_cli_flags()
        if self.harness.discovery == "isolated":
            flags += (
                " --no-extensions --no-skills --no-prompt-templates"
                " --no-themes --no-context-files --no-approve"
            )
        return flags

    def _validate_native_load_trajectory(self, path: Path) -> None:
        if path.suffix != ".jsonl":
            raise ValueError("Pi load_trajectory requires native JSONL")
        try:
            header = json.loads(path.read_text().splitlines()[0])
            import uuid

            uuid.UUID(header["id"])
            if header.get("type") != "session" or header.get("version") != 3:
                raise ValueError()
        except (ValueError, KeyError, IndexError, OSError, TypeError):
            raise ValueError("invalid Pi native v3 session header") from None

    async def _upload_load_trajectory(self, environment: Any, source: Path) -> None:
        directory = "/logs/agent/pi/sessions"
        await self.exec_as_agent(environment, command=f"mkdir -p {directory}")
        await self._upload_agent_owned_file(
            environment, source, f"{directory}/{source.name}"
        )

    def _build_custom_models_json(
        self, access: Any, model_id: str
    ) -> dict[str, Any] | None:
        models = super()._build_custom_models_json(access, model_id)
        if models is not None:
            # Pi 0.74 resolves process.env[config] || config, not $ interpolation.
            for provider in models["providers"].values():
                selector = self._api_key_env_name(access)
                provider["apiKey"] = (
                    f"${{{selector}}}"
                    if self.harness.version == STABLE_VERSIONS["pi"]
                    else selector
                )
        return models

    @with_prompt_template
    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        async with self._native_credentials(environment):
            await self._run_native(instruction, environment, context)

    async def _run_native(
        self, instruction: str, environment: Any, context: Any
    ) -> None:
        await self._upload_resources(environment)
        if self._pi_native:
            self._pi_config_root = "/tmp/harbor-pi-agent"  # nosec B108
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
        if self._load:
            await self._seed_load_trajectory(environment)
        await self._run_pi_process(instruction, environment)

    async def _run_pi_process(self, instruction: str, environment: Any) -> None:
        """Harbor's native Pi invocation with a producer-status-preserving pipeline."""
        provider, model = self.harness.model.split("/", 1)
        access = self.model_connection
        provider = access.provider or provider
        env = dict(access.env)
        if provider == "anthropic" and (
            token := self._get_env("ANTHROPIC_OAUTH_TOKEN")
        ):
            env["ANTHROPIC_OAUTH_TOKEN"] = token
        models = self._build_custom_models_json(access, model)
        prefix = ""
        if models is not None:
            self._pi_config_root = "/tmp/harbor-pi-agent"  # nosec B108
            await self._write_custom_models_json(environment, models)
            prefix = "PI_CODING_AGENT_DIR=/tmp/harbor-pi-agent "
            provider = "harbor-endpoint"
        if skills := self._build_register_skills_command():
            await self.exec_as_agent(environment, command=skills)
        resume = "--continue " if self._resume or self._load else ""
        command = (
            ". ~/.nvm/nvm.sh; set +e; "
            f"{prefix}pi --print --mode json "
            "--session-dir /logs/agent/pi/sessions "
            f"{resume}{shlex.join(['--provider', provider, '--model', model])} "
            f"{self.build_cli_flags()} -- {shlex.quote(instruction)} "
            '2>&1 </dev/null | grep -v \'"type":"message_update"\' | '
            f"stdbuf -oL tee /logs/agent/{self._OUTPUT_FILENAME}; "
            + pi_pipeline_status()
        )
        await self.exec_as_agent(environment, command=command, env=env)


def _producer_status_write(path: str, index: int, nonce: str | None = None) -> str:
    if nonce is not None and re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("invalid native producer nonce")
    template = (
        '{"schema_version":1,"exit_code":%d'
        + (f',"nonce":"{nonce}"' if nonce is not None else "")
        + "}\\n"
    )
    return (
        "umask 077; set -C; "
        f"printf {shlex.quote(template)} "
        f'"${{tetrabench_pi_status[{index}]}}" > {shlex.quote(path)} || exit 125; '
        "set +C; "
    )


def native_status_command(
    command: str, name: str, path: str, *, nonce: str | None = None
) -> str:
    """Add a private, post-exit witness without inspecting native output text."""
    if name == "pi":
        suffix = pi_pipeline_status()
        if not command.endswith(suffix):
            raise ValueError("Pi status wrapper does not match the pinned command")
        return command[: -len(suffix)] + pi_pipeline_status(path, nonce=nonce)
    index = 1 if name == "claude-code" else 0
    return (
        "set +e; "
        + command
        + '; tetrabench_pi_status=("${PIPESTATUS[@]}"); '
        + _producer_status_write(path, index, nonce)
        + f'exit "${{tetrabench_pi_status[{index}]}}"'
    )


def pi_pipeline_status(
    status_path: str | None = None, *, nonce: str | None = None
) -> str:
    """Read Bash's status vector immediately, never a native stdout witness."""
    return (
        'tetrabench_pi_status=("${PIPESTATUS[@]}"); '
        + (
            _producer_status_write(status_path, 0, nonce)
            if status_path is not None
            else ""
        )
        + 'if [ "${tetrabench_pi_status[0]}" -ne 0 ]; then '
        'exit "${tetrabench_pi_status[0]}"; fi; '
        'if [ "${tetrabench_pi_status[1]}" -gt 1 ] || '
        '[ "${tetrabench_pi_status[2]}" -ne 0 ]; then exit 1; fi; exit 0'
    )
